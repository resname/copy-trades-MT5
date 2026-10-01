# IB Futures Slave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Interactive Brokers slave (futures first, any IB asset class via `secType`) to the copy-trades MT5 manager: a dedicated `ib_worker` subprocess speaking the TWS API through IB Gateway, implementing the existing OPEN/MODIFY/PARTIAL_CLOSE/CLOSE command stream with full SL/TP parity.

**Architecture:** The IPC protocol is the platform seam (per the spec). The engine stays untouched; a slave gains a `platform` field (`"mt5"` default, `"ib"`). The supervisor picks the worker entry point from `platform`. `manager/worker/ib/` holds a `FakeIbGateway` (in-memory IB, also the test/dev fake) and `RealIbGateway` (`ib_async` glue) behind one `IbGateway` protocol, plus `contracts.py` (front-month/rollover math), `tags.py` (synthetic position tickets), and `worker.py` (the loop + command execution mirroring `mt5_worker`).

**Tech Stack:** Python 3.11+, `ib_async==2.1.0` (pinned, pip-installable community client for the TWS API), pytest. IB Gateway run/login is the user's (IBC-compatible); the manager only opens a socket.

**Spec:** `docs/superpowers/specs/2026-10-02-ib-futures-slave-design.md` — read it first; this plan argues from it.

## Global Constraints

- **Engine untouched.** No file under `manager/engine/` changes. All IB knowledge lives in `manager/worker/ib/` plus additive config fields.
- **Additive only.** A slave config without `platform` behaves exactly as today. MT5-path tests must pass byte-identical (run full pytest at the end of every task).
- **Units convention (locks spec §8):** one "slave lot" for an IB slave is **one contract** for `balance_step` sizing (`step_size` tiers, e.g. `1.0` contract per tier) and **one master lot** for `copy_master`/`fixed_lot`, which the worker converts to contracts via `ratio = master_point_value / contract_multiplier`, floored with the codebase's `+1e-9` epsilon guard. `ratio == 1.0` for `balance_step`.
- **Engine-reported `SymbolInfo` for IB slaves is virtual:** `volume_step=0.01`, `volume_min=0.01`, `volume_max=100000.0` so the existing sizing pipeline passes master-lot-unit volumes through unclamped; the worker converts to whole contracts (floors to 0 → skip, logged via a failed ack with retcode 0 and a clear error — never silent).
- **Netting model:** IB positions are net per contract. Multiple master tickets mapping to the same contract+side share one net position. Per-command semantics use the *record's own share* (`holdings = share_contracts − already_closed`, from the `CPY#` tag + per-tag close history), clamped to the live net; CLOSE never sweeps another master record's share.
- **Tags:** the engine already encodes the linkage comment on OPEN (`cmd.comment` = `CPY#<ticket>|MV..|SV..`, ≤31 chars). The IB worker uses it verbatim as `orderRef` on the parent *and* both children; recovery decodes it via the existing `decode_comment`.
- **Dependency:** `ib_async==2.1.0` (pinned) added to `pyproject.toml` and installed explicitly by the updater's `--no-deps` reinstall path. All `ib_async` imports stay inside `manager/worker/ib/adapter.py`; nothing else imports it.
- **IB master is out of scope:** `ib_worker` fatals on `role="master"`.
- **Testing:** pytest (`pip install -e .[test]` from repo root). GUI tests need the app venv (has PySide6) — headless runs skip them; run the full suite in the app venv before merging GUI work.
- **Windows-first:** like everything in this repo, run from the repo root; paths in commands are forward-slash for Git Bash.

## Review Focus

Failure modes the spec implies but no task's tests exercise fully — each line's pinning test is named in the task that owns the code.

