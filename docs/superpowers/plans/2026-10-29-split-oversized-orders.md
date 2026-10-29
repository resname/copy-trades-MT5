# Split Oversized Slave Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a computed slave order exceeds the slave symbol's per-order `SYMBOL_VOLUME_MAX`, open the volume as several slave orders (150 lots at a 100-lot max → 100 + 50) instead of silently clamping.

**Architecture:** Worker-side split (spec's design A): the engine stops capping at the symbol max (only the per-slave MaxLots config caps the total) and still sends ONE OPEN command per master trade; the worker splits it into grid-aligned chunks, gives each chunk its own `CPY#|MV|SV` comment, and fans every later MODIFY/PARTIAL/CLOSE out over the legs found by comment scan (falling back to today's exact per-ticket routing). Recovery merges same-master records instead of discarding duplicates.

**Tech Stack:** Python 3.10+, pytest, FakeMt5 in-memory adapter (no MetaTrader5 package needed in tests).

**Spec:** `docs/superpowers/specs/2026-10-29-split-oversized-orders-design.md` (including its "Builder-check decisions" section — all six are normative here).

## Global Constraints

- Order comments stay ≤31 chars (`order_send` rejects longer with -2); `_fmt_volume` in `manager/engine/linkage.py` already strips zeros — never comment raw `f"{v:.8f}"`.
- `TRADE_ACTION_SLTP` is the Python package's value (6), NOT the MQL5 enum's 3 — build SL/TP requests only from `manager/worker/mt5_constants.py` symbols, never literals.
- Filling mode always via `select_filling_mode(info.filling_mode)` (bitmap read: bit0=FOK bit1=IOC).
- No new IPC fields: `CommandMsg`/`AckMsg`/`ReconfigureMsg` are unchanged; no changes to `Record`/`RecordTable` shape, GUI, or settings.
- The engine stays pure; ALL terminal logic lives in `manager/worker/mt5_worker.py`.
- Tests run with the app venv (has PySide6, so GUI suites don't skip): `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest <args>`
- No repo version bump (CI versions from `v*` tags).
- Float grid guards use the existing `+1e-9` epsilon idiom.

## Review Focus

The uncircumvented failure modes this feature implies, each pinned by a named test in the task that owns it:

1. **Broker-rewritten comments** → leg undecodable; fan-out must fall back to today's exact per-ticket routing, never lose the position or vacuously succeed. Pinned: Task 4 tests.
2. **Worker restart mid-split** (supervisor's 30s stale check, `manager/supervisor.py:48,335-358`) → heartbeat status between chunks (Task 3 test) + recovery merge re-attaches all opened legs (Task 5/6 tests).
3. **`volume_max` off the lot-step grid or tiny remainders** → chunks snapped down; remainder < `volume_min` rebalances to equal grid-aligned parts. Pinned: Task 1 tests.
4. **Partial-split failure** (chunk k fails after k−1 filled) → stop, keep filled parts, ack ok=True with achieved sum; zero filled → fail as today. Pinned: Task 3 tests.
5. **Leg drift across partial closes** → per-leg SV (own comment) keeps pro-rata targets correct for every leg independently. Pinned: Task 4 test.

---

### Task 1: `split_volume` pure helper

**Files:**
- Modify: `manager/engine/transform.py` (end of file)
- Test: `manager/tests/test_transform.py`

**Interfaces:**
- Consumes: nothing (pure math, stdlib `math` already imported).
- Produces: `split_volume(total: float, volume_max: float, volume_step: float, volume_min: float) -> list[float]` — grid-aligned parts, each ≤ `volume_max`, parts sum to (about) `total`; used verbatim by Task 3 as `from manager.engine.transform import split_volume`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_transform.py` (add `split_volume` to the import from `manager.engine.transform`):

```python
# ---- split_volume: chunk sizes for oversized slave orders ----

def test_split_volume_below_max_is_single_part():
    assert split_volume(50.0, 100.0, 0.01, 0.01) == [50.0]

def test_split_volume_user_example_150_at_max_100():
    assert split_volume(150.0, 100.0, 0.01, 0.01) == [100.0, 50.0]

def test_split_volume_multiple_full_chunks():
    assert split_volume(250.0, 100.0, 0.01, 0.01) == [100.0, 100.0, 50.0]

def test_split_volume_snaps_max_chunk_to_step_grid():
    # volume_max 100.5 is off the 1.0 step grid -> every chunk snapped down
    assert split_volume(150.0, 100.5, 1.0, 1.0) == [100.0, 50.0]

def test_split_volume_remainder_below_min_rebalances_equal_parts():
    # greedy: 100 + 0.5, but 0.5 < volume_min 1.0 -> equal 2-part split
    assert split_volume(100.5, 100.0, 0.5, 1.0) == [50.5, 50.0]

def test_split_volume_degenerate_inputs_single_part():
    assert split_volume(0.0, 100.0, 0.01, 0.01) == [0.0]
    assert split_volume(5.0, 0.0, 0.01, 0.01) == [5.0]   # unusable volume_max -> legacy single order
    assert split_volume(5.0, 100.0, 0.0, 0.01) == [5.0]  # unusable volume_step -> legacy single order
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py -k split_volume -v`
Expected: FAIL with `ImportError: cannot import name 'split_volume'`.

- [ ] **Step 3: Implement `split_volume` at the end of `manager/engine/transform.py`**

Algorithm (greedy chunks, spec's decided policy):

```python
def split_volume(total: float, volume_max: float, volume_step: float,
                 volume_min: float) -> list[float]:
    """Split an oversized order volume into grid-aligned chunks each at most
    volume_max, for the worker's multi-order OPEN. Greedy: as many full
    volume_max chunks as fit (snapped DOWN to volume_step), remainder last.
    A remainder below volume_min (only possible when volume_max is not a
    volume_step multiple) rebalances into equal parts. Returns [total] when
    splitting is unnecessary or impossible (total <= 0, volume_step <= 0,
    volume_max <= 0, or total <= volume_max) — the caller then opens one
    order exactly as before."""
    if total <= 0.0 or volume_step <= 0.0 or volume_max <= 0.0:
        return [total]
    max_steps = math.floor(volume_max / volume_step + 1e-9)
    total_steps = math.floor(total / volume_step + 1e-9)
    if max_steps < 1 or total <= volume_max + 1e-9:
        return [total]
    lot_digits = max(0, int(round(-math.log10(volume_step))))
    n_fill = total_steps // max_steps          # full max chunks
    rem_steps = total_steps - n_fill * max_steps
    if rem_steps == 0:
        return [round(max_steps * volume_step, lot_digits)] * n_fill
    if rem_steps * volume_step >= volume_min - 1e-9:
        return [round(max_steps * volume_step, lot_digits)] * n_fill \
            + [round(rem_steps * volume_step, lot_digits)]
    # remainder below volume_min: equal split over n_fill + 1 parts; every
    # part is step-aligned and <= volume_max (total_steps < (n_fill+1)*max_steps
    # is guaranteed because rem_steps < volume_min/volume_step <= max_steps).
    n = n_fill + 1
    base = total_steps // n
    extra = total_steps - base * n
    return [round((base + (1 if i < extra else 0)) * volume_step, lot_digits)
            for i in range(n)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py -v`
Expected: ALL PASS (including the pre-existing tests — this task only appends).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/transform.py manager/tests/test_transform.py
git commit -m "feat(engine): split_volume helper for oversized slave orders"
```

---

### Task 2: Engine stops capping at the symbol's volume_max

**Files:**
- Modify: `manager/engine/transform.py:109-168` (`_snap_clamp`, `calculate_slave_lot`, `calculate_lots`)
- Modify: `manager/engine/copy_loop.py:71-75` (call site)
- Modify: `manager/tests/test_transform.py`, `manager/tests/test_engine_integration.py:39`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_snap_clamp(lots, lot_step, min_lot, max_lot) -> float` and `calculate_slave_lot(mode, master_volume, balance, step_amount, step_size, master_base_lot, fixed_lot, max_lot, lot_step, min_lot) -> float` and `calculate_lots(balance, step_amount, step_size, max_lot, lot_step, min_lot) -> float` — all WITHOUT the former trailing `max_lot_symbol` parameter. Later tasks/tasks' tests rely on these exact 4-arg/10-arg/6-arg signatures.

- [ ] **Step 1: Write the failing test**

In `manager/tests/test_transform.py`, replace `test_snap_clamp_caps_at_max_lot_and_symbol_max` (lines ~155-157) with:

```python
def test_snap_clamp_caps_at_max_lot_only():
    assert _snap_clamp(5.0, 0.01, 0.01, 2.0) == 2.0  # MaxLots config cap stays
```

and replace the symbol-max case in `test_calculate_lots_caps_at_symbol_max_or_similar` / `test_calculate_lots_capped…` (line ~125, currently asserting `== 0.2`) with:

```python
def test_calculate_lots_no_longer_capped_at_symbol_max():
    # worker now splits oversized orders; engine caps only at max_lot (10)
    assert calculate_lots(10000, 100, 0.01, 10, 0.01, 0.01) == 1.0
```

(If the file names these tests differently, keep the test's original purpose — the *config-cap* cases at line ~120 stay; only the two symbol-cap assertions become the two rewritten ones above.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py -v`
Expected: the old tests fail/TypeError on missing `max_lot_symbol` (new tests not satisfiable yet).

- [ ] **Step 3: Implement**

- `manager/engine/transform.py`: `_snap_clamp` — remove the `max_lot_symbol` parameter and the `lots = min(lots, max_lot_symbol)` line; update the docstring ("cap at the per-slave `max_lot` config; oversized volume is split worker-side"). `calculate_slave_lot` — drop the `max_lot_symbol` parameter and its three pass-throughs; update docstring. `calculate_lots` — drop the parameter from signature, docstring, and the internal call.
- `manager/engine/copy_loop.py:71-75`: remove `info.volume_max` from the `calculate_slave_lot` call (`lots = calculate_slave_lot(cfg.sizing_mode, pos.volume, state.balance, cfg.step_amount, cfg.step_size, cfg.master_base_lot, cfg.fixed_lot, cfg.max_lot, info.volume_step, info.volume_min)`).
- `manager/tests/test_transform.py` + `manager/tests/test_engine_integration.py:39`: remove the final `max_lot_symbol` argument from every `calculate_lots`/`calculate_slave_lot` call (they all end with the same value, e.g. `100`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/engine/transform.py manager/engine/copy_loop.py manager/tests/test_transform.py manager/tests/test_engine_integration.py
git commit -m "feat(engine): drop symbol volume_max cap from sizing (worker splits instead)"
```

---

### Task 3: Worker splits oversized OPENs

**Files:**
- Modify: `manager/worker/mt5_worker.py` (imports, OPEN branch `execute_command` ~lines 141-172, `_slave_loop` ~line 335)
- Test: `manager/tests/test_mt5_worker.py`

**Interfaces:**
- Consumes: `split_volume` (Task 1), `decode_comment`/`encode_comment` (already imported from `manager.engine.linkage`).
- Produces: `execute_command(adapter, cmd, normalize_sltp, retry_count, retry_delay_ms, heartbeat=None) -> AckMsg` — the optional `heartbeat: Callable[[], None]` is called before each chunk after the first. `_slave_loop` passes a status-emitting closure; engine/other callers keep the 4-arg form (tests, integration harness).

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_mt5_worker.py`:

```python
# ---- execute_command: OPEN split (oversized volumes) ----
from dataclasses import replace
from manager.ipc.messages import CommandMsg

SI_MAX05 = replace(SI, volume_max=0.5)   # same step/min (0.01), tiny per-order max

def _split_adapter(**kw):
    return FakeMt5(
        symbol_infos={"EURUSD": SI_MAX05},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks={"EURUSD": (1.10000, 1.10010)},
        **kw)

def _open_cmd(volume, comment=None):
    return CommandMsg(slave_id="s1", action="OPEN", master_ticket=42,
                      symbol="EURUSD", volume=volume, sl=0.0, tp=0.0,
                      master_open_price=1.10000, side=BUY, magic=magic_for(42),
                      comment=comment if comment is not None
                      else encode_comment(42, 2.0, volume))

def test_execute_open_splits_into_max_chunks_with_per_part_comments():
    mt = _split_adapter()
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert ack.fill_volume == pytest.approx(1.2, abs=1e-9)
    parts = [p for p in mt.positions_get() if p.comment.startswith("CPY#")]
    assert [p.volume for p in parts] == [pytest.approx(0.5), pytest.approx(0.5),
                                         pytest.approx(0.2)]
    for p in parts:  # each leg its own SV, shared master ticket + magic + SL/TP
        assert p.comment in ("CPY#42|MV2|SV0.5", "CPY#42|MV2|SV0.2")
        assert p.magic == magic_for(42)
    assert ack.slave_ticket == parts[0].ticket

def test_execute_open_single_order_unchanged_when_under_symbol_max():
    mt = _split_adapter()
    comment = encode_comment(42, 0.5, 0.1)
    ack = execute_command(mt, _open_cmd(0.1, comment), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok and len([p for p in mt.positions_get()
                           if p.comment.startswith("CPY#")]) == 1
    assert mt.positions_get()[-1].comment == comment  # byte-identical legacy path

def test_execute_open_split_partial_failure_keeps_filled_parts():
    from manager.worker.mt5_constants import TRADE_RETCODE_DONE
    mt = _split_adapter(order_results=[
        {"retcode": TRADE_RETCODE_DONE, "order": 777, "deal": 777,
         "price": 1.10010, "volume": 0.5},
        {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0},
    ])
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok and ack.fill_volume == pytest.approx(0.5)
    assert ack.slave_ticket == 777
    assert "shortfall" in (ack.error or "")
    assert "split" in (ack.error or "")

def test_execute_open_split_total_failure_fails_like_today():
    mt = _split_adapter(order_results=[
        {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0},
        {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0},
    ])
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == 10004

def test_execute_open_split_undecodable_comment_fails():
    mt = _split_adapter()
    ack = execute_command(mt, _open_cmd(1.2, comment="unrelated"),
                          normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and "split" in ack.error and "undecodable" in ack.error

def test_execute_open_split_heartbeats_between_chunks():
    mt = _split_adapter()
    beats = []
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0,
                          heartbeat=lambda: beats.append(1))
    assert ack.ok and len(beats) == 2  # 3 chunks -> 2 inter-chunk heartbeats

def test_execute_open_split_zero_parts_requested_volume_fully_single():
    # volume exactly == volume_max -> single order, legacy path (no split)
    mt = _split_adapter()
    comment = encode_comment(42, 0.5, 0.5)
    ack = execute_command(mt, _open_cmd(0.5, comment), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok and len([p for p in mt.positions_get()
                           if p.comment.startswith("CPY#")]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_mt5_worker.py -k split -v`
Expected: FAIL (`split_volume` sends one oversized order; FakeMt5 still creates one position > volume_max) or TypeError on the `heartbeat` kwarg.

- [ ] **Step 3: Implement in `manager/worker/mt5_worker.py`**

- Import `split_volume` from `manager.engine.transform` (module already imports `normalize_sltp as _normalize_sltp, round_to_tick, parse_symbol_map` — extend that import).
- `execute_command`: add trailing param `heartbeat=None`.
- OPEN branch, after the existing SL/TP rounding block (all of which stays and applies to every chunk):

```python
parts: list[float] | None = None
mv: float | None = None
if cmd.volume > info.volume_max + 1e-9:
    decoded = decode_comment(cmd.comment)
    mv = decoded[1] if decoded else None
    if mv is None:
        return _fail(-1, "cannot split: comment MV/SV undecodable")
    parts = split_volume(cmd.volume, info.volume_max, info.volume_step,
                         info.volume_min)
    if len(parts) <= 1:
        parts = None  # unusable volume_max/step -> legacy single order
```

- Keep the existing request dict as the template. When `parts is None`: today's single send and ack, byte-identical (the request/ack code already there — restructure so the template is built once and the single-order path sends `[{"volume": cmd.volume, "comment": cmd.comment}]`-equivalent request exactly as today; do NOT alter its comment or ack fields).
- When `parts` is set, loop (spec decisions 1/2: stop at first failing chunk; count ACTUAL fills):

```python
filled = 0.0
first_ticket = 0
attempts = 0
for i, part in enumerate(parts):
    if i > 0 and heartbeat is not None:
        heartbeat()  # status heartbeat so the supervisor's stale timer never fires mid-split
    req_part = dict(req, volume=part,
                    comment=encode_comment(cmd.master_ticket, mv, part))
    res = _order_send_with_retry(adapter, req_part, retry_count, retry_delay_ms)
    attempts = i + 1
    if res.get("retcode") != TRADE_RETCODE_DONE:
        break  # stop at first failing chunk
    filled += res.get("volume") or part
    if first_ticket == 0:
        first_ticket = res["order"]
if filled <= 0.0:
    return _fail(res.get("retcode", -1), str(adapter.last_error()))
```

  The success ack mirrors today's fields, plus an informational note (spec decision 6). While looping, track `filled_chunks` (0 or 1 on the completed chunk) and a `failure_retcode` so the note reads, e.g. `split: 2/2 ok` or `split: 1/2 ok (shortfall: requested 1.2, filled 0.5)`. Success ack:

```python
return AckMsg(slave_id=cmd.slave_id, action="OPEN",
              master_ticket=cmd.master_ticket, ok=True,
              slave_ticket=first_ticket, fill_price=res["price"],
              fill_volume=filled, remaining_volume=filled,
              retcode=TRADE_RETCODE_DONE, error=note)
```
- `_slave_loop` (~line 335): define before the loop

```python
def _heartbeat():
    try:
        send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
    except (EOFError, OSError):
        pass  # manager gone: the next real send exits the loop
```

  and pass `heartbeat=_heartbeat` in the `execute_command` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_mt5_worker.py -v`
Expected: ALL PASS (pre-existing OPEN tests keep passing: single-order path unchanged).

- [ ] **Step 5: Commit**

```bash
git add manager/worker/mt5_worker.py manager/tests/test_mt5_worker.py
git commit -m "feat(worker): split oversized OPENs into volume_max chunks with per-leg comments"
```

---

### Task 4: Worker fans out MODIFY / PARTIAL_CLOSE / CLOSE over the split legs

**Files:**
- Modify: `manager/worker/mt5_worker.py` (MODIFY ~174-198, PARTIAL_CLOSE ~200-238, CLOSE ~240-264; new `_find_legs` helper near the top)
- Test: `manager/tests/test_mt5_worker.py`

**Interfaces:**
- Consumes: nothing new outside the module (`decode_comment` already imported; `Position`/`BUY`/`SELL` already imported).
- Produces: `_find_legs(adapter, cmd) -> list[Position]` — positions whose decoded CPY comment names `cmd.master_ticket`, else (scan empty) today's `position_by_ticket(cmd.slave_ticket)` fallback, else `[]`. Module-private; the three fan-out branches are its only consumers (plus tests).

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_mt5_worker.py`:

```python
# ---- execute_command: fan-out over split legs ----

def _leg(ticket, volume, master=1, open_price=1.10010):
    return Position(ticket, "EURUSD", BUY, open_price, volume, 0, 0, 0,
                    0.00001, comment=encode_comment(master, 1.0, volume))

def test_execute_modify_fans_out_over_split_legs_at_own_open_prices():
    mt = _adapter(positions=[_leg(777, 0.6, open_price=1.10010),
                             _leg(778, 0.4, open_price=1.10005)])
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=777, sl=1.09400, tp=1.10600,
                     master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    # SL distance 0.00600 from each leg's own open price
    assert mt.position_by_ticket(777).sl == pytest.approx(1.09410, abs=1e-8)
    assert mt.position_by_ticket(778).sl == pytest.approx(1.09405, abs=1e-8)

def test_execute_modify_falls_back_to_ticket_when_comment_mangled():
    # broker rewrote the comment: CPY decode finds nothing -> legacy
    # per-ticket routing must still reach the leg (Review Focus #1)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.6, 0, 0, 0,
                                      0.00001, comment="broker-rewritten")])
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=777, sl=1.09400, tp=0.0,
                     master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok and mt.position_by_ticket(777).sl == pytest.approx(1.09410, abs=1e-8)

def test_execute_modify_no_legs_fails_like_today():
    mt = _adapter()
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=777, sl=1.09, tp=0.0,
                     master_open_price=1.1, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and "gone" in ack.error

def test_execute_partial_close_pro_rata_each_leg():
    # master 1.0 split as 0.6 + 0.4; master halves (fraction 0.5) -> each leg
    # keeps its OWN fraction: targets 0.3 / 0.2 (Review Focus #5)
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.5,
                     master_open_volume=1.0, slave_open_volume=1.0)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777).volume == pytest.approx(0.3, abs=1e-8)
    assert mt.position_by_ticket(778).volume == pytest.approx(0.2, abs=1e-8)
    assert ack.remaining_volume == pytest.approx(0.5, abs=1e-8)

def test_execute_partial_close_pro_rata_full_chunk_close():
    # master 1.0 split 0.6 + 0.4; master closes to 0.55 -> fraction 0.55:
    # leg1 target 0.33 (close 0.27), leg2 target 0.22 (close 0.18)
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.55,
                     master_open_volume=1.0, slave_open_volume=1.0)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777).volume == pytest.approx(0.33, abs=1e-8)
    assert mt.position_by_ticket(778).volume == pytest.approx(0.22, abs=1e-8)

def test_execute_partial_close_skips_already_at_target_leg_and_closes_only_deficit_leg():
    # mixed state: leg 777 already below its target (closed manually), leg 778 exact
    mt = _adapter(positions=[_leg(777, 0.3), _leg(778, 0.4)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.5,
                     master_open_volume=1.0, slave_open_volume=0.6)
    # NOTE cmd.slave_open_volume unused per leg: each leg's own SV comment governs
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777).volume == pytest.approx(0.3, abs=1e-8)  # nothing to close
    assert mt.position_by_ticket(778).volume == pytest.approx(0.2, abs=1e-8)

def test_execute_close_closes_all_legs_of_split():
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)])
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=777)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777) is None and mt.position_by_ticket(778) is None

def test_execute_close_fanout_fails_ack_on_failing_leg():
    from manager.worker.mt5_constants import TRADE_RETCODE_DONE
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)],
                  order_results=[
                      {"retcode": TRADE_RETCODE_DONE, "order": 777, "deal": 777,
                       "price": 1.1, "volume": 0.6},
                      {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0,
                       "volume": 0.0}])
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=777)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == 10004
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_mt5_worker.py -k "fan or pro_rata or mangled or closes_all or no_legs" -v`
Expected: FAIL (only the single `cmd.slave_ticket` leg is touched — 778 unchanged) or NameError on `_leg`.

- [ ] **Step 3: Implement `_find_legs` and the three fan-out branches in `manager/worker/mt5_worker.py`**

```python
def _find_legs(adapter, cmd) -> list[Position]:
    """Slave legs of one master trade: positions whose decoded CPY comment
    names this master ticket (the split-OPEN writer's per-leg comments). If
    the scan finds nothing -- e.g. a broker rewrote the comment -- fall back
    to today's exact per-ticket routing so a position is never lost."""
    legs = [p for p in adapter.positions_get()
            if (d := decode_comment(p.comment)) and d[0] == cmd.master_ticket]
    if not legs:
        pos = adapter.position_by_ticket(cmd.slave_ticket)
        if pos is not None:
            legs = [pos]
    return legs
```

Rewrite the three branches of `execute_command` to loop over `legs = _find_legs(adapter, cmd)`, preserving today's single-leg behavior when the list has one element:

- **MODIFY**: keep today's "no tick"/info/rounding failure messages and sequence; per leg: `slave_open = pos.open_price` → normalize → tick-round → `TRADE_ACTION_SLTP` send (with retry). Return `_fail` on the first failing leg (today's semantics — accepted, no retry policy change). On success ack exactly as today (`slave_ticket=cmd.slave_ticket`). With no legs at all: today's `_fail(-1, f"position #{cmd.slave_ticket} gone")`.
- **PARTIAL_CLOSE**: fraction guard first (`if cmd.master_open_volume <= 0.0: return _fail(-1, "cannot compute partial fraction")`), then per leg: `sv = d[2] if decode ok else cmd.slave_open_volume` (an undecodable legacy leg uses the record aggregate — today's exact value); `target = sv * fraction`; `vol_to_close = min(floor((current − target)/info.volume_step + 1e-9) * info.volume_step, current)`; skip legs with ≤ 0 to close; tick/price/opposite-type exactly as today; first failing leg → `_fail`. Success: `fill_volume` = sum closed so far, `remaining_volume` = sum of each leg's live volume (`position_by_ticket`, gone → 0.0), `slave_ticket=cmd.slave_ticket` as today.
- **CLOSE**: per leg: vanished leg counts as closed (`position_by_ticket` None inside the loop → skip with no error); tick missing → `_fail` (as today); send today's close request per leg; first failing leg → `_fail` (record stays; restart recovers remaining legs). ALL legs gone upfront → today's ok-ack (vanished position). Success ack as today.

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_mt5_worker.py manager/tests/test_engine_integration.py -v`
Expected: ALL PASS — the pre-existing single-position MODIFY/PARTIAL/CLOSE tests must still pass unchanged (scan finds the same leg).

- [ ] **Step 5: Commit**

```bash
git add manager/worker/mt5_worker.py manager/tests/test_mt5_worker.py
git commit -m "feat(worker): fan out MODIFY/PARTIAL_CLOSE/CLOSE over split legs with per-ticket fallback"
```

---

### Task 5: Recovery merge — seed instead of discarding duplicate master tickets

**Files:**
- Modify: `manager/engine/baseline.py:17-27` (`seed_from_recovery`)
- Test: `manager/tests/test_baseline.py`, `manager/tests/test_recovery.py`

**Interfaces:**
- Consumes: `Record`, `RecordTable` (unchanged shapes).
- Produces: `seed_from_recovery(table: RecordTable, records: Iterable[Record]) -> int` — same signature; the return value becomes the number of master tickets added. A recovery batch may now carry N records per master ticket (the worker's `build_recovery_records` already emits that shape); identical batches re-sent WITHOUT a table reset stay no-ops (pinned test must stay green).

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_baseline.py`:

```python
def test_seed_from_recovery_merges_split_legs():
    magic = 1077
    table = RecordTable()
    legs = [Record(1, magic, 100, 1.0, 0.5), Record(1, magic, 101, 1.0, 0.5),
            Record(2, 1078, 102, 0.5, 0.3)]
    added = seed_from_recovery(table, legs)
    assert added == 2
    rec = table.get(1)
    assert rec is not None
    assert rec.slave_ticket == 100            # first leg's ticket is the record's
    assert rec.slave_open_volume == pytest.approx(1.0)   # legs summed
    assert rec.master_open_volume == pytest.approx(1.0)
    assert table.get(2).slave_open_volume == pytest.approx(0.3)

def test_seed_from_recovery_reapply_without_reset_is_noop():
    # second RecoveryMsg without a table reset (no reset_slave in between):
    # must not double-count or overwrite (pinned behavior preserved)
    table = RecordTable()
    legs = [Record(1, 1077, 100, 1.0, 0.5), Record(1, 1077, 101, 1.0, 0.5)]
    assert seed_from_recovery(table, legs) == 1
    assert seed_from_recovery(table, legs) == 0
    assert table.get(1).slave_open_volume == pytest.approx(1.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_baseline.py -v`
Expected: FAIL — the 0.5 leg is silently discarded today (`baseline.py:23-24` `if table.has(...): continue`).

- [ ] **Step 3: Implement**

Rewrite `seed_from_recovery` to group the incoming batch by master ticket first, then add:

```python
def seed_from_recovery(table: RecordTable, records: Iterable[Record]) -> int:
    """Populate a RecordTable from records recovered by a slave worker on
    (re)start. Records are grouped by master ticket first: a split order's
    legs (N records, one per slave leg) merge into ONE Record — first leg's
    slave_ticket, summed slave_open_volume; MV/magic are identical across
    legs (the first record's wins). Master tickets already present in the
    table are skipped: production always calls reset_slave before re-seeding,
    so a pre-existing ticket means the same batch is being re-applied and
    must not double-count. Returns the number of master tickets added."""
    merged: dict[int, Record] = {}
    for r in records:
        m = merged.get(r.master_ticket)
        if m is None:
            merged[r.master_ticket] = Record(
                master_ticket=r.master_ticket, magic=r.magic,
                slave_ticket=r.slave_ticket,
                master_open_volume=r.master_open_volume,
                slave_open_volume=r.slave_open_volume)
        else:
            m.slave_open_volume += r.slave_open_volume
    added = 0
    for rec in merged.values():
        if table.has(rec.master_ticket):
            continue
        table.add(rec)
        added += 1
    return added
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_baseline.py manager/tests/test_recovery.py manager/tests/test_copy_loop.py -v`
Expected: ALL PASS — `test_recovery_does_not_overwrite_existing_record` and the duplicate-skip test must stay green unchanged.

- [ ] **Step 5: Commit**

```bash
git add manager/engine/baseline.py manager/tests/test_baseline.py
git commit -m "fix(engine): merge same-master recovery records instead of discarding split legs"
```

---

### Task 6: End-to-end split integration test

**Files:**
- Test: `manager/tests/test_engine_integration.py` (append; reuse the module's existing engine/worker fixture patterns)

**Interfaces:**
- Consumes: `CopyEngine` + `derive_command`/`apply_ack` flow and `execute_command`/`build_recovery_records`/`seed_from_recovery` — all from Tasks 1-5.
- Produces: one composed test proving the whole story: derive → split-open → ack → restart-recover → partial → close.

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_engine_integration.py`:

```python
def test_split_oversized_order_end_to_end():
    # slave symbol max 0.5; master opens 1.0 with copy_master sizing (max_lot 10)
    si = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                    volume_step=0.01, volume_min=0.01, volume_max=0.5)
    mt = FakeMt5(symbol_infos={"EURUSD": si},
                 account={"login": 1, "balance": 100.0, "equity": 100.0,
                          "currency": "USD", "server": "Demo"},
                 ticks={"EURUSD": (1.10000, 1.10010)})
    engine = CopyEngine()
    engine.add_slave(SlaveConfig(
        slave_id="s1", symbol_map_csv="EURUSD=EURUSD", step_amount=100.0,
        step_size=0.01, max_lot=10.0, max_trade_age_minutes=60,
        normalize_sltp=True, sizing_mode=SIZING_COPY_MASTER,
        master_base_lot=0.0, fixed_lot=0.01))
    engine.apply_symbol_info("s1", {"EURUSD": si}, requested=["EURUSD"])
    position = Position(42, "EURUSD", BUY, 1.10000, 1.0, 0, 0, 0, 0.00001)
    cmds = engine.ingest_snapshot(Snapshot(timestamp=1000, heartbeat=0,
                                           positions=(position,)), now=1000)
    (cmd,) = cmds["s1"]
    assert cmd.action == "OPEN" and cmd.volume == pytest.approx(1.0)
    # worker splits: two legs of 0.5
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok and ack.fill_volume == pytest.approx(1.0)
    legs = [p for p in mt.positions_get() if p.comment.startswith("CPY#")]
    assert len(legs) == 2
    engine.apply_ack("s1", ack)   # book the aggregate into the record
    # restart path: worker rebuilds one record per leg; engine merges to one
    records = build_recovery_records(mt)
    assert len(records) == 2
    engine.reset_slave("s1")
    assert engine.apply_recovery("s1", records) == 1
    # master CLOSES -> fan-out CLOSE reaches BOTH legs, ack ok
    cmds = engine.ingest_snapshot(Snapshot(timestamp=2000, heartbeat=1,
                                           positions=()), now=2000)
    (close_cmd,) = cmds["s1"]
    assert close_cmd.action == "CLOSE"
    close_ack = execute_command(mt, close_cmd, normalize_sltp=True,
                                retry_count=1, retry_delay_ms=0)
    assert close_ack.ok
    assert not any(p.comment.startswith("CPY#") for p in mt.positions_get())
    assert engine.apply_ack("s1", close_ack) == []
    # no re-NEW: the master is gone and the record was removed
    assert all(c.action != "OPEN" for c in
               engine.ingest_snapshot(Snapshot(timestamp=2200, heartbeat=3,
                                               positions=()), now=2200)["s1"])
```

(Adapt the import list/naming to what the module already imports;
extend imports from `manager.engine.copy_loop` / `manager.engine.transform`
in the module's existing style.)

- [ ] **Step 2: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_engine_integration.py -v`
Expected: PASS. If the first run red-lights something in Tasks 2-5, fix THAT task's code, not the test.

- [ ] **Step 3: Commit**

```bash
git add manager/tests/test_engine_integration.py
git commit -m "test: end-to-end oversized-order split, recovery merge, fan-out close"
```

---

### Task 7: README wording

**Files:**
- Modify: `README.md:81-90` (Risk-managed lot sizing bullets)

- [ ] **Step 1: Update the wording**

Keep the "clamped to its min/max" phrases but append one sentence to the bullet block (after line 88, before or combined with line 90's cap sentence):

> If a computed volume exceeds the slave symbol's per-order max (`SYMBOL_VOLUME_MAX`), the order is **split into several slave orders** — e.g. 150 lots at a 100-lot max opens 100 + 50 — each leg carrying its own linkage comment; partial closes shrink every leg pro-rata and full closes close them all. (Netting-mode slave accounts merge same-symbol positions, so splitting targets hedging accounts.) The per-slave **max lot** still caps the copied total.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README — oversized volumes split across multiple slave orders"
```

---

## Final verification (after Task 7)

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests -v`
Expected: the entire suite passes. GUI modules are covered because this venv has PySide6 (headless-pytest skips would NOT be acceptable here).