1. **Netting collision:** two master tickets map to the same contract+side; a CLOSE of one must reduce only that record's remaining share, never sweep the other's — and the closed record's stale children must be retired while survivors' children keep their share. Pinned: Task 7, `test_close_only_closes_own_share` (fake + `_sync_protection_after_reduce`).
2. **Bracket leak on partial close:** a partial without resizing children lets a stop fire past the remaining holding and open a reverse trade. Pinned: Task 7, `test_partial_resizes_children`.
3. **Contract-floor epsilon:** `units × ratio` sitting an ulp below an integer must be that integer (the 0.28-lot bug class already survived this repo once). Pinned: Task 7, `test_contracts_epsilon_floor`.
4. **Gateway restart mid-command:** an unfilled/unacknowledged order must time out into a failed ack, never hang the pipe. Pinned: Task 5, `test_reduce_timeout_models_gateway_restart` (the fake models the failure return; Task 6's `_wait_terminal` wait loop carries it) and Task 7, `test_command_timeout_rejects_not_hangs` (the worker surfaces the failed gateway call).
5. **Rollover boundary:** an OPEN during the roll window routes to the *next* month while existing positions keep their contract; the GUI sees "rolling". Pinned: Task 3, `test_pick_front_month_rolls_in_window` (month math); Task 7, `test_open_uses_rolled_contract` and `test_close_targets_record_month_not_rolled_front` (the worker routes the recorded month, never the rolled front month, for an existing position).
6. **Read-Only API cannot be probed over the socket** — the Start gate for IB slaves is "socket connected"; a read-only Gateway surfaces as a clear failed-ack error on the first order instead. Pinned: Task 5, `test_open_bracket_read_only_server`.

---

### Task 1: IPC additive fields (`StatusMsg.detail`, `ReconfigureMsg.contracts`)

**Files:**
- Modify: `manager/ipc/messages.py`
- Test: `manager/tests/test_messages.py`

**Interfaces:**
- Produces: `StatusMsg(..., detail: str = "")` and `ReconfigureMsg(..., contracts: dict[str, dict] = field(default_factory=dict))` — consumed by Tasks 7 (worker fills `detail`, handles IB reconfigure), 8 (supervisor forwards `detail`), 10 (GUI shows `detail`). Defaults keep MT5 messages unchanged (a MT5 `StatusMsg` has `detail=""`, IB config dicts absent ⇒ `"mt5"` semantics everywhere).

- [ ] **Step 1: Write the failing test**

Add to the end of `manager/tests/test_messages.py` (match the file's existing roundtrip style):

```python
def test_status_msg_detail_roundtrip():
    m = StatusMsg(source_id="ib1", role="slave", connected=True, login=0,
                  balance=1.0, equity=1.0, currency="USD", server="ib:4002",
                  trade_allowed=True, detail="YM 202612 active")
    copy = _roundtrip(m)
    assert copy.detail == "YM 202612 active"


def test_status_msg_detail_defaults_empty():
    m = StatusMsg(source_id="m", role="master", connected=True, login=1,
                  balance=1.0, equity=1.0, currency="USD", server="s")
    assert m.detail == ""


def test_reconfigure_msg_contracts_roundtrip():
    m = ReconfigureMsg(source_id="ib1", symbol_map_csv="US30=YM",
                       normalize_sltp=True,
                       contracts={"YM": {"exchange": "CME", "sec_type": "FUT",
                                          "master_point_value": 1.0}})
    copy = _roundtrip(m)
    assert copy.contracts == {"YM": {"exchange": "CME", "sec_type": "FUT",
                                      "master_point_value": 1.0}}


def test_reconfigure_msg_contracts_defaults_empty():
    m = ReconfigureMsg(source_id="s1", symbol_map_csv="US30=WS30",
                       normalize_sltp=True)
    assert m.contracts == {}
```

If the test file has no `_roundtrip` helper, use the existing per-test send/recv pair it uses elsewhere (`send_msg`/`recv_msg` over `multiprocessing.Pipe`) — mirror the neighboring tests' exact mechanism rather than inventing one.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_messages.py -k detail_or_contracts -v` (or run the four new tests by name with `-k "detail or reconfigure"`)
Expected: FAIL with `TypeError: ... unexpected keyword argument 'detail'` / `'contracts'`.

- [ ] **Step 3: Implement**

In `manager/ipc/messages.py`, add to `StatusMsg` (after `trade_allowed: bool = True`):

```python
    detail: str = ""    # worker-provided status line (IB: contract/roll state)
```

and to `ReconfigureMsg` (after `normalize_sltp`):

```python
    contracts: dict[str, dict] = field(default_factory=dict)
```

(`field` is already imported in this module.) No other change: the framing layer serializes dataclass fields generically.

- [ ] **Step 4: Run full messages tests**

Run: `python -m pytest manager/tests/test_messages.py -v`
Expected: PASS (all, including pre-existing roundtrips).

- [ ] **Step 5: Commit**

```bash
git add manager/ipc/messages.py manager/tests/test_messages.py
git commit -m "feat(ipc): additive StatusMsg.detail + ReconfigureMsg.contracts"
```

---

### Task 2: Controller platform plumbing (`AccountSpec.platform`, IB worker config)

**Files:**
- Modify: `manager/app/controller.py`
- Test: `manager/tests/test_controller.py` (append new tests)

**Interfaces:**
- Produces: `AccountSpec.platform: str = "mt5"`, `AccountSpec.ib_host: str = "127.0.0.1"`, `AccountSpec.ib_port: int = 4002`, `AccountSpec.ib_client_id: int = 7`, `AccountSpec.contract_map: dict[str, dict] = field(default_factory=dict)`. `build_worker_configs` emits the IB config dict (exact keys in the code below) — consumed by Task 8 (supervisor dispatch) and Task 9 (GUI builds this spec). `prepare()` excludes IB slaves from terminal assignment.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_controller.py`. First check how existing tests construct fakes for `terminal_manager` (look at the module-top fixtures; reuse the same fake class). Then:

```python
def _ib_spec(sid="ib1", contracts=None):
    from manager.app.controller import AccountSpec
    return AccountSpec(id=sid, terminal_path="", symbol_map_csv="US30=YM",
                       step_amount=1000.0, step_size=1.0, max_lot=100.0,
                       max_trade_age_minutes=10, normalize_sltp=True,
                       platform="ib",
                       contract_map=contracts or {"YM": {
                           "exchange": "CME", "sec_type": "FUT",
                           "master_point_value": 1.0}})


def test_build_worker_configs_ib_slave():
    ctrl = _controller_with_fakes()  # same fake wiring the neighboring tests use
    cfgs = ctrl.build_worker_configs(
        _master_spec(), [_ib_spec()], assigned={})  # IB slave needs NO instance
    cfg = cfgs["ib1"]
    assert cfg["platform"] == "ib"
    assert cfg["contract_map"] == {"YM": {"exchange": "CME",
                                           "sec_type": "FUT",
                                           "master_point_value": 1.0}}
    assert cfg["ib_port"] == 4002
    assert cfg["sizing_mode"] == "balance_step"
    assert cfg["roll_days"] == 5
    assert cfg["ack_timeout_ms"] == 15000


def test_build_worker_configs_mt5_unchanged():
    ctrl = _controller_with_fakes()
    cfgs = ctrl.build_worker_configs(_master_spec(),
                                     [_mt5_spec()], assigned=_fake_assigned())
    assert "platform" not in cfgs["s1"]        # mt5 config gains no keys
    assert cfgs["s1"]["terminal_path"].endswith("terminal64.exe")


def test_prepare_skips_ib_slaves_for_terminal_assignment():
    ctrl = _controller_with_fakes()
    assigned = ctrl.prepare(_master_spec(), [_ib_spec()])
    assert "ib1" not in assigned                # no terminal was assigned
    # and the duplicate-terminal-path validation still runs for mt5 accounts
```

Use the file's existing helpers for `_master_spec()` / `_mt5_spec()` / `_controller_with_fakes()` / `_fake_assigned()` — i.e., whatever the neighboring tests already use; if the tests construct these inline, inline them here too. The assertions above are the contract; the wiring must reuse the file's existing fake machinery.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_controller.py -k ib -v`
Expected: FAIL — `AccountSpec` has no `platform`/`contract_map`.

- [ ] **Step 3: Implement**

In `manager/app/controller.py`:

1. Add `field` to the dataclass import: `from dataclasses import dataclass, field`.
2. Extend `AccountSpec`:

```python
    platform: str = "mt5"
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_client_id: int = 7
    contract_map: dict[str, dict] = field(default_factory=dict)
```

3. `prepare()` — validate/assign only MT5 slaves (the master is always MT5; IB slaves have no terminal):

```python
    def prepare(self, master: AccountSpec, slaves: list[AccountSpec]
                ) -> dict[str, TerminalInstance]:
        if master.platform == "ib":
            raise ControllerError("IB accounts cannot be the master")
        seen: dict[str, str] = {}
        mt5_accounts = [self._account_dict(master)]
        for s in slaves:
            if s.platform == "ib":
                continue
            mt5_accounts.append(self._account_dict(s))
        for a in mt5_accounts:
            ov = a.get("terminal_path")
            if ov:
                exe = _normalize_override_exe(ov)
                a["terminal_path"] = exe
                if exe in seen:
                    raise ControllerError(
                        f"terminal path {exe} assigned to both "
                        f"{seen[exe]} and {a['id']}")
                seen[exe] = a["id"]
        self._status("info", "assigning terminal instances…")
        assigned = self._terminal_manager.assign(mt5_accounts)
        self._status("info", "terminal instances assigned")
        return assigned
```

4. `build_worker_configs()` — IB branch before the MT5 branch:

```python
        for s in slaves:
            if s.platform == "ib":
                cfgs[s.id] = {
                    "slave_id": s.id, "platform": "ib",
                    "symbol_map_csv": s.symbol_map_csv,
                    "normalize_sltp": s.normalize_sltp,
                    "contract_map": s.contract_map,
                    "ib_host": s.ib_host, "ib_port": s.ib_port,
                    "ib_client_id": s.ib_client_id,
                    "sizing_mode": s.sizing_mode,
                    "roll_days": 5, "ack_timeout_ms": 15000,
                    "retry_count": 3, "retry_delay_ms": 500,
                    "slave_status_interval_ms": 5000,
                }
                continue
            s_inst = assigned[s.id]
            # ... existing MT5 config dict unchanged ...
```

`start()`'s engine `add_slave` loop needs no change: it uses only the slave's trading params (symbol map + sizing), which are identical in shape for both platforms. The IB worker sets `StatusMsg.trade_allowed` from its own connection state (Task 7), so the preflight predicate works unchanged. Note in the docstring of `build_worker_configs` that `platform` is deliberately NOT emitted for MT5 slaves — the supervisor defaults to MT5 on absence (keeps old configs byte-identical).

5. `start()`'s Algo-Trading preflight names a blocked terminal via
   `cfgs[s.id]["terminal_path"]` — an IB config has no such key (KeyError
   would crash Start). Read the endpoint instead; MT5 configs keep
   `terminal_path` so MT5 preflight output is byte-identical:

```python
        disabled = [(s.id, cfgs[s.id].get("terminal_path",
                                          f"{s.ib_host}:{s.ib_port}"))
                    for s in slaves if not sup.slave_trade_allowed(s.id)]
```
   (keep the surrounding `disabled`-check code exactly as-is; only this
   subscript changes).

- [ ] **Step 4: Run tests**

Run: `python -m pytest manager/tests/test_controller.py -v`
Expected: PASS (new + all pre-existing; the pre-existing suite must not change behavior).

- [ ] **Step 5: Commit**

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(controller): platform field + IB slave worker config"
```

---

### Task 3: `manager/worker/ib/contracts.py` — contract specs + front-month/rollover math

**Files:**
- Create: `manager/worker/ib/__init__.py` (empty)
- Create: `manager/worker/ib/contracts.py`
- Test: `manager/tests/test_ib_contracts.py`

**Interfaces:**
- Produces: `ContractSpec(symbol, exchange, sec_type, master_point_value, currency="USD")`, `parse_contract_map(raw) -> dict[str, ContractSpec]` (raises `ValueError` on a malformed row — config typos must fail Start loudly, not silently drop a master symbol), `pick_front_month(candidates, today, roll_days) -> tuple[str, bool] | None` where candidates are `(expiry:str, open_interest: float|None)` pairs, expiry is `YYYYMMDD` or `YYYYMM`, and the return is `(expiry, rolling: bool)`. Consumed by Tasks 5/6 (adapter resolution) and 7 (worker).

- [ ] **Step 1: Write the failing tests**

```python
# manager/tests/test_ib_contracts.py
import pytest

from manager.worker.ib.contracts import ContractSpec, parse_contract_map, pick_front_month


def test_parse_contract_map_full_row():
    m = parse_contract_map({"YM": {"exchange": "CME", "sec_type": "fut",
                                    "master_point_value": "1.0"}})
    assert m["YM"] == ContractSpec(symbol="YM", exchange="CME", sec_type="FUT",
                                   master_point_value=1.0, currency="USD")


def test_parse_contract_map_rejects_malformed_rows():
    with pytest.raises(ValueError):
        parse_contract_map({"YM": {"sec_type": "FUT"}})          # no exchange
    with pytest.raises(ValueError):
        parse_contract_map({"YM": {"exchange": "CME", "sec_type": "FUT",
                                    "master_point_value": 0.0}})  # must be > 0
    with pytest.raises(ValueError):
        parse_contract_map({"YM": ["not", "a", "dict"]})


def test_pick_front_month_nearest_by_default():
    assert pick_front_month([("20261201", None), ("20270301", None)],
                            today="20261002", roll_days=5) == ("20261201", False)


def test_pick_front_month_rolls_in_window():
    # inside the 5-day roll window: next month is chosen, state says rolling
    chosen, rolling = pick_front_month([("20261218", 3000), ("20270318", 2000)],
                                       today="20261215", roll_days=5)
    assert chosen == "20270318" and rolling is True


def test_pick_front_month_no_roll_outside_window():
    chosen, rolling = pick_front_month([("20261218", 3000), ("20270318", 2000)],
                                       today="20261201", roll_days=5)
    assert chosen == "20261218" and rolling is False


def test_pick_front_month_prefers_open_interest():
    chosen, _ = pick_front_month([("20261201", 100.0), ("20270301", 9500.0)],
                                 today="20261002", roll_days=5)
    assert chosen == "20270301"


def test_pick_front_month_zero_oi_falls_back_to_nearest():
    chosen, _ = pick_front_month([("20261201", 0.0), ("20270301", 0.0)],
                                 today="20261002", roll_days=5)
    assert chosen == "20261201"


def test_pick_front_month_none_when_no_future():
    assert pick_front_month([("20250601", None)], today="20261002",
                            roll_days=5) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_ib_contracts.py -v`
Expected: FAIL — `ModuleNotFoundError: ... manager.worker.ib`.

- [ ] **Step 3: Implement**

Create `manager/worker/ib/__init__.py` (empty file). Create `manager/worker/ib/contracts.py`:

```python
# manager/worker/ib/contracts.py
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime

_ALLOWED_SECTYPES = ("FUT", "STK", "CASH", "IND", "CMDTY")


@dataclass(frozen=True)
class ContractSpec:
    """One slave contract as configured in an IB slave's contract map row."""
    symbol: str
    exchange: str
    sec_type: str
    master_point_value: float   # master CFD: dollars per point per lot
    currency: str = "USD"


def parse_contract_map(raw=None) -> dict[str, ContractSpec]:
    out: dict[str, ContractSpec] = {}
    for symbol, d in (raw or {}).items():
        if not isinstance(d, dict):
            raise ValueError(f"contract map: row for {symbol!r} is not a mapping")
        try:
            spec = ContractSpec(
                symbol=str(symbol),
                exchange=str(d["exchange"]).upper(),
                sec_type=str(d["sec_type"]).upper(),
                master_point_value=float(d["master_point_value"]),
                currency=str(d.get("currency", "USD")).upper(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"contract map: bad row for {symbol!r}: {exc}") from exc
        if spec.sec_type not in _ALLOWED_SECTYPES:
            raise ValueError(
                f"contract map: sec_type {spec.sec_type!r} not allowed "
                f"for {symbol!r} (allowed: {', '.join(_ALLOWED_SECTYPES)})")
        if spec.master_point_value <= 0.0:
            raise ValueError(
                f"contract map: master_point_value must be > 0 for {symbol!r}")
        out[spec.symbol] = spec
    return out


def pick_front_month(candidates, today: str, roll_days: int = 5):
    """candidates: [(expiry, open_interest|None)], expiry 'YYYYMMDD' or 'YYYYMM';
    today 'YYYYMMDD'. Among contracts not yet expired: highest open interest
    wins (ties -> later month); with no usable interest data, nearest expiry.
    Inside the roll window (<= roll_days to expiry) the NEXT month is picked
    for new orders instead. Returns (expiry, rolling) or None."""
    future = sorted((c, oi) for c, oi in candidates
                    if c[:6] >= today[:6] and (oi is None or oi is not False))
    if not future:
        return None
    with_oi = [(c, float(oi)) for c, oi in future if oi and float(oi) > 0.0]
    chosen = (max(with_oi, key=lambda t: (t[1], t[0]))[0] if with_oi
              else future[0][0])
    rolling = False
    if _days_until(chosen, today) <= roll_days:
        later = [c for c, _ in future if c > chosen]
        if later:
            chosen, rolling = later[0], True
    return chosen, rolling


def _days_until(expiry: str, today: str) -> int:
    if len(expiry) == 8:
        ed = datetime.strptime(expiry, "%Y%m%d").date()
    else:
        y, m = int(expiry[:4]), int(expiry[4:6])
        ed = date(y, m, calendar.monthrange(y, m)[1])
    return (ed - datetime.strptime(today, "%Y%m%d").date()).days
```

Import `date` too (`from datetime import date, datetime`).

- [ ] **Step 4: Run tests**

Run: `python -m pytest manager/tests/test_ib_contracts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/worker/ib/ manager/tests/test_ib_contracts.py
git commit -m "feat(ib): contract specs + front-month/rollover math"
```

---

### Task 4: `manager/worker/ib/tags.py` — synthetic position tickets

**Files:**
- Create: `manager/worker/ib/tags.py`
- Test: `manager/tests/test_ib_tags.py`

**Interfaces:**
- Consumes: `manager.engine.linkage.decode_comment` (existing) — the engine already encodes `cmd.comment = "CPY#<master_ticket>|MV<..>|SV<..>"` on OPEN; the IB worker reuses it as the `orderRef` tag verbatim (Global Constraints).
- Produces: `synthetic_ticket(symbol: str, side: int) -> int` — stable positive int derived from contract+side (Global Constraints: netting model), deterministic across worker restarts; and `tag_master_ticket(tag: str) -> int | None`. Consumed by Task 7 (`OPEN` ack `slave_ticket`, position lookup, recovery records).

- [ ] **Step 1: Write the failing tests**

```python
# manager/tests/test_ib_tags.py
from manager.engine.models import BUY, SELL
from manager.worker.ib.tags import synthetic_ticket, tag_master_ticket


def test_synthetic_ticket_stable_and_positive():
    a = synthetic_ticket("YM", BUY)
    assert a == synthetic_ticket("YM", BUY)  # deterministic across processes
    # ticket space is [2e9, 3e9) — above every real MT5 ticket, no 2**31 cap
    assert 2_000_000_000 <= a < 3_000_000_000


def test_synthetic_ticket_differs_by_side_and_symbol():
    assert synthetic_ticket("YM", BUY) != synthetic_ticket("YM", SELL)
    assert synthetic_ticket("YM", BUY) != synthetic_ticket("ES", BUY)


def test_tag_master_ticket_decodes_engine_comment():
    from manager.engine.linkage import encode_comment
    tag = encode_comment(12345, 0.28, 2.0)
    assert tag_master_ticket(tag) == 12345
    assert tag_master_ticket("not a tag") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_ib_tags.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# manager/worker/ib/tags.py
from __future__ import annotations

import hashlib

from manager.engine.linkage import decode_comment


def synthetic_ticket(symbol: str, side: int) -> int:
    """Stable positive id for an IB net position (contract + side). IB has no
    position tickets; the engine's RecordTable needs an int slave_ticket."""
    digest = hashlib.blake2b(f"pos|{symbol}|{side}".encode(),
                             digest_size=8).digest()
    return 2_000_000_000 + int.from_bytes(digest, "big") % 1_000_000_000


def tag_master_ticket(tag: str) -> int | None:
    decoded = decode_comment(tag)
    if decoded is None:
        return None
    return decoded[0]
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest manager/tests/test_ib_tags.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/worker/ib/tags.py manager/tests/test_ib_tags.py
git commit -m "feat(ib): synthetic position tickets + tag decoding"
```

---

### Task 5: `manager/worker/ib/adapter.py` — `IbGateway` protocol + `FakeIbGateway`

**Files:**
- Create: `manager/worker/ib/adapter.py`
- Test: `manager/tests/test_ib_adapter.py`

**Interfaces:**
- Consumes: `pick_front_month` (Task 3), engine `BUY`/`SELL` ints (`manager.engine.models`).
- Produces (the single protocol Task 7 programs against; Task 6 implements it for real):

```python
@dataclass
class IbOrder:
    order_id: int
    tag: str            # the CPY#... linkage comment from cmd.comment
    symbol: str
    action: str         # "BUY" | "SELL" (IB order convention)
    order_type: str     # "MKT" | "LMT" | "STP"
    qty: float
    month: str = ""     # resolved contract month ("" = non-FUT)
    limit_price: float = 0.0
    stop_price: float = 0.0
    parent_id: int = 0
    oca_group: str = ""
    active: bool = True     # children sit inactive until the parent fills
    filled: bool = False


@dataclass
class IbExecution:
    order_id: int
    tag: str
    action: str
    qty: float


@dataclass
class IbPosition:
    symbol: str
    side: int           # 0 = BUY(long), 1 = SELL(short) — engine convention
    qty: float          # absolute, in contracts
    open_price: float
    month: str = ""     # the contract this net position lives on


class IbGateway(Protocol):
    def initialize(self, host: str, port: int, client_id: int) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> str: ...
    def read_only(self) -> bool: ...
    def resolve_contract(self, symbol: str, today: str, exchange: str = "",
                         sec_type: str = "FUT", roll_days: int = 5
                         ) -> dict | None:
        # -> {"month": str, "rolling": bool, "multiplier": float,
        #     "tick_size": float, "margin_est": float}
    def tick(self, symbol: str) -> tuple[float, float] | None: ...
    def account(self) -> dict: ...
    def net_positions(self) -> list[IbPosition]: ...
    def tagged_orders(self) -> list[IbOrder]: ...
    def closed_per_tag(self, tag: str) -> float: ...
    def open_bracket(self, symbol: str, side: int, qty: float,
                     sl: float, tp: float, tag: str,
                     month: str = "", timeout_s: float = 15.0
                     ) -> tuple[bool, float, str]: ...
    def set_protective(self, symbol: str, side: int, tag: str,
                       sl: float, tp: float, qty: float,
                       month: str = "", timeout_s: float = 15.0
                       ) -> tuple[bool, str]: ...
    def reduce(self, symbol: str, side: int, qty: float, tag: str,
               close: bool, month: str = "", timeout_s: float = 15.0
               ) -> tuple[bool, float, str]: ...
```

`month` (empty = use the currently resolved front month) pins the contract the
command targets: an OPEN passes the freshly resolved month; MODIFY/PARTIAL/CLOSE
pass the position's own recorded month (rollover must never re-route an
*existing* position to the next month — the fake only carries one contract per
symbol, so the pin that the worker routes the recorded month is the spy test in
Task 7, `test_close_targets_record_month_not_rolled_front`; the real Gateway
honors it via `_contract_for(symbol, month)` in Task 6). `timeout_s` is honored
by Task 6's wait loops; the fake fills synchronously and ignores it.

`FakeIbGateway.__init__` kwargs (this signature IS the supervisor's IB `fake_state` for tests):

```python
def __init__(self, account=None, prices=None, contract_multipliers=None,
             tick_sizes=None, months=None, open_interest=None,
             margin_per_contract=None, server_read_only=False,
             fail_reduce_timeout=False):
```

Semantics the fake must reproduce (these are the unit-test oracle for Task 7): market fills at current bid/ask; `open_bracket` places a filled MKT parent + tagged STP/LMT children in one OCA group (children activate on parent fill); `set_price` moves the market and triggers stops/limits with OCA cancel of the sibling; `reduce` fills the opposite side only up to the same-side net quantity (a no-op reduce returns `(True, 0.0, "")` — the tolerated no-op of the spec's race rule); `close=True` additionally cancels that tag's children once the net position is flat; `closed_per_tag` accumulates reduce-order and fired-child fills; `fail_reduce_timeout=True` makes every `reduce` return `(False, 0.0, "timeout waiting for fill")` to model Task 6's timeout path.

- [ ] **Step 1: Write the failing tests**

```python
# manager/tests/test_ib_adapter.py
from manager.engine.models import BUY, SELL
from manager.worker.ib.adapter import FakeIbGateway


def make_gw(**kw):
    kw.setdefault("prices", {"YM": (45_000.0, 45_001.0)})
    kw.setdefault("contract_multipliers", {"YM": 5.0})
    kw.setdefault("tick_sizes", {"YM": 0.25})
    kw.setdefault("months", {"YM": ["202612", "202703"]})
    kw.setdefault("open_interest", {"YM": {"202612": 5000.0}})
    kw.setdefault("margin_per_contract", {"YM": 12_000.0})
    return FakeIbGateway(**kw)


def test_initialize_and_account():
    gw = make_gw(account={"balance": 50_000.0, "margin_available": 90_000.0})
    assert gw.initialize("127.0.0.1", 4002, 7) is True
    acc = gw.account()
    assert acc["balance"] == 50_000.0 and acc["margin_available"] == 90_000.0


def test_resolve_contract_picks_front_month():
    gw = make_gw()
    assert gw.initialize("h", 1, 1)
    r = gw.resolve_contract("YM", today="20261002", exchange="CME")
    assert r == {"month": "202612", "rolling": False, "multiplier": 5.0,
                 "tick_size": 0.25, "margin_est": 12_000.0}


def test_resolve_contract_unknown_symbol_is_none():
    gw = make_gw()
    gw.initialize("h", 1, 1)
    assert gw.resolve_contract("NOPE", today="20261002", exchange="CME") is None


def test_open_bracket_fills_and_creates_children():
    gw = make_gw(); gw.initialize("h", 1, 1)
    ok, px, err = gw.open_bracket("YM", BUY, 2.0, 44_950.0, 45_100.0,
                                  "CPY#1|MV0.28|SV2")
    assert ok and px == 45_001.0 and err == ""
    pos = gw.net_positions()
    assert len(pos) == 1 and pos[0].side == BUY and pos[0].qty == 2.0
    kids = [o for o in gw.tagged_orders() if o.order_type in ("STP", "LMT")]
    assert {k.order_type for k in kids} == {"STP", "LMT"}
    assert all(k.active for k in kids)
    oca = {k.oca_group for k in kids}
    assert len(oca) == 1 and next(iter(oca)) != ""


def test_stop_fires_with_oca_sibling_cancel():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 2.0, 44_950.0, 45_100.0, "CPY#1|MV0.28|SV2")
    gw.set_price("YM", 44_940.0, 44_941.0)      # bid breaks the stop
    kids = [o for o in gw.tagged_orders() if o.order_type in ("STP", "LMT")]
    stp = next(k for k in kids if k.order_type == "STP")
    lmt = next(k for k in kids if k.order_type == "LMT")
    assert stp.filled and lmt.filled            # stopped, sibling OCA-cancelled
    assert gw.closed_per_tag("CPY#1|MV0.28|SV2") == 2.0
    assert gw.net_positions() == []             # flat


def test_partial_reduce_then_close():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 4.0, 44_950.0, 45_100.0, "CPY#1|MV0.28|SV4")
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV4", close=False)
    assert ok and filled == 2.0
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV4", close=True)
    assert ok and filled == 2.0
    assert gw.net_positions() == []
    # close=True + flat net => that tag's children are cancelled
    assert all(o.filled for o in gw.tagged_orders())
    assert gw.closed_per_tag("CPY#1|MV0.28|SV4") == 4.0


def test_reduce_cannot_overshoot_net():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 2.0, 0.0, 0.0, "CPY#1|MV0.28|SV2")
    ok, filled, _ = gw.reduce("YM", BUY, 5.0, "CPY#1|MV0.28|SV2", close=True)
    assert ok and filled == 2.0                 # clamped to the net long


def test_reduce_noop_when_position_gone():
    gw = make_gw(); gw.initialize("h", 1, 1)
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV2", close=True)
    assert ok and filled == 0.0                 # tolerated race no-op


def test_reduce_timeout_models_gateway_restart():
    gw = make_gw(fail_reduce_timeout=True); gw.initialize("h", 1, 1)
    ok, filled, err = gw.reduce("YM", BUY, 1.0, "CPY#1|MV0.28|SV2", close=True)
    assert not ok and err == "timeout waiting for fill"


def test_open_bracket_read_only_server():
    gw = make_gw(server_read_only=True); gw.initialize("h", 1, 1)
    ok, px, err = gw.open_bracket("YM", BUY, 1.0, 0.0, 0.0, "CPY#1|MV0.1|SV1")
    assert not ok and "Read-Only" in err


def test_tick_and_positions_open_price():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 2.0, 0.0, 0.0, "CPY#1|MV0.1|SV2")
    assert gw.tick("YM") == (45_000.0, 45_001.0)
    assert gw.net_positions()[0].open_price == 45_001.0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_ib_adapter.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `manager/worker/ib/adapter.py` with, in order: `IbOrder`/`IbExecution`/`IbPosition` dataclasses, the `IbGateway` Protocol (exact signatures above), constants `OCA_PREFIX = "ct-oca-"` and `MARGIN_FRACTION = 0.9` (used by the worker in Task 7, exported here for one place to tune), then `FakeIbGateway`:

```python
# manager/worker/ib/adapter.py  (FakeIbGateway portion)
class FakeIbGateway:
    """In-memory IB with deterministic fills — the unit-test oracle for the
    IB worker and the supervisor's fake adapter for IB slaves."""

    def __init__(self, account=None, prices=None, contract_multipliers=None,
                 tick_sizes=None, months=None, open_interest=None,
                 margin_per_contract=None, server_read_only=False,
                 fail_reduce_timeout=False):
        self._account = {"balance": 10000.0, "equity": 10000.0,
                         "currency": "USD", "margin_available": 1_000_000.0,
                         "account_id": "DU123"}
        self._account.update(account or {})
        self.prices = {**({"YM": (45_000.0, 45_001.0), "ES": (6_000.0, 6_001.0)}
                          if prices is None else prices)}
        self.multipliers = {**({"YM": 5.0, "ES": 50.0}
                               if contract_multipliers is None
                               else contract_multipliers)}
        self.price_ticks = {**({"YM": 0.25, "ES": 0.25}
                               if tick_sizes is None else tick_sizes)}
        self.months = {**({"YM": ["202612"], "ES": ["202612", "202703"]}
                          if months is None else months)}
        self.open_interest = {} if open_interest is None else open_interest
        self.margin_per_contract = ({} if margin_per_contract is None
                                    else margin_per_contract)
        self.connected = False
        self._read_only = server_read_only
        self._fail_reduce_timeout = fail_reduce_timeout
        self._next_oid = 1000
        self.orders: list[IbOrder] = []
        self.positions: dict[str, float] = {}       # symbol -> signed net
        self.avg_price: dict[str, float] = {}       # symbol -> avg open price
        self.pos_month: dict[str, str] = {}         # symbol -> open's month
        self.executions: list[IbExecution] = []

    # ---- lifecycle ----
    def initialize(self, host: str, port: int, client_id: int) -> bool:
        self.connected = True
        return True

    def shutdown(self) -> None:
        self.connected = False

    def last_error(self) -> str:
        return ""

    def read_only(self) -> bool:
        return self._read_only

    def _alloc(self) -> int:
        self._next_oid += 1
        return self._next_oid

    @staticmethod
    def _action(side: int) -> str:
        return "BUY" if side == BUY else "SELL"

    # ---- contracts / market data ----
    def resolve_contract(self, symbol: str, today: str, exchange: str = "",
                         sec_type: str = "FUT", roll_days: int = 5) -> dict | None:
        rows = self.months.get(symbol)
        if rows is None:
            return None
        oi = self.open_interest.get(symbol, {})
        picked = pick_front_month([(m, oi.get(m)) for m in rows], today,
                                  roll_days=roll_days)
        if picked is None:
            return None
        month, rolling = picked
        return {"month": month, "rolling": rolling,
                "multiplier": self.multipliers.get(symbol, 1.0),
                "tick_size": self.price_ticks.get(symbol, 0.25),
                "margin_est": self.margin_per_contract.get(symbol, 0.0)}

    def tick(self, symbol: str) -> tuple[float, float] | None:
        return self.prices.get(symbol)

    def account(self) -> dict:
        return dict(self._account)

    # ---- reads ----
    def net_positions(self) -> list[IbPosition]:
        out = []
        for symbol, signed in sorted(self.positions.items()):
            if signed == 0.0:
                continue
            out.append(IbPosition(symbol=symbol,
                                  side=BUY if signed > 0 else SELL,
                                  qty=abs(signed),
                                  open_price=self.avg_price.get(symbol, 0.0),
                                  month=self.pos_month.get(symbol, "")))
        return out

    def tagged_orders(self) -> list[IbOrder]:
        return [o for o in self.orders if o.tag.startswith("CPY#")]

    def closed_per_tag(self, tag: str) -> float:
        return sum(e.qty for e in self.executions if e.tag == tag)

    # ---- fills ----
    def _apply_fill(self, symbol: str, action: str, qty: float,
                    price: float) -> None:
        signed = qty if action == "BUY" else -qty
        current = self.positions.get(symbol, 0.0)
        flipped = current != 0.0 and (current > 0) != (signed > 0)
        if flipped:
            self.avg_price.pop(symbol, None)
        self.positions[symbol] = current + signed
        if self.positions[symbol] == 0.0:
            self.positions.pop(symbol, None)
            self.avg_price.pop(symbol, None)
        elif signed * self.positions[symbol] > 0:   # increased exposure
            prev = abs(self.positions[symbol]) - qty
            base = self.avg_price.get(symbol, price)
            self.avg_price[symbol] = (base * prev + price * qty) \
                / max(prev + qty, 1e-9)
        self._trigger_checks()

    def _protect(self, parent_id: int, tag: str, symbol: str, action: str,
                 order_type: str, qty: float, *, stop_price: float = 0.0,
                 limit_price: float = 0.0, oca: str = "") -> IbOrder:
        child = IbOrder(order_id=self._alloc(), tag=tag, symbol=symbol,
                        action=action, order_type=order_type, qty=qty,
                        limit_price=limit_price, stop_price=stop_price,
                        parent_id=parent_id, oca_group=oca)
        self.orders.append(child)
        return child

    def open_bracket(self, symbol: str, side: int, qty: float,
                     sl: float, tp: float, tag: str, month: str = "",
                     timeout_s: float = 15.0) -> tuple[bool, float, str]:
        if self._read_only:
            return (False, 0.0, "IB Gateway Read-Only API is on")
        act = self._action(side)
        bid, ask = self.prices.get(symbol, (0.0, 0.0))
        px = ask if act == "BUY" else bid
        oid = self._alloc()
        self.orders.append(IbOrder(order_id=oid, tag=tag, symbol=symbol,
                                   action=act, order_type="MKT", qty=qty,
                                   month=month, filled=True))
        if month:
            self.pos_month[symbol] = month      # this net positions' contract
        oca = OCA_PREFIX + str(oid)
        prot = self._action(1 - side)   # protective side opposes the entry
        if sl > 0.0:
            self._protect(oid, tag, symbol, prot, "STP", qty,
                          stop_price=sl, oca=oca)
        if tp > 0.0:
            self._protect(oid, tag, symbol, prot, "LMT", qty,
                          limit_price=tp, oca=oca)
        self._apply_fill(symbol, act, qty, px)
        return (True, px, "")

    def set_price(self, symbol: str, bid: float, ask: float) -> None:
        self.prices[symbol] = (bid, ask)
        self._trigger_checks()

    def _trigger_checks(self) -> None:
        for o in list(self.orders):
            if o.filled or not o.active or o.order_type not in ("STP", "LMT"):
                continue
            bid, ask = self.prices.get(o.symbol, (0.0, 0.0))
            if o.order_type == "STP":
                trig = (ask >= o.stop_price if o.action == "BUY"
                        else (bid <= o.stop_price))
                trig = trig and o.stop_price > 0
            else:
                trig = (bid >= o.limit_price if o.action == "SELL"
                        else (ask <= o.limit_price))
                trig = trig and o.limit_price > 0
            if trig:
                self._fire_child(o)

    def _fire_child(self, o: IbOrder) -> None:
        o.filled = True
        o.active = False
        bid, ask = self.prices.get(o.symbol, (0.0, 0.0)) 
        px = bid if o.action == "SELL" else ask
        self.executions.append(IbExecution(order_id=o.order_id, tag=o.tag,
                                           action=o.action, qty=o.qty))
        self._apply_fill(o.symbol, o.action, o.qty, px)
        self._oca_cancel(o.oca_group, o.order_id)

    def _oca_cancel(self, oca_group: str, except_id: int) -> None:
        if not oca_group:
            return
        for sib in self.orders:
            if (sib.oca_group == oca_group and sib.order_id != except_id
                    and not sib.filled):
                sib.filled = True     # cancelled by OCA
                sib.active = False

    def set_protective(self, symbol: str, side: int, tag: str,
                       sl: float, tp: float, qty: float, month: str = "",
                       timeout_s: float = 15.0) -> tuple[bool, str]:
        if self._read_only:
            return (False, "IB Gateway Read-Only API is on")
        prot = self._action(1 - side)
        for o in self.orders:                       # cancel current children
            if (o.tag == tag and o.symbol == symbol
                    and o.order_type in ("STP", "LMT") and not o.filled):
                o.filled = True
                o.active = False
        if sl <= 0.0 and tp <= 0.0:
            return (True, "")                       # children removed, none to add
        oca = OCA_PREFIX + str(self._alloc())
        if sl > 0.0:
            self._protect(0, tag, symbol, prot, "STP", qty,
                          stop_price=sl, oca=oca)
        if tp > 0.0:
            self._protect(0, tag, symbol, prot, "LMT", qty,
                          limit_price=tp, oca=oca)
        self._trigger_checks()
        return (True, "")

    def reduce(self, symbol: str, side: int, qty: float, tag: str,
               close: bool, month: str = "", timeout_s: float = 15.0
               ) -> tuple[bool, float, str]:
        if self._fail_reduce_timeout:
            return (False, 0.0, "timeout waiting for fill")
        if self._read_only:
            return (False, 0.0, "IB Gateway Read-Only API is on")
        signed = self.positions.get(symbol, 0.0)
        if side == BUY:
            filled = min(qty, signed if signed > 0 else 0.0)
            action = "SELL"
            px = self.prices.get(symbol, (0.0, 0.0))[0]
        else:
            filled = min(qty, -signed if signed < 0 else 0.0)
            action = "BUY"
            px = self.prices.get(symbol, (0.0, 0.0))[1]
        if filled <= 0.0:
            return (True, 0.0, "")                  # tolerated race no-op
        oid = self._alloc()
        self.orders.append(IbOrder(order_id=oid, tag=tag, symbol=symbol,
                                   action=action, order_type="MKT",
                                   qty=filled, filled=True))
        self.executions.append(IbExecution(order_id=oid, tag=tag,
                                           action=action, qty=filled))
        self._apply_fill(symbol, action, filled, px)
        if close and self.positions.get(symbol, 0.0) == 0.0:
            for o in self.orders:                   # kill this tag's children
                if (o.tag == tag and o.symbol == symbol
                        and o.order_type in ("STP", "LMT")
                        and not o.filled):
                    o.filled = True
                    o.active = False
                    self._oca_cancel(o.oca_group, o.order_id)
        return (True, filled, "")
```

Note the fake's position book is *net per symbol* (like IB), not per (symbol, side): `net_positions()` derives side from the sign.

- [ ] **Step 4: Run tests**

Run: `python -m pytest manager/tests/test_ib_adapter.py -v`
Expected: PASS. (The `open_price` test relies on the avg-price tracking in `_apply_fill` — if that bookkeeping reads wrong, fix the tracker, never the test's asserted value.)

- [ ] **Step 5: Commit**

```bash
git add manager/worker/ib/adapter.py manager/tests/test_ib_adapter.py
git commit -m "feat(ib): IbGateway protocol + FakeIbGateway with bracket/OCA fills"
```

---

### Task 6: `RealIbGateway` — `ib_async` glue

**Files:**
- Modify: `manager/worker/ib/adapter.py` (append the class)
- Modify: `pyproject.toml` (add the dependency)
- Test: `manager/tests/test_ib_adapter.py` (append; real Gateway is integration-tested only, unit tests cover the pure glue seams)

**Interfaces:**
- Consumes: `IbGateway` protocol, `pick_front_month` (Task 3).
- Produces: `RealIbGateway` — same methods, implemented on `ib_async`. All `ib_async` imports stay inside this file (Global Constraints). Real market-data/account/order APIs are exercised later via the paper-Gateway smoke doc (Task 11).

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, under `[project] dependency` list, add exactly:

```toml
    "ib_async==2.1.0",
```

Run: `python -m pip show ib_async` — if absent, `pip install -e .` from the repo root installs it now.
Expected: `Name: ib_async`, `Version: 2.1.0`.

- [ ] **Step 2: Implement**

Append to `manager/worker/ib/adapter.py`:

```python
class RealIbGateway:
    """IbGateway over ib_async (TWS API). Synchronous style: each method
    submits and waits (bounded by the worker's ack_timeout contract). All
    ib_async imports live here — MT5-only installs never import this module."""

    def __init__(self):
        from ib_async import IB     # ImportError = clear error, surfaced below
        self._IB = IB
        self.ib = None
        self._account_id = ""
        self._last_error = ""
        self._resolved: dict[str, dict] = {}     # symbol -> resolve result
        self._contracts: dict[str, object] = {}  # symbol -> current Contract
        self._contracts_by_month: dict[tuple[str, str], object] = {}

    # ---- lifecycle ----
    def initialize(self, host: str, port: int, client_id: int) -> bool:
        try:
            ib = self._IB()
            ib.connect(host, port, clientId=client_id, timeout=15.0)
        except Exception as exc:
            self._last_error = f"IB connect failed ({host}:{port}): {exc}"
            return False
        if not ib.isConnected():
            self._last_error = f"IB connect failed ({host}:{port})"
            return False
        self.ib = ib
        try:
            ib.reqPositions()
            self._account_id = (ib.managedAccounts or "").split(",")[0]
            if self._account_id:
                ib.reqAccountUpdates(True, self._account_id)
        except Exception as exc:
            self._last_error = f"IB setup failed: {exc}"
            return False
        return True

    def shutdown(self) -> None:
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:
                pass
            self.ib = None

    def last_error(self) -> str:
        return self._last_error

    def read_only(self) -> bool:
        # TWS/Gateway exposes no read-only introspection over the socket; the
        # Start gate is connect-only and a read-only Gateway surfaces on the
        # first rejected order (see spec + Review Focus #6).
        return False

    # ---- contracts ----
    def resolve_contract(self, symbol: str, today: str, exchange: str = "",
                         sec_type: str = "FUT", roll_days: int = 5) -> dict | None:
        from ib_async import Contract
        if self.ib is None:
            return None
        try:
            if sec_type == "FUT":
                probe = Contract(symbol=symbol, secType="FUT",
                                 exchange=exchange or "CME")
                details = sorted(self.ib.reqContractDetails(probe),
                                 key=lambda d: d.lastTradeDateOrContractMonth)
                months = [(d.lastTradeDateOrContractMonth, None)
                          for d in details]
                picked = pick_front_month(months, today, roll_days=roll_days)
                if picked is None:
                    self._last_error = f"no tradable future month for {symbol}"
                    return None
                month, rolling = picked
                contract = Contract(symbol=symbol, secType="FUT",
                                    exchange=exchange or "CME",
                                    lastTradeDateOrContractMonth=month)
                d0 = next((d for d in details
                           if d.lastTradeDateOrContractMonth == month),
                          details[0])
            else:
                probe = Contract(symbol=symbol, secType=sec_type,
                                 exchange=exchange or "")
                details = self.ib.reqContractDetails(probe)
                if not details:
                    self._last_error = f"no contract details for {symbol}"
                    return None
                month, rolling, d0 = "", False, details[0]
                contract = d0.contract
        except Exception as exc:
            self._last_error = f"contract resolution failed for {symbol}: {exc}"
            return None
        try:
            mult = float(d0.multiplier) if d0.multiplier else 1.0
            tick = float(d0.minTick) if d0.minTick else 0.0
        except (TypeError, ValueError):
            mult, tick = 1.0, 0.0
        margin_est = self._margin_estimate(contract)
        res = {"month": month, "rolling": rolling, "multiplier": mult,
               "tick_size": tick, "margin_est": margin_est,
               "sec_type": sec_type, "exchange": exchange or ""}
        self._resolved[symbol] = res
        self._contracts[symbol] = contract
        if month:
            self._contracts_by_month[(symbol, month)] = contract
        return res

    def _margin_estimate(self, contract) -> float:
        """whatIf probe: real maintenance margin per 1 contract. 0.0 = unknown
        (the worker then skips the margin guard rather than refusing)."""
        from ib_async import MarketOrder
        try:
            probe = MarketOrder("BUY", 1)
            probe.whatIf = True
            trade = self.ib.placeOrder(contract, probe)
            margin = float(trade.orderStatus.initMarginAfter or 0.0) \
                or float(trade.orderStatus.maintMarginAfter or 0.0)
            self.ib.cancelOrder(probe)
            return margin
        except Exception:
            return 0.0

    # ---- market data / account ----
    def tick(self, symbol: str) -> tuple[float, float] | None:
        if self.ib is None:
            return None
        contract = self._contracts.get(symbol)  # the resolved contract
        if contract is None:
            return None
        try:
            t = self.ib.reqTickers(contract)[0]
        except Exception as exc:
            self._last_error = f"market data for {symbol}: {exc}"
            return None
        bid, ask = float(t.bid or 0.0), float(t.ask or 0.0)
        if bid <= 0.0 or ask <= 0.0:
            self._last_error = (f"no live market data for {symbol} "
                                f"(IB data subscription required)")
            return None
        return bid, ask

    def account(self) -> dict:
        def val(tag: str) -> float:
            return float({a.tag: a.value
                          for a in self.ib.accountValues(self._account_id)}
                         .get(tag, 0.0))
        if self.ib is None:
            return {}
        vals = {a.tag for a in self.ib.accountValues(self._account_id)}
        return {"balance": val("TotalCashValue"), "equity": val("NetLiquidation"),
                "margin_available": val("AvailableFunds"),
                "currency": "USD"}

    # ---- reads ----
    def net_positions(self) -> list[IbPosition]:
        out: list[IbPosition] = []
        if self.ib is None:
            return out
        for p in self.ib.positions():
            if not p.position:
                continue
            symbol = p.contract.symbol
            mult = float(p.contract.multiplier or 1) \
                if getattr(p.contract, "multiplier", None) else 1.0
            open_price = (abs(float(p.avgCost)) / mult) if mult else 0.0
            out.append(IbPosition(symbol=symbol,
                                  side=BUY if p.position > 0 else SELL,
                                  qty=abs(float(p.position)),
                                  open_price=open_price,
                                  month=p.contract.lastTradeDateOrContractMonth
                                  or ""))
        return out

    def tagged_orders(self) -> list[IbOrder]:
        out: list[IbOrder] = []
        if self.ib is None:
            return out
        type_map = {"MKT": "MKT", "LMT": "LMT", "STP": "STP"}
        for tr in self.ib.trades():
            ref = tr.order.orderRef or ""
            if not ref.startswith("CPY#"):
                continue
            out.append(IbOrder(
                order_id=int(tr.order.orderId), tag=ref,
                symbol=tr.contract.symbol,
                action=tr.order.action,
                order_type=type_map.get(tr.order.orderType, tr.order.orderType),
                qty=float(tr.order.totalQuantity),
                month=tr.contract.lastTradeDateOrContractMonth or "",
                limit_price=float(tr.order.lmtPrice or 0.0),
                stop_price=float(tr.order.auxPrice or 0.0),
                parent_id=int(tr.order.parentId or 0),
                oca_group=tr.order.ocaGroup or "",
                active=tr.orderStatus.status in
                       ("Presubmitted", "PendingSubmit", "Submitted", "ApiPending"),
                filled=tr.orderStatus.status == "Filled"))
        return out

    def closed_per_tag(self, tag: str) -> float:
        """Quantity closed against a tagged record: every fill under this
        tag whose side OPPOSES the record's opening fill (fired children,
        partial reduce orders, manual closes). The opening fill itself has
        the same side as its own order, so a rule keyed on the fill's own
        order action would misclassify reduce fills — key on the opening
        side instead."""
        if self.ib is None:
            return 0.0
        fills = [f for f in self.ib.fills()
                 if (f.execution.orderRef or "") == tag]
        if not fills:
            return 0.0
        open_side = fills[0].execution.side     # first fill chronologically = the open
        return sum(abs(float(f.execution.shares))
                   for f in fills if f.execution.side != open_side)
```

And the three command methods:

```python
    # ---- commands (each bounded by its internal wait loop) ----
    def open_bracket(self, symbol: str, side: int, qty: float,
                     sl: float, tp: float, tag: str, month: str = "",
                     timeout_s: float = 15.0) -> tuple[bool, float, str]:
        from ib_async import MarketOrder, StopOrder, LimitOrder
        if self.ib is None:
            return (False, 0.0, self._last_error or "not connected")
        contract = self._contract_for(symbol, month)
        if contract is None:
            return (False, 0.0, f"contract not resolved for {symbol}")
        action = "BUY" if side == BUY else "SELL"
        parent = MarketOrder(action, qty)
        parent.orderRef = tag
        try:
            trade = self.ib.placeOrder(contract, parent)
        except Exception as exc:
            return (False, 0.0, f"order rejected: {exc}")
        if not self._wait_terminal(trade, timeout_s):
            return (False, 0.0, f"timeout waiting for {action} fill ({tag})")
        if trade.orderStatus.status == "Cancelled":
            return (False, 0.0, f"order cancelled: {trade.orderStatus.warningText}")
        fill_px = float(trade.orderStatus.avgFillPrice or 0.0)
        oca = OCA_PREFIX + str(parent.orderId)
        prot = "SELL" if action == "BUY" else "BUY"
        if sl > 0.0:
            child = StopOrder(prot, qty, sl)
            child.orderRef, child.parentId, child.ocaGroup = tag, parent.orderId, oca
            self.ib.placeOrder(contract, child)
        if tp > 0.0:
            child = LimitOrder(prot, qty, tp)
            child.orderRef, child.parentId, child.ocaGroup = tag, parent.orderId, oca
            self.ib.placeOrder(contract, child)
        return (True, fill_px, "")

    def set_protective(self, symbol: str, side: int, tag: str,
                       sl: float, tp: float, qty: float, month: str = "",
                       timeout_s: float = 15.0) -> tuple[bool, str]:
        from ib_async import StopOrder, LimitOrder
        if self.ib is None:
            return (False, self._last_error or "not connected")
        contract = self._contract_for(symbol, month)
        if contract is None:
            return (False, f"contract not resolved for {symbol}")
        prot = "SELL" if side == BUY else "BUY"
        oca = OCA_PREFIX + str(int(time.time() * 1000) % 1_000_000_000)
        for tr in list(self.ib.trades()):
            ref = tr.order.orderRef or ""
            if (ref == tag and tr.contract.localSymbol == contract.localSymbol
                    and tr.order.orderType in ("STP", "LMT")
                    and tr.orderStatus.status not in ("Filled", "Cancelled")):
                self.ib.cancelOrder(tr.order)
        if sl > 0.0:
            child = StopOrder(prot, qty, sl)
            child.orderRef, child.ocaGroup = tag, oca
            self.ib.placeOrder(contract, child)
        if tp > 0.0:
            child = LimitOrder(prot, qty, tp)
            child.orderRef, child.ocaGroup = tag, oca
            self.ib.placeOrder(contract, child)
        return (True, "")

    def reduce(self, symbol: str, side: int, qty: float, tag: str,
               close: bool, month: str = "", timeout_s: float = 15.0
               ) -> tuple[bool, float, str]:
        from ib_async import MarketOrder
        if self.ib is None:
            return (False, 0.0, self._last_error or "not connected")
        contract = self._contract_for(symbol, month)
        if contract is None:
            return (False, 0.0, f"contract not resolved for {symbol}")
        action = "SELL" if side == BUY else "BUY"
        order = MarketOrder(action, qty)
        order.orderRef = tag
        try:
            trade = self.ib.placeOrder(contract, order)
        except Exception as exc:
            return (False, 0.0, f"order rejected: {exc}")
        if not self._wait_terminal(trade, timeout_s):
            return (False, 0.0, "timeout waiting for fill")
        if trade.orderStatus.status == "Cancelled":
            return (False, 0.0, f"order cancelled: {trade.orderStatus.warningText}")
        filled = min(qty, float(trade.orderStatus.cumFillQuantity or 0.0))
        if close:
            flat = self._net_qty(symbol) == 0.0
            if flat:
                for tr in list(self.ib.trades()):
                    if ((tr.order.orderRef or "") == tag
                            and tr.order.orderType in ("STP", "LMT")
                            and tr.orderStatus.status not in ("Filled", "Cancelled")):
                        self.ib.cancelOrder(tr.order)
        return (True, filled, "")

    # ---- helpers ----
    def _contract_for(self, symbol: str, month: str = ""):
        """month='': the currently resolved contract. A recorded month builds
        the exact contract an existing position lives on (rollover keeps
        existing positions on their month — Review Focus #5)."""
        if self.ib is None:
            return None
        if not month:
            return self._contracts.get(symbol)
        spec = self._resolved.get(symbol, {})
        if spec.get("sec_type", "FUT") != "FUT":
            return self._contracts.get(symbol)  # non-FUT: one contract, no months
        cached = self._contracts_by_month.get((symbol, month))
        if cached is not None:
            return cached
        from ib_async import Contract
        c = Contract(symbol=symbol, secType="FUT",
                     exchange=spec.get("exchange", ""),
                     lastTradeDateOrContractMonth=month)
        self._contracts_by_month[(symbol, month)] = c
        return c

    def _net_qty(self, symbol: str) -> float:
        total = 0.0
        for p in self.ib.positions():
            if p.contract.symbol == symbol:
                total += float(p.position)
        return total

    @staticmethod
    def _wait_terminal(trade, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if trade.orderStatus.status in ("Filled", "Cancelled"):
                return True
            trade.ib.sleep(0.1)
        return False
```

Add `import time` at the top of `adapter.py` alongside the existing imports. Prerequisitely: `reqContractDetails` on ib_async is a synchronous call returning a list; `reqTickers` blocks for a snapshot — both documented for `ib_async==2.1.0`. Note for the implementer: `ib_async` may be driven synchronously from a plain thread (it owns its own event loop); do NOT spin up an asyncio loop of your own.

- [ ] **Step 3: Unit-test the seams that need no Gateway**

The Gateway-dependent behavior gets its smoke doc (Task 11); unit tests here pin the glue seams that need no Gateway: contract-month choice over mock details, and that MT5-only imports never need `ib_async`. Append to `manager/tests/test_ib_adapter.py`. There is **no existing external-SDK stub idiom in this repo** (`test_mt5_adapter.py` imports the real, installed `MetaTrader5`-shaped stub via its `FakeMt5` class instead), so the `ib_async` stub is self-contained in this test module:

```python
class _IBStubClass:
    """Stand-in for ib_async.IB: only the methods adapter.py touches."""
    managedAccounts = "DU123"

    def __init__(self):
        self.calls: list[tuple] = []
        self._details = [self._detail("202612"), self._detail("202703")]

    @staticmethod
    def _detail(month):
        from types import SimpleNamespace
        return SimpleNamespace(lastTradeDateOrContractMonth=month,
                               multiplier="5", minTick=0.25, contract=None)

    def connect(self, host, port, clientId=0, timeout=10.0):
        self.calls.append(("connect", host, port, clientId))
        self.connected = True

    def isConnected(self):
        return getattr(self, "connected", False)

    def reqPositions(self): ...
    def reqAccountUpdates(self, subscribe, account_id): ...
    def disconnect(self): ...

    def reqContractDetails(self, contract):
        self.calls.append(("details", contract.symbol))
        return self._details


def test_real_gateway_resolve_uses_pick_front_month(monkeypatch):
    """resolve_contract over a stubbed ib_async picks the month through the
    same pure helper the fake uses (roll window -> next month)."""
    import sys
    import types
    from types import SimpleNamespace
    from manager.worker.ib import adapter as ad

    stub = types.ModuleType("ib_async")
    stub.IB = _IBStubClass
    stub.Contract = lambda **kw: SimpleNamespace(**kw)
    stub.MarketOrder = stub.StopOrder = stub.LimitOrder = \
        lambda *a, **k: SimpleNamespace(orderId=0)
    monkeypatch.setitem(sys.modules, "ib_async", stub)

    ib = _IBStubClass()
    ib.connected = True
    gw = ad.RealIbGateway()
    gw.ib = ib                      # skip connect; simulate an initialized gw
    r = gw.resolve_contract("YM", today="20261226", exchange="CME", roll_days=5)
    assert r["month"] == "202703" and r["rolling"] is True   # roll window
    assert r["multiplier"] == 5.0 and r["tick_size"] == 0.25
    # the whatIf margin probe failed (stubbed placeOrder absent here) -> 0.0
    assert r["margin_est"] == 0.0
```

(`reqContractDetails` returns two months with no interest data, so `pick_front_month` falls back to nearest — `202612` — and today sits inside the 5-day roll window, so the next month is chosen with `rolling=True`; `multiplier`/`minTick` come off the matched detail. The margin probe's `placeOrder` path raises inside the stub and is swallowed to `0.0` — that is the documented unknown-margin behavior.)

Also add (with `ib_async` absent from `sys.modules` — the class object must exist without the package importable; this is the lazy-import guarantee, and the imports must stay inside `adapter.py`):

```python
def test_adapter_import_does_not_require_ib_async():
    import manager.worker.ib.adapter as mod
    mod.RealIbGateway  # class object exists without ib_async present at import
```

(with `ib_async` absent from `sys.modules`; the class is constructed, never `initialize`d, so no import error — this is the lazy-import guarantee.)

- [ ] **Step 4: Run all IB tests**

Run: `python -m pytest manager/tests/test_ib_adapter.py manager/tests/test_ib_contracts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/worker/ib/adapter.py manager/tests/test_ib_adapter.py pyproject.toml
git commit -m "feat(ib): RealIbGateway over ib_async 2.1.0 (+dep, lazy import)"
```

---

### Task 7: `manager/worker/ib/worker.py` — IB worker loop + command execution

**Files:**
- Create: `manager/worker/ib/worker.py`
- Test: `manager/tests/test_ib_worker.py`

**Interfaces:**
- Consumes: `IbGateway` protocol + `FakeIbGateway` (Task 5), `ContractSpec`/`parse_contract_map` (Task 3), `synthetic_ticket`/tag decode via `manager.engine.linkage.decode_comment` (Task 4), engine `SIZING_BALANCE_STEP` from `manager.engine.transform`, all IPC message types + `send_msg`/`recv_msg` (existing).
- Produces: `worker_main(pipe, role: str, adapter_kind: str = "real", fake_state=None)` — **exactly the same call signature as `manager.worker.mt5_worker.worker_main`**, which is what the supervisor dispatches on (Task 8). IB `fake_state` is a kwargs dict for `FakeIbGateway`. Reads a `StartMsg(config)` where config keys are the IB dict of Task 2 (`platform="ib"`, `contract_map`, `ib_host/ib_port/ib_client_id`, `sizing_mode`, `roll_days`, `ack_timeout_ms`, `symbol_map_csv`, `normalize_sltp`, `slave_status_interval_ms`).

- [ ] **Step 1: Write the failing tests**

`manager/tests/test_ib_worker.py`. The worker's entry drives a pipe; the tests stand up the full worker loop the same way `manager/tests/test_mt5_worker.py` does — read that file first and mirror its pipe-test idiom exactly (it either runs `worker_main` in a thread with a full-duplex pipe or tests the pure functions directly; reuse whatever it does, and in addition test the pure functions directly). New pure functions to pin first:

```python
# manager/tests/test_ib_worker.py
import pytest

from manager.engine.models import BUY, SELL
from manager.ipc.messages import CommandMsg
from manager.worker.ib.adapter import FakeIbGateway
from manager.worker.ib.contracts import parse_contract_map
from manager.worker.ib.worker import (
    _contracts_for, _holdings, _symbol_info_for, execute_command,
)

CONTRACTS = parse_contract_map({"YM": {"exchange": "CME", "sec_type": "FUT",
                                        "master_point_value": 1.0}})


def make_gw(**kw):
    kw.setdefault("prices", {"YM": (45_000.0, 45_001.0)})
    kw.setdefault("contract_multipliers", {"YM": 5.0})
    kw.setdefault("tick_sizes", {"YM": 0.25})
    kw.setdefault("months", {"YM": ["202612"]})
    kw.setdefault("margin_per_contract", {"YM": 12_000.0})
    return FakeIbGateway(**kw)


def _cfg():
    """The StartMsg-config shape Task 2's `build_worker_configs` emits for
    an IB slave (the loop test passes it straight to `_slave_loop`)."""
    return {"slave_id": "ib1", "platform": "ib", "symbol_map_csv": "US30=YM",
            "normalize_sltp": True, "sizing_mode": "balance_step",
            "contract_map": {"YM": {"exchange": "CME", "sec_type": "FUT",
                                     "master_point_value": 1.0}},
            "ib_host": "127.0.0.1", "ib_port": 4002,
            "ib_client_id": 7, "roll_days": 5, "ack_timeout_ms": 15000,
            "slave_status_interval_ms": 200}


def _open_cmd(volume=0.28, sl=44_950.0, tp=45_100.0, **kw):
    from manager.engine.linkage import encode_comment, magic_for
    defaults = dict(slave_id="ib1", action="OPEN", master_ticket=123,
                    symbol="YM", volume=volume, sl=sl, tp=tp,
                    master_open_price=45_000.0, side=BUY,
                    magic=magic_for(123), master_open_volume=0.28,
                    comment=encode_comment(123, 0.28, volume))
    defaults.update(kw)
    return CommandMsg(**defaults)


def test_contracts_epsilon_floor():
    # 0.28 units at ratio 0.2 is 0.056 -> 0; 2.9999999999/1.0 -> 3
    assert _contracts_for(0.28, 0.2) == 0
    assert _contracts_for(2.9999999999, 1.0) == 3
    assert _contracts_for(0.29, 1.0) == 0        # epsilon-below-grid case


def test_ratio_balance_step_units_are_contracts():
    from manager.engine.transform import SIZING_BALANCE_STEP
    from manager.worker.ib.worker import _ratio
    assert _ratio(SIZING_BALANCE_STEP, CONTRACTS["YM"],
                  {"multiplier": 5.0}) == 1.0


def test_ratio_copy_master_uses_point_value_ratio():
    from manager.engine.transform import SIZING_COPY_MASTER
    from manager.worker.ib.worker import _ratio
    # master CFD $1/pt per lot, YM $5/pt per contract -> 1 lot = 0.2 contract
    assert _ratio(SIZING_COPY_MASTER, CONTRACTS["YM"],
                  {"multiplier": 5.0}) == pytest.approx(0.2)
```

The first OPEN direction (`0.28 × (1.0/5.0) = 0.056 → 0 contracts` must return `ok=False, retcode=0` with a "rounds to 0 contracts" error and NO bracket):

```python
def test_open_below_one_contract_is_a_logged_skip():
    gw = make_gw(); gw.initialize("h", 1, 1)
    ack = execute_command(gw, _open_cmd(volume=0.28), CONTRACTS,
                          normalize_sltp=True, sizing_mode="copy_master",
                          roll_days=5, timeout_ms=15_000)
    assert not ack.ok and ack.retcode == 0
    assert "0 contracts" in ack.error
    assert gw.tagged_orders() == [] and gw.net_positions() == []


def test_open_balance_step_units_are_contracts():
    gw = make_gw(); gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=2.0)     # balance_step: 2 units == 2 contracts
    ack = execute_command(gw, cmd, CONTRACTS, normalize_sltp=True,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000)
    assert ack.ok
    assert gw.net_positions()[0].qty == 2.0
    assert ack.slave_ticket > 1_999_999_999      # synthetic ticket space


def test_open_normalizes_sl_onto_slave_fill():
    gw = make_gw(); gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=2.0)     # master open 45000, sl 44950 -> dist 50
    ack = execute_command(gw, cmd, CONTRACTS, normalize_sltp=True,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000)
    stp = next(o for o in gw.tagged_orders() if o.order_type == "STP")
    assert stp.stop_price == pytest.approx(44_951.0)   # fill 45001 - 50


def test_open_margin_guard():
    gw = make_gw(account={"margin_available": 10_000.0})
    gw.initialize("h", 1, 1)
    ack = execute_command(gw, _open_cmd(volume=2.0), CONTRACTS,
                          normalize_sltp=True, sizing_mode="balance_step",
                          roll_days=5, timeout_ms=15_000)
    assert not ack.ok and "insufficient margin" in ack.error
    assert gw.net_positions() == []


def _holdings_setup(gw, volume=2.0, **execute_kw):
    gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=volume)
    ack = execute_command(gw, cmd, CONTRACTS, normalize_sltp=True,
                          sizing_mode="balance_step", roll_days=5,
                          timeout_ms=15_000, **execute_kw)
    return cmd, ack


def _partial_cmd(cmd, ack, new_master_volume, mv=0.28):
    # MODIFY/PARTIAL/CLOSE route by the OPEN ack's slave_ticket (the
    # synthetic ticket), exactly like an MT5 slave — not by tag lookup.
    return CommandMsg(slave_id="ib1", action="PARTIAL_CLOSE",
                      master_ticket=cmd.master_ticket,
                      slave_ticket=ack.slave_ticket,
                      new_master_volume=new_master_volume,
                      master_open_volume=mv,
                      slave_open_volume=cmd.volume)


def test_partial_resizes_children():
    gw = make_gw(); cmd, ack = _holdings_setup(gw, volume=4.0)
    ack2 = execute_command(gw, _partial_cmd(cmd, ack, 0.14), CONTRACTS,
                           normalize_sltp=True, sizing_mode="balance_step",
                           roll_days=5, timeout_ms=15_000)
    assert ack2.ok
    assert gw.net_positions()[0].qty == 2.0        # closed half: 4 -> 2
    live = [o for o in gw.tagged_orders()
            if o.order_type in ("STP", "LMT") and not o.filled]
    assert all(o.qty == 2.0 for o in live)         # REVIEW-FOCUS #2
    assert live and {o.qty for o in live} == {2.0}


def _close_cmd(cmd, ack):
    from manager.engine.linkage import magic_for
    return CommandMsg(slave_id="ib1", action="CLOSE",
                      master_ticket=cmd.master_ticket,
                      slave_ticket=ack.slave_ticket,
                      magic=magic_for(cmd.master_ticket))


def test_close_only_closes_own_share():
    # REVIEW-FOCUS #1: two records on the same contract+side net together;
    # CLOSE of one must leave the other's share standing.
    gw = make_gw(); gw.initialize("h", 1, 1)
    from manager.engine.linkage import encode_comment, magic_for
    c1 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=1,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(1),
                    master_open_volume=0.28, comment=encode_comment(1, 0.28, 2.0))
    c2 = CommandMsg(slave_id="ib1", action="OPEN", master_ticket=2,
                    symbol="YM", volume=2.0, sl=44_950.0, tp=45_100.0,
                    master_open_price=45_000.0, side=BUY, magic=magic_for(2),
                    master_open_volume=0.28, comment=encode_comment(2, 0.28, 2.0))
    ack1 = execute_command(gw, c1, CONTRACTS, True, "balance_step", 5, 15_000)
    execute_command(gw, c2, CONTRACTS, True, "balance_step", 5, 15_000)
    assert gw.net_positions()[0].qty == 4.0        # one net long of 4
    close1 = _close_cmd(c1, ack1)
    ack = execute_command(gw, close1, CONTRACTS, True, "balance_step", 5, 15_000)
    assert ack.ok
    assert gw.net_positions()[0].qty == 2.0        # record 2's share survives
    # and the closed record's children are gone (the net did NOT go flat, so
    # the fake's own close-cancel didn't run — the worker's post-close
    # protection sync retires record 1's stale stop/limit)
    assert not any(o.tag == c1.comment and not o.filled
                   and o.order_type in ("STP", "LMT")
                   for o in gw.tagged_orders())
    # ...while record 2's children stay live, sized to its surviving share
    assert all(o.qty == 2.0 for o in gw.tagged_orders()
               if o.tag == c2.comment and not o.filled
               and o.order_type in ("STP", "LMT"))


def test_command_timeout_rejects_not_hangs():
    # REVIEW-FOCUS #4: a gateway that never fills (fail_reduce_timeout models
    # a hung Gateway restart) surfaces as a failed ack with "timeout" in the
    # error — never a hung pipe loop; the position is untouched.
    gw = make_gw(fail_reduce_timeout=True)
    cmd, ack = _holdings_setup(gw, volume=2.0)
    assert ack.ok
    ack2 = execute_command(gw, _partial_cmd(cmd, ack, 0.14), CONTRACTS, True,
                           "balance_step", 5, 15_000)
    assert not ack2.ok and "timeout" in ack2.error
    assert gw.net_positions()[0].qty == 2.0        # nothing reduced


class _SpyGw(FakeIbGateway):
    """Records which contract month each command routed to (the fake books
    one contract per symbol, but the month argument is what Task 6's real
    gateway turns into the IB contract — Review Focus #5)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.open_months: list[str] = []
        self.protect_months: list[str] = []
        self.reduce_months: list[str] = []

    def open_bracket(self, symbol, side, qty, sl, tp, tag,
                     month="", timeout_s=15.0):
        self.open_months.append(month)
        return super().open_bracket(symbol, side, qty, sl, tp, tag,
                                    month=month, timeout_s=timeout_s)

    def set_protective(self, symbol, side, tag, sl, tp, qty,
                       month="", timeout_s=15.0):
        self.protect_months.append(month)
        return super().set_protective(symbol, side, tag, sl, tp, qty,
                                      month=month, timeout_s=timeout_s)

    def reduce(self, symbol, side, qty, tag, close,
               month="", timeout_s=15.0):
        self.reduce_months.append(month)
        return super().reduce(symbol, side, qty, tag, close,
                              month=month, timeout_s=timeout_s)


def _spy_gw(months, **kw):
    return _SpyGw(prices={"YM": (45_000.0, 45_001.0)},
                  contract_multipliers={"YM": 5.0},
                  tick_sizes={"YM": 0.25},
                  months={"YM": months},
                  open_interest={"YM": {m: 5000.0 - 100.0 * i  # earlier month
                                        for i, m in enumerate(months)}},
                  margin_per_contract={"YM": 12_000.0}, **kw)


def test_open_uses_rolled_contract():
    # REVIEW-FOCUS #5: inside the roll window the OPEN resolves to the NEXT
    # month and the route to the gateway carries it.
    gw = _spy_gw(["202612", "202703"])
    gw.initialize("h", 1, 1)
    ack = execute_command(gw, _open_cmd(volume=2.0), CONTRACTS, True,
                          "balance_step", 5, 15_000, today="20261226")
    assert ack.ok
    assert gw.open_months == ["202703"]


def test_close_targets_record_month_not_rolled_front():
    # The record opened before the roll window must keep its own contract:
    # a CLOSE evaluated inside the roll window still routes month "202612".
    gw = _spy_gw(["202612", "202703"])
    gw.initialize("h", 1, 1)
    cmd = _open_cmd(volume=2.0)
    ack = execute_command(gw, cmd, CONTRACTS, True, "balance_step", 5,
                          15_000, today="20261002")
    assert ack.ok and gw.open_months == ["202612"]
    close = _close_cmd(cmd, ack)
    ack2 = execute_command(gw, close, CONTRACTS, True, "balance_step", 5,
                           15_000, today="20261226")    # roll window today
    assert ack2.ok
    assert gw.reduce_months == ["202612"]
```

Then the recovery and symbol-info tests (pure builders, same module):

```python
def test_recovery_builds_records_from_tags():
    from manager.worker.ib.worker import build_recovery_records
    from manager.engine.linkage import magic_for
    gw = make_gw(); cmd, _ = _holdings_setup(gw, volume=4.0)
    records = build_recovery_records(gw)
    assert len(records) == 1
    rec = records[0]
    assert rec.master_ticket == 123
    assert rec.slave_open_volume == 4.0
    assert rec.slave_ticket > 1_999_999_999   # resolved from the parent MKT order
    assert rec.magic == magic_for(123)


def test_symbol_info_reports_virtual_volume_units():
    from manager.engine.models import SymbolInfo
    specs = CONTRACTS
    gw = make_gw(); gw.initialize("h", 1, 1)
    info = _symbol_info_for(gw, specs["YM"], today="20261002", roll_days=5)
    assert info is not None
    assert info.volume_step == 0.01 and info.volume_min == 0.01
    assert info.tick_size == 0.25
```

And a loop-level test mirroring `test_mt5_worker.py`'s existing pipe harness for `_slave_loop` (its `test_slave_loop_symbol_info_request_replies_with_requested` at lines 353–405: a real `multiprocessing.Pipe` pair, `_slave_loop` running in a daemon thread, a parent-side send/recv) — adapt it for the IB worker:

```python
def test_slave_loop_symbol_info_request_replies_over_pipe():
    """Loop behavior end to end: init messages arrive (Recovery, SymbolInfo,
    Status — same order as the MT5 worker), and a SymbolInfoRequestMsg gets a
    SymbolInfoMsg naming every requested symbol."""
    import multiprocessing
    import threading
    from manager.ipc.messages import (RecoveryMsg, SymbolInfoMsg, StatusMsg,
                                      SymbolInfoRequestMsg)
    from manager.ipc.pipe_framing import send_msg, recv_msg
    from manager.worker.ib.worker import _slave_loop

    gw = make_gw(); gw.initialize("h", 1, 1)
    cfg = _cfg()

    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop,
                         args=(child, gw, cfg), daemon=True)
    t.start()
    try:
        init = [recv_msg(parent) for _ in range(3)]
        assert isinstance(init[0], RecoveryMsg)
        assert isinstance(init[1], SymbolInfoMsg)
        assert isinstance(init[2], StatusMsg)

        send_msg(parent, SymbolInfoRequestMsg(source_id="ib1",
                                              symbols=["YM", "NQ"]))
        replies = []
        while len(replies) < 1:
            msg = recv_msg(parent)
            if isinstance(msg, SymbolInfoMsg):
                replies.append(msg)
        si = replies[0]
        assert set(si.infos).issuperset({"YM"})     # NQ unknown: named-only
        assert si.requested == ["YM", "NQ"]
    finally:
        parent.close()  # -> worker reads EOFError -> graceful return
        t.join(timeout=2.0)
        assert not t.is_alive(), "slave loop must exit when the pipe closes"
```

(Exactly the `test_mt5_worker.py` harness idiom, lines 295–405.) The status-loop branch (`connected=_connected(gw)`) is covered through the supervisor in Task 8 — don't duplicate it here.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_ib_worker.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `manager/worker/ib/worker.py`:

```python
# manager/worker/ib/worker.py
from __future__ import annotations

import math
import time
import traceback

from manager.engine.models import Record, BUY, SELL, SymbolInfo
from manager.engine.linkage import decode_comment, magic_for
from manager.engine.transform import (normalize_sltp as _normalize_sltp,
                                      round_to_tick, SIZING_BALANCE_STEP)
from manager.ipc.messages import (AckMsg, ErrorMsg, StatusMsg, SymbolInfoMsg,
                                   RecoveryMsg, ReconfigureMsg,
                                   SymbolInfoRequestMsg)
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.ib.adapter import FakeIbGateway, RealIbGateway
from manager.worker.ib.contracts import parse_contract_map
from manager.worker.ib.tags import synthetic_ticket

UNIT_VOLUME_STEP = 0.01
UNIT_VOLUME_MIN = 0.01
UNIT_VOLUME_MAX = 100_000.0
DEFAULT_ROLL_DAYS = 5
DEFAULT_ACK_TIMEOUT_MS = 15_000


def _ratio(sizing_mode: str, spec, resolved: dict) -> float:
    """Engine units -> contracts. balance_step units ARE contracts; the other
    two modes carry master CFD lots, converted by the point-value ratio."""
    if sizing_mode == SIZING_BALANCE_STEP:
        return 1.0
    mult = float(resolved.get("multiplier", 1.0)) or 1.0
    return spec.master_point_value / mult


def _contracts_for(units: float, ratio: float) -> int:
    # +1e-9: the repo's epsilon grid guard (0.28/0.01 bug class)
    return int(math.floor(units * ratio + 1e-9))


def _digits_of(tick: float) -> int:
    n, x = 0, round(float(tick), 8)
    while abs(x - round(x)) > 1e-9 and n < 8:
        x *= 10.0
        n += 1
    return n


def _resolve(gw, spec, roll_days: int, today: str = "") -> dict | None:
    return gw.resolve_contract(spec.symbol, today or time.strftime("%Y%m%d"),
                               exchange=spec.exchange, sec_type=spec.sec_type,
                               roll_days=roll_days)


def _symbol_info_for(gw, spec, today: str, roll_days: int) -> SymbolInfo | None:
    res = gw.resolve_contract(spec.symbol, today, exchange=spec.exchange,
                              sec_type=spec.sec_type, roll_days=roll_days)
    if res is None:
        return None
    return SymbolInfo(point=1.0, digits=_digits_of(res["tick_size"]),
                      tick_size=res["tick_size"],
                      volume_step=UNIT_VOLUME_STEP,
                      volume_min=UNIT_VOLUME_MIN,
                      volume_max=UNIT_VOLUME_MAX)


def build_symbol_info_msg(gw, slave_id: str, contracts: dict,
                          roll_days: int,
                          requested: list[str] | None = None) -> SymbolInfoMsg:
    """`requested=None`: the startup bulk report for every mapped symbol
    that resolves. Otherwise a SymbolInfoRequestMsg reply covering ONLY the
    requested symbols — the reply names every requested symbol; a symbol
    absent from `infos` is confirmed missing (mt5_worker's
    build_symbol_info_reply semantics)."""
    wanted = list(requested) if requested is not None else list(contracts)
    infos = {}
    for symbol in wanted:
        spec = contracts.get(symbol)
        if spec is not None:
            info = _symbol_info_for(gw, spec, time.strftime("%Y%m%d"),
                                    roll_days)
            if info is not None:
                infos[symbol] = info
    return SymbolInfoMsg(source_id=slave_id, infos=infos,
                         requested=list(requested) if requested else [])


def build_recovery_records(gw) -> list[Record]:
    """Rebuild linkage records from this slave's tagged orders (the tag IS the
    engine's CPY comment). One record per master ticket, anchored on its
    parent MKT order — `Record` is frozen, so the synthetic ticket is derived
    in the same pass rather than patched onto a built record."""
    out: list[Record] = []
    seen: set[int] = set()
    for o in gw.tagged_orders():
        dec = decode_comment(o.tag)
        if dec is None or o.order_type != "MKT":
            continue           # record anchors on its parent MKT order only
        master_ticket, mv, sv = dec
        if master_ticket in seen or mv is None or sv is None:
            continue
        seen.add(master_ticket)
        side = BUY if o.action == "BUY" else SELL
        out.append(Record(master_ticket=master_ticket,
                          magic=magic_for(master_ticket),
                          slave_ticket=synthetic_ticket(o.symbol, side),
                          master_open_volume=mv,
                          slave_open_volume=sv))
    return out


def _holdings(gw, tag: str, ratio: float) -> int:
    dec = decode_comment(tag)
    share = _contracts_for(float(dec[2] or 0.0) if dec else 0.0, ratio)
    closed = gw.closed_per_tag(tag)
    return max(0, math.floor(share - closed + 1e-9))


def _current_qty(gw, symbol: str, side: int) -> float:
    for p in gw.net_positions():
        if p.symbol == symbol and p.side == side:
            return p.qty
    return 0.0


def _find_symbol(gw, slave_ticket: int):
    for p in gw.net_positions():
        if synthetic_ticket(p.symbol, p.side) == slave_ticket:
            return p.symbol, p.side, p.open_price
    return None


def _find_tag(gw, master_ticket: int) -> str | None:
    for o in gw.tagged_orders():
        dec = decode_comment(o.tag)
        if dec and dec[0] == master_ticket:
            return o.tag
    return None


def _child_prices(gw, tag: str) -> tuple[float, float]:
    sl = tp = 0.0
    for o in gw.tagged_orders():
        if o.tag == tag and not o.filled:
            if o.order_type == "STP":
                sl = o.stop_price
            elif o.order_type == "LMT":
                tp = o.limit_price
    return sl, tp


def _record_month(gw, tag: str) -> str:
    """The contract month the record's parent order went to ("" = unknown,
    e.g. no surviving order object after a Gateway restart)."""
    for o in gw.tagged_orders():
        if o.tag == tag and o.order_type == "MKT" and o.month:
            return o.month
    return ""


def _position_month(gw, symbol: str, side: int) -> str:
    for p in gw.net_positions():
        if p.symbol == symbol and p.side == side:
            return p.month
    return ""


def _target_month(gw, tag: str, symbol: str, side: int) -> str:
    """Rollover rule (spec §5): an existing position keeps its contract.
    Prefer its own tagged parent order's month; fall back to the live net
    position's month; only a brand-new OPEN uses a freshly resolved month."""
    return _record_month(gw, tag) or _position_month(gw, symbol, side)


def _all_tags(gw, symbol: str) -> set[str]:
    return {o.tag for o in gw.tagged_orders() if o.symbol == symbol}


def _connected(gw) -> bool:
    """FakeIbGateway.connected vs RealIbGateway.ib.isConnected() under one
    read (both gateways are duck-typed; never hasattr on the worker side)."""
    ib = getattr(gw, "ib", None)
    if ib is not None:
        try:
            return bool(ib.isConnected())
        except Exception:
            return False
    return bool(getattr(gw, "connected", False))


def _sync_protection_after_reduce(gw, symbol: str, ratio: float,
                                  timeout_s: float) -> None:
    """Re-run after every reduce (CLOSE/PARTIAL): each record's SL/TP
    children must be sized to its own remaining share (netting model). A
    share that is fully retired gets its children cancelled — closing one
    record of several netted ones must leave no stale stop firing past the
    survivors (REVIEW FOCUS #1). set_protective(sl=0, tp=0, qty=0) is the
    cancel-only path in both gateway implementations."""
    for tag in sorted(_all_tags(gw, symbol)):
        dec = decode_comment(tag)
        if dec is None:
            continue
        orders = [o for o in gw.tagged_orders() if o.tag == tag]
        tside = next((BUY if o.action == "BUY" else SELL
                      for o in orders if o.order_type == "MKT"), None)
        if tside is None:
            continue
        hold = _holdings(gw, tag, ratio)
        sl, tp = _child_prices(gw, tag)
        if hold <= 0:
            sl = tp = 0.0                    # share fully closed: cancel only
        gw.set_protective(symbol, tside, tag, sl, tp, float(hold),
                          month=_record_month(gw, tag) or "",
                          timeout_s=timeout_s)
```

SL/TP preparation (mirrors mt5_worker's order of operations — normalize
against the slave fill first, then round to the contract's tick):

```python
def _prep_sltp(cmd, slave_open: float, normalize: bool) -> tuple[float, float]:
    if normalize:
        return _normalize_sltp(cmd.master_open_price, cmd.sl, cmd.tp,
                               slave_open, cmd.side)
    return cmd.sl, cmd.tp


def _round_sltp(sl: float, tp: float,
                tick_size: float) -> tuple[float, float] | None:
    digits = _digits_of(tick_size)
    rsl = round_to_tick(sl, tick_size, digits)
    rtp = round_to_tick(tp, tick_size, digits)
    if rsl is None or rtp is None:
        return None
    return rsl, rtp


def execute_command(gw, cmd, contracts, normalize_sltp: bool,
                    sizing_mode: str, roll_days: int, timeout_ms: int,
                    today: str = "") -> AckMsg:
    """One CommandMsg -> one AckMsg. `today` is injectable for tests
    (rollover decisions are time-dependent); production passes "" (clock)."""
    def _fail(retcode: int, err: str) -> AckMsg:
        return AckMsg(slave_id=cmd.slave_id, action=cmd.action,
                      master_ticket=cmd.master_ticket, ok=False,
                      retcode=retcode, error=err)

    timeout_s = max(1.0, timeout_ms / 1000.0)

    if cmd.action == "OPEN":
        spec = contracts.get(cmd.symbol)
        if spec is None:
            return _fail(-1, f"no contract spec for {cmd.symbol}")
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error()
                         or f"cannot resolve contract for {cmd.symbol}")
        ratio = _ratio(sizing_mode, spec, res)
        qty = _contracts_for(cmd.volume, ratio)
        if qty <= 0:
            # logged skip: visible as a failed ack, retcode 0 (not an error
            # class), never silent — Global Constraints
            return _fail(0, f"skipped: {cmd.volume:g} units -> 0 contracts "
                            f"(ratio {ratio:.4g})")
        margin_est = qty * float(res.get("margin_est", 0.0))
        acc = gw.account()
        avail = float(acc.get("margin_available", 0.0))
        if margin_est > 0.0 and margin_est > avail * 0.9:
            return _fail(0, f"insufficient margin: est {margin_est:.0f} > "
                            f"available {avail:.0f}")
        tick = gw.tick(cmd.symbol)
        if tick is None:
            return _fail(-1, gw.last_error() or f"no tick for {cmd.symbol}")
        bid, ask = tick
        slave_open = ask if cmd.side == BUY else bid
        sl, tp = _prep_sltp(cmd, slave_open, normalize_sltp)
        rsltp = _round_sltp(sl, tp, res["tick_size"])
        if rsltp is None:
            return _fail(-1, "SL/TP normalization/tick rounding failed")
        rsl, rtp = rsltp
        ok, fill_px, err = gw.open_bracket(cmd.symbol, cmd.side, qty,
                                           rsl, rtp, cmd.comment,
                                           month=res["month"],
                                           timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or gw.last_error() or "bracket rejected")
        return AckMsg(slave_id=cmd.slave_id, action="OPEN",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=synthetic_ticket(cmd.symbol, cmd.side),
                      fill_price=fill_px, fill_volume=cmd.volume,
                      remaining_volume=cmd.volume, retcode=0)

    if cmd.action == "MODIFY":
        found = _find_symbol(gw, cmd.slave_ticket)
        if found is None:
            return _fail(-1, f"position for #{cmd.slave_ticket} not found")
        symbol, side, open_price = found
        spec = contracts.get(symbol)
        if spec is None:
            return _fail(-1, f"no contract spec for {symbol}")
        tag = _find_tag(gw, cmd.master_ticket)
        if tag is None:
            return _fail(-1, f"tag not found for master #{cmd.master_ticket}")
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error() or f"cannot resolve {symbol}")
        sl, tp = _prep_sltp(cmd, open_price, normalize_sltp)
        rsltp = _round_sltp(sl, tp, res["tick_size"])
        if rsltp is None:
            return _fail(-1, "SL/TP normalization/tick rounding failed")
        rsl, rtp = rsltp
        ratio = _ratio(sizing_mode, spec, res)
        hold = _holdings(gw, tag, ratio)
        ok, err = gw.set_protective(symbol, side, tag, rsl, rtp,
                                    float(hold),
                                    month=_target_month(gw, tag, symbol, side),
                                    timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or "children not replaced")
        return AckMsg(slave_id=cmd.slave_id, action="MODIFY",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, retcode=0)

    if cmd.action == "PARTIAL_CLOSE":
        found = _find_symbol(gw, cmd.slave_ticket)
        if found is None:
            return _fail(-1, f"position for #{cmd.slave_ticket} not found")
        symbol, side, _open_price = found
        spec = contracts.get(symbol)
        tag = _find_tag(gw, cmd.master_ticket)
        if spec is None or tag is None:
            return _fail(-1, "contract spec or tag missing")
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error() or f"cannot resolve {symbol}")
        if cmd.master_open_volume <= 0.0:
            return _fail(-1, "cannot compute partial fraction")
        ratio = _ratio(sizing_mode, spec, res)
        share = _contracts_for(
            float(decode_comment(tag)[2] or 0.0), ratio)
        closed = gw.closed_per_tag(tag)
        holdings = max(0, math.floor(share - closed + 1e-9))
        fraction = cmd.new_master_volume / cmd.master_open_volume
        target = math.floor(share * fraction + 1e-9)
        current = _current_qty(gw, symbol, side)
        by = min(holdings - target, current)
        by = max(0, int(math.floor(by + 1e-9)))
        if by <= 0:
            return AckMsg(slave_id=cmd.slave_id, action="PARTIAL_CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket,
                          remaining_volume=float(holdings), retcode=0)
        month = _target_month(gw, tag, symbol, side)
        ok, filled, err = gw.reduce(symbol, side, by, tag, close=False,
                                    month=month, timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or "reduce failed")
        # REVIEW-FOCUS #2: resize children to the new remaining share so a
        # stale stop cannot fire past the holding (reverse-trade hazard)
        new_qty = max(0, holdings - filled)
        sl, tp = _child_prices(gw, tag)
        gw.set_protective(symbol, side, tag, sl, tp, float(new_qty),
                          month=month, timeout_s=timeout_s)
        return AckMsg(slave_id=cmd.slave_id, action="PARTIAL_CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket,
                      fill_volume=float(filled / (ratio if ratio else 1.0)),
                      remaining_volume=float(new_qty), retcode=0)

    if cmd.action == "CLOSE":
        found = _find_symbol(gw, cmd.slave_ticket)
        if found is None:
            # race tolerance — already flat (bracket fired): retire the record
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket, retcode=0)
        symbol, side, _open_price = found
        spec = contracts.get(symbol)
        tag = _find_tag(gw, cmd.master_ticket)
        if spec is None or tag is None:
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket, retcode=0)
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error() or f"cannot resolve {symbol}")
        ratio = _ratio(sizing_mode, spec, res)
        holdings = _holdings(gw, tag, ratio)
        current = _current_qty(gw, symbol, side)
        by = min(holdings, current)
        if by <= 0:
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket,
                          remaining_volume=0.0, retcode=0)
        month = _target_month(gw, tag, symbol, side)
        ok, filled, err = gw.reduce(symbol, side, by, tag, close=True,
                                    month=month, timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or "close failed")
        # Sibling protection sync: the net may still hold other records'
        # shares — retire this record's stale children (already cancelled by
        # the gateway when the net went flat) and re-size the survivors'
        # children to their own holdings.
        _sync_protection_after_reduce(gw, symbol, ratio, timeout_s)
        return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket,
                      remaining_volume=0.0, retcode=0)

    return _fail(-1, f"unknown action {cmd.action!r}")
```

Then the loop half of the same file (status + init + message loop — mirrors `mt5_worker._slave_loop` plus the IB bits):

```python
def _status(gw, source_id: str, connected: bool, front: dict) -> StatusMsg:
    acc = gw.account()
    detail = "; ".join(
        f"{sym} {r['month']}{' rolling' if r['rolling'] else ''}"
        for sym, r in sorted(front.items()))
    return StatusMsg(source_id=source_id, role="slave", connected=connected,
                     login=0, balance=float(acc.get("balance", 0.0)),
                     equity=float(acc.get("equity", 0.0)),
                     currency=str(acc.get("currency", "USD")),
                     server="ib-gateway",
                     trade_allowed=connected and not gw.read_only(),
                     detail=detail)


def slave_init(gw, config: dict, contracts: dict, roll_days: int):
    slave_id = config["slave_id"]
    front = {s: r for s, spec in contracts.items()
             if (r := gw.resolve_contract(
                 spec.symbol, time.strftime("%Y%m%d"),
                 exchange=spec.exchange, sec_type=spec.sec_type,
                 roll_days=roll_days)) is not None}
    rec_msg = RecoveryMsg(source_id=slave_id,
                          records=tuple(build_recovery_records(gw)))
    si_msg = build_symbol_info_msg(gw, slave_id, contracts, roll_days)
    st_msg = _status(gw, slave_id, connected=True, front=front)
    return rec_msg, si_msg, st_msg, front


def _slave_loop(pipe, gw, config):
    slave_id = config["slave_id"]
    roll_days = int(config.get("roll_days", DEFAULT_ROLL_DAYS))
    timeout_ms = int(config.get("ack_timeout_ms", DEFAULT_ACK_TIMEOUT_MS))
    contracts = parse_contract_map(config.get("contract_map"))
    normalize = bool(config.get("normalize_sltp", True))
    sizing_mode = str(config.get("sizing_mode", "balance_step"))
    rec_msg, si_msg, st_msg, front = slave_init(gw, config, contracts,
                                                roll_days)
    send_msg(pipe, rec_msg); send_msg(pipe, si_msg); send_msg(pipe, st_msg)
    status_interval = float(config.get("slave_status_interval_ms", 5000)) / 1000.0
    last_status = time.time()
    poll_timeout = min(1.0, status_interval)
    while True:
        if pipe.poll(poll_timeout):
            msg = recv_msg(pipe)  # raises EOFError on manager close
            if isinstance(msg, ReconfigureMsg):
                normalize = msg.normalize_sltp
                if msg.contracts:
                    contracts = parse_contract_map(msg.contracts)
                    front = {s: r for s, spec in contracts.items()
                             if (r := gw.resolve_contract(
                                 spec.symbol, time.strftime("%Y%m%d"),
                                 exchange=spec.exchange, sec_type=spec.sec_type,
                                 roll_days=roll_days)) is not None}
                send_msg(pipe, build_symbol_info_msg(gw, slave_id, contracts,
                                                     roll_days))
                last_status = time.time()
                continue
            if isinstance(msg, SymbolInfoRequestMsg):
                send_msg(pipe, build_symbol_info_msg(gw, slave_id, contracts,
                                                     roll_days,
                                                     requested=list(msg.symbols)))
                last_status = time.time()
                continue
            ack = execute_command(gw, msg, contracts, normalize, sizing_mode,
                                  roll_days, timeout_ms)
            try:
                send_msg(pipe, ack)
                send_msg(pipe, _status(gw, slave_id, connected=_connected(gw),
                                       front))
            except (EOFError, OSError):
                return  # manager gone
            last_status = time.time()
        elif time.time() - last_status >= status_interval:
            try:
                send_msg(pipe, _status(gw, slave_id, connected=_connected(gw),
                                       front=front))
            except (EOFError, OSError):
                return
            last_status = time.time()


def worker_main(pipe, role: str, adapter_kind: str = "real", fake_state=None):
    """Subprocess entry (same signature as mt5_worker.worker_main). Reads the
    StartMsg config, connects to the IB Gateway socket, runs the slave loop."""
    try:
        start = recv_msg(pipe)
    except EOFError:
        return
    config = start.config
    if role == "master":
        try:
            send_msg(pipe, ErrorMsg(source_id="master",
                   message="IB accounts cannot be the master", fatal=True))
        except (EOFError, OSError):
            pass
        return
    if adapter_kind == "fake":
        gw = FakeIbGateway(**(fake_state or {}))
    else:
        gw = RealIbGateway()
    ok = gw.initialize(config.get("ib_host", "127.0.0.1"),
                       int(config.get("ib_port", 4002)),
                       int(config.get("ib_client_id", 7)))
    if not ok:
        try:
            send_msg(pipe, ErrorMsg(source_id=config["slave_id"],
                   message=f"IB Gateway connect failed: {gw.last_error()}",
                   fatal=True))
        except (EOFError, OSError):
            pass
        return
    try:
        _slave_loop(pipe, gw, config)
    except ValueError as exc:      # bad contract map: fatal, user-fixable
        try:
            send_msg(pipe, ErrorMsg(source_id=config["slave_id"],
                   message=f"bad contract map: {exc}", fatal=True))
        except (EOFError, OSError):
            pass
    except EOFError:
        pass
    except Exception as exc:
        try:
            send_msg(pipe, ErrorMsg(source_id=config["slave_id"],
                   message=f"worker crashed: {exc}\n{traceback.format_exc()}",
                   fatal=True))
        except (EOFError, OSError):
            pass
    finally:
        try:
            gw.shutdown()
        except Exception:
            pass
```

Clean up while writing: every helper above is the final form — there is no sketch to replace. `_sync_protection_after_reduce` runs after a successful CLOSE reduce only; the PARTIAL path re-sizes its own tag's children inline (its share shrink is the record's own). `_connected` covers both gateway implementations from one place. `execute_command`'s `today` parameter must be plumbed through `_resolve` at every call site inside `execute_command`; the startup bulk report (`slave_init`/reconfigure) keeps the real clock — a resolve that rolls during a reconfigure is correct there (new bulk infos describe the new front month).

- [ ] **Step 4: Run tests**

Run: `python -m pytest manager/tests/test_ib_worker.py -v`
Expected: PASS. If `test_open_below_one_contract_is_a_logged_skip` fails with ok=True, re-read Global Constraints — the skip is a failed ack with retcode 0, not a silent success.

- [ ] **Step 5: Run the MT5-path regression**

Run: `python -m pytest manager/tests -v --tb=short`
Expected: PASS — nothing outside the new files changed.

- [ ] **Step 6: Commit**

```bash
git add manager/worker/ib/worker.py manager/tests/test_ib_worker.py
git commit -m "feat(ib): ib_worker command execution + recovery + loops"
```

---

### Task 8: Supervisor dispatch on `platform` + reconfigure contracts

**Files:**
- Modify: `manager/supervisor.py:140-149` (`_spawn`)
- Modify: `manager/supervisor.py:299-315` (`reconfigure_slave`)
- Modify: `manager/app/controller.py` (`apply_slave_edit`, one line)
- Test: `manager/tests/test_supervisor.py` (append)

**Interfaces:**
- Consumes: `config["platform"] == "ib"` (Task 2), `ReconfigureMsg.contracts` (Task 1), the worker's reconfigure contracts handling (Task 7, line ~2297: `contracts = parse_contract_map(msg.contracts)`).
- Produces: `reconfigure_slave(slave_id, symbol_map_csv, normalize_sltp, contracts: dict | None = None)` — truthy `contracts` stored into `h.config["contract_map"]` and sent as `ReconfigureMsg.contracts`; `None` (every MT5 call) sends `contracts={}` and touches nothing. The supervisor now also spawns the right worker process for IB slaves; IB `fake_state` is a kwargs dict for `FakeIbGateway` (adapter_kind stays `"fake"` / `"real"` exactly as for MT5).

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_supervisor.py`. The file's harness: real `Supervisor(CopyEngine())` with `_tick_until` spawning genuine worker subprocesses over pipes with `adapter_kind="fake"` — mirror `test_end_to_end_open_through_subprocesses` exactly (module-top helpers `_engine()`, `_slave_cfg()`, `_tick_until`):

```python
def test_spawn_ib_slave_uses_ib_worker():
    """An IB slave's config platform selects the ib worker entry; end-to-end
    through real subprocesses: spawn -> recovery/symbol-info/status arrive ->
    the slave is ready and trade-allowed (the fake IB gateway reads fine)."""
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="ib1", symbol_map_csv="US30=YM",
                              step_amount=1000.0, step_size=1.0, max_lot=100.0,
                              max_trade_age_minutes=999999,
                              normalize_sltp=True))
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=30,
                     consecutive_failures=3, poll_timeout=0.02)
    cfg = {"slave_id": "ib1", "platform": "ib",
           "contract_map": {"YM": {"exchange": "CME", "sec_type": "FUT",
                                    "master_point_value": 1.0}},
           "symbol_map_csv": "US30=YM", "normalize_sltp": True,
           "sizing_mode": "balance_step", "roll_days": 5,
           "ack_timeout_ms": 1500, "slave_status_interval_ms": 200}
    sup.spawn_slave("ib1", cfg, adapter_kind="fake",
                    fake_state={"prices": {"YM": (45_000.0, 45_001.0)},
                                "months": {"YM": ["202612"]}})
    try:
        assert _tick_until(sup, lambda: sup.slave_ready("ib1")), \
            "IB slave never became ready (worker/config/plumbing broken)"
        assert sup.slave_trade_allowed("ib1")  # fake gateway: not read-only
    finally:
        sup.shutdown()
```

(The config deliberately carries no `terminal_path` key — the same KeyError-adjacent hazard the Task 2 preflight fix removes.)

If the file's harness passes fake_state into the worker differently (check how `spawn_slave`'s fake path constructs `FakeMt5`), adapt the call, not the assertion names.

Also append a reconfigure-contracts test (directly exercising the existing `reconfigure_slave` — it currently has no `contracts` parameter; the test fails at the TypeError, which is the intended red):

```python
def test_reconfigure_ib_slave_sends_contracts():
    """Editing a running IB slave must forward its contract map so the
    worker re-parses and re-reports contract state (worker side: Task 7)."""
    import multiprocessing
    from manager.ipc.messages import ReconfigureMsg
    from manager.ipc.pipe_framing import send_msg, recv_msg
    from manager.supervisor import WorkerHandle
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="ib1", symbol_map_csv="US30=YM",
                              step_amount=1000.0, step_size=1.0, max_lot=100.0,
                              max_trade_age_minutes=999999,
                              normalize_sltp=True))
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=30,
                     consecutive_failures=3, poll_timeout=0.02)
    parent, child = multiprocessing.Pipe(duplex=True)
    sup._handles["ib1"] = WorkerHandle(
        name="ib1", role="slave", proc=None, pipe=child,
        config={"platform": "ib"}, adapter_kind="", fake_state=None)
    try:
        contracts = {"YM": {"exchange": "CME", "sec_type": "FUT",
                             "master_point_value": 2.0}}
        sup.reconfigure_slave("ib1", "US30=YM", True, contracts=contracts)
        msg = recv_msg(parent)
        assert isinstance(msg, ReconfigureMsg) and msg.contracts == contracts
        # stored for the respawn path (a restarted worker re-reads its config)
        assert sup._handles["ib1"].config["contract_map"] == contracts

        # MT5 path unchanged: contracts=None (the default) sends {} and does
        # not inject contract_map into the config
        sup.reconfigure_slave("ib1", "US30=YM", True)
        msg2 = recv_msg(parent)
        assert isinstance(msg2, ReconfigureMsg) and msg2.contracts == {}
    finally:
        sup.shutdown()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_supervisor.py -k ib -v`
Expected: FAIL — the end-to-end test: the fake's `platform` config reaches `mt5_worker.worker_main`, which calls `adapter.initialize(config["terminal_path"], ...)` with a missing key (`KeyError`) → fatal error / not ready. The reconfigure test: `TypeError ... unexpected keyword argument 'contracts'`.

- [ ] **Step 3: Implement**

In `manager/supervisor.py`, add a method and use it in `_spawn`:

```python
    def _worker_target(self, config):
        """Pick the worker entry point from the config's platform. Master is
        always MT5 (spec non-goal); a missing platform key = mt5 (old configs
        byte-identical). ib_async stays unimported until an IB slave spawns."""
        if str(config.get("platform", "mt5")).lower() == "ib":
            from manager.worker.ib.worker import worker_main as ib_worker_main
            return ib_worker_main
        return worker_main
```

and in `_spawn`, replace the fixed target line:

```python
        proc = multiprocessing.Process(target=self._worker_target(config),
            args=(child_pipe, role, adapter_kind, fake_state), daemon=True)
```

Everything else in `_spawn` (StartMsg, handle fields) is untouched. `_worker_target(config)` needs no early import of the ib package: `from manager.worker.ib.worker import ...` inside the method is the lazy-import guard (Global Constraints). Note: `RealIbGateway`'s own ib_async import is inside its `__init__`/methods (Task 6), so spawning an MT5-only session never touches it.

2. Extend `reconfigure_slave` (read its current body at `manager/supervisor.py:299-315` first — add the parameter and two contract touches; everything else stays character-for-character):

```python
    def reconfigure_slave(self, slave_id: str, symbol_map_csv: str,
                          normalize_sltp: bool,
                          contracts: dict | None = None) -> None:
```

With, after the existing handle/config update and before the send: if `contracts` is truthy, `h.config["contract_map"] = contracts`; and the existing `ReconfigureMsg(...)` construction gains `contracts=contracts or {}`. The no-handle no-op and the closed-pipe swallow behavior are exactly as today — MT5 path unchanged (`None` → sends `contracts={}`, no config injection; `manager/worker/mt5_worker.py`'s reconfigure branch does not read the field).

3. In `manager/app/controller.py`'s `apply_slave_edit` (line ~276), the final reconfigure call becomes:

```python
        self._supervisor.reconfigure_slave(
            slave_id, spec.symbol_map_csv, spec.normalize_sltp,
            contracts=(spec.contract_map if spec.platform == "ib" else None))
```

No new controller test is needed: the existing `apply_slave_edit` tests already pin the MT5 call — if any asserts the exact reconfigure call tuple, adapt only the assertion expectation (the calls now carry `contracts=None` for MT5 specs, which produces the identical behavior).

- [ ] **Step 4: Run tests**

Run: `python -m pytest manager/tests/test_supervisor.py manager/tests/test_supervisor_readiness.py -v`
Expected: PASS (new + pre-existing).

- [ ] **Step 5: Commit**

```bash
git add manager/supervisor.py manager/app/controller.py manager/tests/test_supervisor.py
git commit -m "feat(supervisor): spawn ib_worker for platform=ib; reconfigure sends contracts"
```

---

### Task 9: GUI — platform picker + contract table in the slave editor

**Files:**
- Modify: `manager/gui/slave_editor.py`
- Test: `manager/tests/test_slave_editor.py` (append; runs in the app venv — headless runs skip)

**Interfaces:**
- Consumes: `AccountSpec` new fields (Task 2).
- Produces: IB-mode `AccountSpec`s with `platform="ib"`, `contract_map={symbol: {"exchange", "sec_type", "master_point_value", "currency":"USD"}}`, `symbol_map_csv` composed from the contract table's Master + Symbol columns, `terminal_path=None`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_slave_editor.py` (reuse its `qapp` fixture + editor-construction helpers):

```python
def test_editor_platform_mt5_default(qapp):
    dlg = SlaveEditor(controller=fake_controller())
    assert dlg.platform.currentData() == "mt5"
    assert not dlg.terminal.isHidden()


def test_editor_ib_mode_hides_terminal_shows_ib_fields(qapp):
    dlg = SlaveEditor(controller=fake_controller())
    dlg.platform.setCurrentIndex(1)             # IB
    assert dlg.terminal.isHidden()
    assert dlg.launch_terminal_button.isHidden()
    assert not dlg.ib_host.isHidden() and not dlg.ib_port.isHidden()
    # header labels switch to the contract columns
    assert dlg.symbol_table.columnCount() == 5
    assert dlg.symbol_table.horizontalHeaderItem(4).text() == "Master $/pt"


def test_editor_ib_spec_roundtrip(qapp):
    dlg = SlaveEditor(controller=fake_controller())
    dlg.platform.setCurrentIndex(1)
    dlg.id_edit.setText("ib1")
    dlg.symbol_table.setRowCount(1)
    for col, text in enumerate(["US30", "YM", "CME", "FUT", "1.0"]):
        dlg.symbol_table.setItem(0, col, QTableWidgetItem(text))
    dlg.ib_port.setText("4002")
    dlg.sizing_mode.setCurrentIndex(1)          # copy_master
    dlg._update_sizing_visibility()
    dlg.accept()                                 # sets result() == Accepted; spec() reads it
    spec = dlg.spec()
    assert spec.platform == "ib" and spec.terminal_path is None
    assert spec.symbol_map_csv == "US30=YM"
    assert spec.contract_map == {"YM": {"exchange": "CME", "sec_type": "FUT",
                                         "master_point_value": 1.0,
                                         "currency": "USD"}}
    assert spec.sizing_mode == "copy_master"


def test_editor_edit_existing_ib_spec(qapp):
    dlg = SlaveEditor(controller=fake_controller())
    spec = AccountSpec(id="ib1", platform="ib", terminal_path=None,
                       symbol_map_csv="US30=YM",
                       contract_map={"YM": {"exchange": "CME", "sec_type": "FUT",
                                             "master_point_value": 1.0}})
    dlg.set_spec(spec, lock_identity=True)
    assert dlg.platform.currentData() == "ib"
    assert dlg.symbol_table.columnCount() == 5
    assert dlg.symbol_table.item(0, 1).text() == "YM"
    assert dlg.ib_port.text() == str(spec.ib_port)
```

Use the file's existing `fake_controller()` name — read the file first for its actual helper names and mirror them.

- [ ] **Step 2: Run to verify failure**

Run (app venv): `python -m pytest manager/tests/test_slave_editor.py -v`
Expected: new tests FAIL (`SlaveEditor` has no `platform` widget).

- [ ] **Step 3: Implement**

In `manager/gui/slave_editor.py`:

1. Add a platform combo + IB fields to `_build_ui` (before the terminal row):

```python
        self.platform = QComboBox()
        self.platform.addItem("MetaTrader terminal", "mt5")
        self.platform.addItem("IB Gateway (TWS API)", "ib")
        form.addRow("Slave platform", self.platform)
```

and in the form, after the terminal row:

```python
        self.ib_host = QLineEdit("127.0.0.1")
        self.ib_port = QLineEdit("4002")
        self.ib_client_id = QLineEdit("7")
        form.addRow("IB host", self.ib_host)
        form.addRow("IB port", self.ib_port)
        form.addRow("IB client id", self.ib_client_id)
```

2. Platform visibility + column switch:

```python
    _MT5_HEADERS = ["Master symbol (regex)", "Slave symbol"]
    _IB_HEADERS = ["Master symbol (regex)", "Symbol", "Exchange", "SecType",
                   "Master $/pt"]

    def _on_platform_changed(self, *_args) -> None:
        ib = self.platform.currentData() == "ib"
        self.terminal.setVisible(not ib)
        self.launch_terminal_button.setVisible(not ib)
        self.ib_host.setVisible(ib)
        self.ib_port.setVisible(ib)
        self.ib_client_id.setVisible(ib)
        headers = self._IB_HEADERS if ib else self._MT5_HEADERS
        self.symbol_table.setColumnCount(5 if ib else 2)
        self.symbol_table.setHorizontalHeaderLabels(headers)
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
```

Connect it: `self.platform.currentIndexChanged.connect(self._on_platform_changed)` after `_build_ui` builds the table; call `self._on_platform_changed()` once at construction (after `_populate_terminals`).

3. `_symbol_map_csv()` stays as-is for MT5 (columns 0/1). New helper:

```python
    def _contract_map(self) -> dict:
        out: dict = {}
        for r in range(self.symbol_table.rowCount()):
            def cell(r, c):
                item = self.symbol_table.item(r, c)
                return item.text().strip() if item is not None else ""
            m, sym, exch, sec, mpv = (cell(r, 0), cell(r, 1), cell(r, 2),
                                       cell(r, 3), cell(r, 4))
            if not (m and sym and exch and sec and mpv):
                continue
            out[sym] = {"exchange": exch.upper(), "sec_type": sec.upper(),
                        "master_point_value": float(mpv),
                        "currency": "USD"}
        return out
```

4. `spec()` — IB branch (MT5 branch unchanged):

```python
    def spec(self) -> AccountSpec | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        platform = self.platform.currentData()
        if platform == "ib":
            return AccountSpec(
                id=self.id_edit.text().strip() or "s1", terminal_path=None,
                symbol_map_csv=self._symbol_map_csv(),
                step_amount=float(self.step_amount.text()),
                step_size=float(self.step_size.text()),
                max_lot=float(self.max_lot.text()),
                max_trade_age_minutes=float(self.max_trade_age_minutes.text()),
                normalize_sltp=self.normalize_sltp.isChecked(),
                sizing_mode=self.sizing_mode.currentData(),
                master_base_lot=float(self.master_base_lot.text()),
                fixed_lot=float(self.fixed_lot.text()),
                platform="ib",
                ib_host=self.ib_host.text().strip() or "127.0.0.1",
                ib_port=int(self.ib_port.text() or "4002"),
                ib_client_id=int(self.ib_client_id.text() or "7"),
                contract_map=self._contract_map())
        return self._spec_from_fields(  # existing MT5 path unchanged
            ...)
```

Guard `float(mpv)` etc. with the dialog's existing float-parse tolerance (check how `_spec_from_fields` handles bad input today — a `ValueError` here should surface as a modal message box, or reuse whatever the file already does; do not invent a new mechanism).

5. `set_spec` — populate the platform + IB fields:

```python
        idx = self.platform.findData(spec.platform or "mt5")
        self.platform.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_platform_changed()
        if spec.platform == "ib":
            self.ib_host.setText(spec.ib_host)
            self.ib_port.setText(str(spec.ib_port))
            self.ib_client_id.setText(str(spec.ib_client_id))
            self.symbol_table.setRowCount(0)
            for master, slave in parse_symbol_map(spec.symbol_map_csv).items():
                d = (spec.contract_map or {}).get(slave, {})
                r = self.symbol_table.rowCount()
                self.symbol_table.insertRow(r)
                for col, text in enumerate(
                        [master, slave,
                         str(d.get("exchange", "")),
                         str(d.get("sec_type", "FUT")),
                         str(d.get("master_point_value", "1.0"))]):
                    self.symbol_table.setItem(r, col, QTableWidgetItem(text))
```

Also hide `self.launch_terminal_button`/row widgets per platform as in `_on_platform_changed` (already covered). MainWindow's `_config_dict`/`_load_config` need no change: `dataclasses.asdict` picks up the new AccountSpec fields automatically, and old saved configs (missing `platform`) load into the defaults.

- [ ] **Step 4: Run tests**

Run (app venv): `python -m pytest manager/tests/test_slave_editor.py manager/tests/test_main_window.py -v`
Expected: PASS (new + pre-existing; `_load_config` from an old-format settings JSON must still build a running config).

- [ ] **Step 5: Commit**

```bash
git add manager/gui/slave_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): IB platform picker + contract map table in slave editor"
```

---

### Task 10: Status-detail plumbing (`StatusMsg.detail` → GUI status)

**Files:**
- Modify: `manager/supervisor.py` (`_dispatch_slave` StatusMsg branch + `WorkerHandle`)
- Modify: `manager/app/controller.py` (wire the callback)
- Test: `manager/tests/test_supervisor.py` (append)

**Interfaces:**
- Consumes: `StatusMsg.detail` (Task 1), the IB worker filling it (Task 7).
- Produces: `Supervisor.on_slave_status: callable(name: str, detail: str) | None` — a GUI-facing callback invoked only when a slave's detail string changes; the controller forwards it as a `"slave_status"` `StatusUpdate` with `slave_id` set. MT5 slaves send `detail=""` forever ⇒ no callback, no GUI change; regression-safe.

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_supervisor.py` using the real `Supervisor(CopyEngine())` (no subprocesses needed — dispatch messages straight into `_dispatch_slave`):

```python
def _ib_status_handle() -> Supervisor:
    """Supervisor with an IB slave registered but no worker spawned."""
    from manager.ipc.messages import StatusMsg
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="ib1", symbol_map_csv="US30=YM",
                              step_amount=1000.0, step_size=1.0, max_lot=100.0,
                              max_trade_age_minutes=999999,
                              normalize_sltp=True))
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=30,
                     consecutive_failures=3, poll_timeout=0.02)
    sup._handles["ib1"] = WorkerHandle(
        name="ib1", role="slave", proc=None, pipe=None,
        config={"platform": "ib"}, adapter_kind="", fake_state=None)
    return sup


def test_slave_status_detail_forwarded_when_changed():
    from manager.ipc.messages import StatusMsg
    sup = _ib_status_handle()
    seen: list[tuple[str, str]] = []
    sup.on_slave_status = lambda name, detail: seen.append((name, detail))
    try:
        sup._dispatch_slave("ib1", StatusMsg(
            source_id="ib1", role="slave", connected=True, login=0,
            balance=1.0, equity=1.0, currency="USD", server="ib-gateway",
            trade_allowed=True, detail="YM 202612"))
        assert seen == [("ib1", "YM 202612")]
        sup._dispatch_slave("ib1", StatusMsg(
            source_id="ib1", role="slave", connected=True, login=0,
            balance=1.0, equity=1.0, currency="USD", server="ib-gateway",
            trade_allowed=True, detail="YM 202612"))
        assert seen == [("ib1", "YM 202612")]           # unchanged: no repeat
    finally:
        sup.shutdown()


def test_mt5_status_never_forwards_detail():
    from manager.ipc.messages import StatusMsg
    eng = CopyEngine()
    eng.add_slave(_slave_cfg())                     # the file's existing MT5 builder
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=30,
                     consecutive_failures=3, poll_timeout=0.02)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=None, pipe=None,
        config={}, adapter_kind="", fake_state=None)
    seen: list = []
    sup.on_slave_status = lambda *a: seen.append(a)
    try:
        sup._dispatch_slave("s1", StatusMsg(
            source_id="s1", role="slave", connected=True, login=1,
            balance=1.0, equity=1.0, currency="USD", server="Demo"))
        assert seen == []                           # empty detail => silent
    finally:
        sup.shutdown()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_supervisor.py -k detail -v`
Expected: FAIL — no `on_slave_status` attribute.

- [ ] **Step 3: Implement**

In `manager/supervisor.py`:

1. `WorkerHandle` gains `detail: str = ""` (defaults for MT5 handles).
2. `__init__` gains `self.on_slave_status = None` (beside `on_error`).
3. In `_dispatch_slave`'s `StatusMsg` branch:

```python
        elif isinstance(msg, StatusMsg):
            if h is not None:
                h.trade_allowed = msg.trade_allowed
            self._engine.apply_status(slave_id, msg)
            if (msg.detail and h is not None and h.detail != msg.detail
                    and self.on_slave_status is not None):
                h.detail = msg.detail
                self.on_slave_status(slave_id, msg.detail)
```

4. In `manager/app/controller.py`'s `build_supervisor`, wire it:

```python
        sup.on_slave_status = (lambda name, detail: self._status(
            "slave_status", f"{name}: {detail}", slave_id=name))
```

The GUI needs nothing new: `MainWindow.append_status` already renders every `StatusUpdate` (contract/roll state arrives as a status line). The spec's fuller contract-state card is intentionally deferred to smoke-test feedback — note that in the plan, do not expand scope here.

- [ ] **Step 4: Run tests**

Run: `python -m pytest manager/tests/test_supervisor.py manager/tests/test_controller.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/supervisor.py manager/app/controller.py manager/tests/test_supervisor.py
git commit -m "feat(supervisor): forward slave status detail (IB contract state)"
```

---

### Task 11: Updater dependency + paper-gateway smoke doc

**Files:**
- Modify: `manager/update_helper.py:118-143` (`_reinstall`)
- Create: `docs/smoke-test-ib.md`
- Test: `manager/tests/test_update_helper.py` (append)

**Interfaces:**
- Consumes: `ib_async==2.1.0` (Task 6, in `pyproject.toml`).
- Produces: the update path installs `ib_async` explicitly (the in-app updater reinstalls with `--no-deps`, so the wheel's own deps are never processed — this package would silently vanish after an update and every IB slave would stop connecting).

- [ ] **Step 1: Write the failing test**

Read `manager/tests/test_update_helper.py` first — it monkeypatches `update_helper.subprocess.run` with fakes and has a `_run_ok` helper whose class `R` has `returncode=0`. Append the new test:

```python
def test_reinstall_ensures_ib_async_after_wheel(tmp_path, monkeypatch):
    """The helper's post-wheel install names the IB dependency: without it,
    an in-app update (which installs with --no-deps) wipes ib_async and every
    IB slave loses its connection on next Start."""
    import os
    w = _make_wheel(tmp_path / "manager-latest.whl")   # the real cache shape
    cmds: list[list[str]] = []
    monkeypatch.setattr(update_helper.subprocess, "run",
                        lambda cmd, **k: cmds.append(list(cmd)) or _run_ok(cmd))
    monkeypatch.setattr(update_helper, "_log", lambda _m: None)
    rc = update_helper._reinstall(str(w))
    assert rc == 0
    ib = [c for c in cmds if any("ib_async==2.1.0" in a for a in c)]
    assert ib, f"no ib_async install among {cmds}"
    assert any("--upgrade" in c for c in ib)
    # the ib install runs AFTER the wheel install
    wheel_idx = next(i for i, c in enumerate(cmds)
                     if os.path.basename(c[-1]).endswith(".whl"))
    ib_idx = cmds.index(ib[0])
    assert ib_idx > wheel_idx
```

Then adapt the three existing success-path tests that capture a single pip command — the helper now runs a *second* pip command (ib_async), so a single-slot capture would record the wrong one. The assertions stay intact; only the capture changes:

1. `test_reinstall_passes_valid_wheel_filename_to_pip` — change `captured["cmd"] = cmd` to record only wheel commands:
   ```python
   if os.path.basename(cmd[-1]).endswith(".whl"):
       captured["cmd"] = cmd
   ```
   (add `import os` at the top of the test file if the file has none).
2. `test_reinstall_uses_no_deps_so_pip_skips_locked_dependency_dlls` (the `--no-deps` test) — record into a list and assert against the wheel command:
   ```python
   seen_cmds: list[list[str]] = []
   ...   # in the fake: seen_cmds.append(list(cmd))
   ...
   wheel_cmds = [c for c in seen_cmds if c[-1].endswith(".whl")]
   assert any("--no-deps" in c for c in wheel_cmds)
   ```
   (keep the original assertion's other content — mirror the test's own style).
3. `test_reinstall_cleans_up_temp_copy` — only wheel commands should feed the recorded temp dir (`os.path.dirname(cmd[-1])`), so an ib_async command (`... pip install --upgrade ib_async==2.1.0`) doesn't overwrite it. Replace its inline `lambda` fake with a small `fake_run` in the same style:
   ```python
   def fake_run(cmd, **k):
       if cmd[-1].endswith(".whl"):
           seen["d"] = os.path.dirname(cmd[-1])
       return _run_ok(cmd, **k)

   monkeypatch.setattr(update_helper.subprocess, "run", fake_run)
   ```
   (its assertion `assert not os.path.exists(seen["d"])` stays exactly as-is).

The failure-path tests (`test_reinstall_logs_pip_output_on_failure`, which fails on the *wheel* command via `lambda *a, **k: FakeFail()`) are untouched and need no adaptation. They double as the pin for the implementation's placement in Step 3: on a failed wheel install (rc ≠ 0) the ib step must never run.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest manager/tests/test_update_helper.py -v`
Expected: FAIL — no `ib_async` in the pip calls.

- [ ] **Step 3: Implement**

In `manager/update_helper.py`, add a module-level helper (beside `_reinstall`) and call it from `_reinstall`'s try block, after the rc-logging, only on wheel success:

```python
def _ensure_ib_async() -> None:
    # The wheel install uses --no-deps by design (shiboken6's locked
    # msvcp140.dll fails a full --force-reinstall), so it never brings
    # ib_async in. Older installs don't have it; a missing ib_async breaks
    # every IB slave after an update, so install it explicitly. Never fatal:
    # a machine without IB slaves must not have updates broken by this.
    _log("ensuring ib_async")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             "ib_async==2.1.0"],
            capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            _log(f"ib_async install rc={proc.returncode}")
            _log(f"pip stderr:\n{proc.stderr}")
    except Exception as exc:
        _log(f"ib_async install error: {exc}")
```

In `_reinstall`'s try block, replace the bare `return proc.returncode` tail with:

```python
        if proc.returncode != 0:
            _log(f"pip install failed rc={proc.returncode}")
            _log(f"pip stdout:\n{proc.stdout}")
            _log(f"pip stderr:\n{proc.stderr}")
        else:
            _ensure_ib_async()
        return proc.returncode
```

(The else-branch placement is what Step 1's failure-path tests pin: a failed wheel install never reaches the ib step. The existing `finally: shutil.rmtree(...)` stays as the last statement.)

- [ ] **Step 4: Write `docs/smoke-test-ib.md`**

Model it on `docs/smoke-test.md` (read it for tone/structure). Content skeleton — write the full doc in the plan's final file, not a placeholder:

```markdown
# Smoke test — IB slave (paper)

Manual walkthrough for one IB-paper slave. Prereqs: IB Gateway installed
(ibkrguides.com/software/gateway — IB Gateway 10.x), logged in to a paper
account (API port 4002 visible in the Gateway login → Connection → API →
Settings: enable "ActiveX and Socket Clients", disable "Read-Only API"),
`pip install -e .` fresh installs `ib_async==2.1.0` from pyproject.

1. In the manager: Add Slave… → platform "IB Gateway (TWS API)"; id `ib1`;
   contract row `US30 → YM / CME / FUT / 1.0`; sizing Balance step,
   step amount 1000, step size 1; Start.
2. Expect: status shows `ib1: YM 202612` (or `ib1: YM 202612 rolling` inside
   the roll window — the current front month).
3. Open a long on the master (DEMO US_30). Expect the parent + stop + limit
   appear in the Gateway's Orders view tagged `CPY#…`, volume = contracts.
4. Nudge the master's SL/TP; expect the tagged children replaced.
5. Partially close the master; expect one opposite `CPY#…` MKT order and the
   children resized (their quantity ≤ the remaining contracts).
6. Close the master; expect the net position flat and the stop/limit gone.
7. Quit the Gateway mid-run (Tests → Kill). Expect reconnect errors, no
   crash; Start again after Gateway relaunch — recovery records must be
   re-seeded (no duplicate trades after the next open on the master).
8. Set step size so qty floors to 0 for a small master trade: expect a
   logged `skipped: … units -> 0 contracts` and nothing placed.
```

- [ ] **Step 5: Run tests + README pointer**

Add one line to the README's docs list (next to the smoke-test link if present; find it with a grep of `smoke-test.md`):

```markdown
For IB-paper slave setup, see [`docs/smoke-test-ib.md`](docs/smoke-test-ib.md).
```

Run: `python -m pytest manager/tests/test_update_helper.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/update_helper.py manager/tests/test_update_helper.py docs/smoke-test-ib.md README.md
git commit -m "feat(update): install ib_async on no-deps update; IB smoke doc"
```

---

### Task 12: Full regression + spec-assertion pass

**Files:**
- No product files; verification only.

**Interfaces:**
- Consumes: everything.

- [ ] **Step 1: Full unit suite**

Run: `python -m pytest manager/tests -v --tb=short`
Expected: PASS. Any MT5-path failure here is a regression — fix the new code, never the old tests.

- [ ] **Step 2: GUI suite in the app venv**

Run (in the PySide6 venv — see the repo's testing note): `python -m pytest manager/tests/test_slave_editor.py manager/tests/test_main_window.py manager/tests/test_main_window_updates.py -v`
Expected: PASS.

- [ ] **Step 3: Spec-assertion sweep**

Read `docs/superpowers/specs/2026-10-02-ib-futures-slave-design.md` top to bottom; for every "Produces/Must/never rely on IB's implicit auto-cancel" claim, name the test that pins it — the sweep ends with a two-column list (spec claim → test) appended to the PR description. The three claims with no test are plan failures requiring tests, not prose.

- [ ] **Step 4: Commit (docs-only if anything was added)**

```bash
git add -A
git commit -m "test: IB slave regression sweep complete"
```