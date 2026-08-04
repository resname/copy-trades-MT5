# Algo-Trading Preflight + README Quick Start — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Block Start (before any copy is attempted) when any terminal reports `mt5.terminal_info().trade_allowed == False`, surfacing a modal GUI message naming the offending terminal(s); add a concise Quick Start guide to the README with an explicit "enable Algo Trading" step.

**Architecture:** Reuse the existing worker + supervisor readiness gate. Each worker already sends an initial `StatusMsg`; add `trade_allowed` (read from a new `adapter.terminal_info()`) to that message. The supervisor records it on the `WorkerHandle`. After the slave readiness gate passes, the controller checks every slave's `trade_allowed` and blocks (shuts down workers, raises `AlgoTradingDisabledError`) if any is `False`; after spawning the master it waits for the master's first status and applies the same check. The GUI catches `AlgoTradingDisabledError` and shows a `QMessageBox.warning`.

**Tech Stack:** Python 3.11, PySide6/Qt, pytest, `MetaTrader5` Python package (real adapter), `FakeMt5` (tests).

## Global Constraints

- Work on `main` only (no feature branches, no worktrees).
- Demo accounts only — never a real account; no credentials stored/piped/logged.
- `StatusMsg` is a `frozen=True` dataclass serialized by a generic scalar-field dump in `manager/ipc/messages.py` `encode`/`decode`; adding a scalar field with a default round-trips automatically. Do NOT add bespoke encode/decode for it.
- `FakeMt5.__init__` takes keyword-only-ish dict params (`positions`, `symbol_infos`, `account`, `ticks`, `order_results`); add `terminal_info` in the same style.
- GUI tests need the PySide6 venv at `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe`; headless pytest skips PySide6 tests via `pytest.importorskip("PySide6")`.
- `gh` CLI is not on PATH — call `C:\Program Files\GitHub CLI\gh.exe` by full path if needed.
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Run the suite with `pytest -q` from the repo root (or the PySide6 venv for GUI tests).

---

## File Structure

- `manager/worker/mt5_adapter.py` — add `terminal_info()` to the `Mt5Adapter` Protocol, `RealMt5`, and `FakeMt5`.
- `manager/ipc/messages.py` — add `trade_allowed: bool = True` to `StatusMsg`.
- `manager/worker/mt5_worker.py` — `_status()` reads `adapter.terminal_info()` and sets `trade_allowed` on the `StatusMsg`.
- `manager/supervisor.py` — `WorkerHandle.trade_allowed`; record it on slave + master `StatusMsg`; add `slave_trade_allowed()`, `master_trade_allowed()`, `wait_for_master_status()`; set master `got_status` on its `StatusMsg`.
- `manager/app/controller.py` — `AlgoTradingDisabledError`; slave + master preflight blocks in `start()`.
- `manager/gui/main_window.py` — catch `AlgoTradingDisabledError` in `_on_start` → `QMessageBox.warning`.
- `README.md` — Quick Start section + Usage note + Troubleshooting row.
- Tests: `manager/tests/test_mt5_adapter.py`, `manager/tests/test_messages.py`, `manager/tests/test_mt5_worker.py`, `manager/tests/test_supervisor.py`, `manager/tests/test_controller.py`, `manager/tests/test_main_window.py`.

---

### Task 1: Adapter `terminal_info()`

**Files:**
- Modify: `manager/worker/mt5_adapter.py:12-25` (Protocol), `:28-43` (FakeMt5 `__init__`), `:207-213` (RealMt5, add method after `account_info`)
- Test: `manager/tests/test_mt5_adapter.py`

**Interfaces:**
- Produces: `Mt5Adapter.terminal_info() -> dict` returning `{"trade_allowed": bool}`. `RealMt5.terminal_info()` reads `mt5.terminal_info().trade_allowed` (False if None). `FakeMt5.terminal_info()` returns a copy of `self.terminal_info` (dict defaulting to `{"trade_allowed": True}`). Task 2's `_status()` consumes `adapter.terminal_info()["trade_allowed"]`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_mt5_adapter.py`:

```python
def test_terminal_info_defaults_trade_allowed_true():
    mt = FakeMt5()
    assert mt.terminal_info() == {"trade_allowed": True}


def test_terminal_info_scripts_trade_allowed_false():
    mt = FakeMt5(terminal_info={"trade_allowed": False})
    assert mt.terminal_info() == {"trade_allowed": False}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest manager/tests/test_mt5_adapter.py -k terminal_info -v`
Expected: FAIL with `AttributeError: 'FakeMt5' object has no attribute 'terminal_info'`

- [ ] **Step 3: Add `terminal_info()` to the Protocol**

In `manager/worker/mt5_adapter.py`, add to the `Mt5Adapter` Protocol (after the `order_send` line, line 25):

```python
    def terminal_info(self) -> dict: ...
```

- [ ] **Step 4: Add `terminal_info` to `FakeMt5`**

In `FakeMt5.__init__` (lines 33-43), add a `terminal_info=None` parameter and store it. The full updated `__init__` signature + first lines:

```python
    def __init__(self, positions=None, symbol_infos=None, account=None,
                 ticks=None, order_results=None, terminal_info=None):
        self.positions: list[Position] = list(positions or [])
        self.symbol_infos: dict[str, SymbolInfo] = dict(symbol_infos or {})
        self.account: dict = dict(account or {})
        self.ticks: dict[str, tuple[float, float]] = dict(ticks or {})
        self._canned = list(order_results or [])
        self._order_seq = 500000
        self._last_error: tuple[int, str] = (0, "")
        self._connected = False
        self.last_request: dict = {}
        self.terminal_info: dict = dict(terminal_info or {"trade_allowed": True})
```

Then add the method (after `account_info`, around line 72):

```python
    def terminal_info(self):
        return dict(self.terminal_info)
```

- [ ] **Step 5: Add `terminal_info()` to `RealMt5`**

In `RealMt5` (after `account_info`, after line 213), add:

```python
    def terminal_info(self):
        mt5 = self._mod()
        t = mt5.terminal_info()
        if t is None:
            return {"trade_allowed": False}
        return {"trade_allowed": bool(t.trade_allowed)}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest manager/tests/test_mt5_adapter.py -k terminal_info -v`
Expected: PASS

- [ ] **Step 7: Run the full adapter suite to confirm no regression**

Run: `pytest manager/tests/test_mt5_adapter.py -q`
Expected: all PASS (existing tests unaffected — `terminal_info` is a new optional kwarg defaulting to `{"trade_allowed": True}`).

- [ ] **Step 8: Commit**

```bash
git add manager/worker/mt5_adapter.py manager/tests/test_mt5_adapter.py
git commit -m "feat(adapter): add terminal_info() reading trade_allowed (Algo Trading)" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `StatusMsg.trade_allowed` + worker `_status` + supervisor recording

**Files:**
- Modify: `manager/ipc/messages.py:73-83` (`StatusMsg`)
- Modify: `manager/worker/mt5_worker.py:74-80` (`_status`)
- Modify: `manager/supervisor.py:18-34` (`WorkerHandle`), `:154-182` (`_dispatch_slave`), `:227-228` (`_read_master` StatusMsg branch)
- Test: `manager/tests/test_messages.py`, `manager/tests/test_mt5_worker.py`, `manager/tests/test_supervisor.py`

**Interfaces:**
- Produces: `StatusMsg.trade_allowed: bool` (default `True`). `Supervisor.WorkerHandle.trade_allowed: bool` (default `True`), set from `msg.trade_allowed` on every slave `StatusMsg` (in `_dispatch_slave`) and every master `StatusMsg` (in `_read_master`, which also sets `h.got_status = True`). Task 3's accessors read these.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_messages.py` (extend the existing status round-trip rather than duplicate — modify `test_status_symbolinfo_recovery_round_trip`'s first block):

```python
def test_status_round_trip_carries_trade_allowed():
    st = M.StatusMsg(source_id="s1", role="slave", connected=True, login=123,
                    balance=1000.0, equity=1000.0, currency="USD", server="Demo",
                    trade_allowed=False)
    rt = M.decode(M.encode(st))
    assert rt.trade_allowed is False
    # default True when unset
    st2 = M.StatusMsg(source_id="s2", role="slave", connected=True, login=1,
                     balance=0.0, equity=0.0, currency="USD", server="Demo")
    assert M.decode(M.encode(st2)).trade_allowed is True
```

Append to `manager/tests/test_mt5_worker.py`:

```python
def test_status_carries_trade_allowed_from_adapter():
    from manager.worker.mt5_worker import _status
    from manager.worker.mt5_adapter import FakeMt5
    acc = {"login": 1, "balance": 0.0, "equity": 0.0, "currency": "USD", "server": "Demo"}
    assert _status(FakeMt5(account=acc), "s1", "slave", True).trade_allowed is True
    assert _status(FakeMt5(account=acc, terminal_info={"trade_allowed": False}),
                   "s1", "slave", True).trade_allowed is False
```

Append to `manager/tests/test_supervisor.py`:

```python
def test_slave_status_records_trade_allowed_on_handle():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    state = dict(_slave_state())
    state["terminal_info"] = {"trade_allowed": False}
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake", fake_state=state)
    try:
        _tick_until(sup, lambda: sup._handles["s1"].got_status)
        assert sup._handles["s1"].trade_allowed is False
    finally:
        sup.shutdown()


def test_master_status_records_trade_allowed_on_handle():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    master_state = {
        "positions": [], "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"},
        "terminal_info": {"trade_allowed": False}}
    sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 60000},
                     adapter_kind="fake", fake_state=master_state)
    try:
        _tick_until(sup, lambda: sup._handles["master"].got_status)
        assert sup._handles["master"].trade_allowed is False
    finally:
        sup.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest manager/tests/test_messages.py::test_status_round_trip_carries_trade_allowed manager/tests/test_mt5_worker.py::test_status_carries_trade_allowed_from_adapter manager/tests/test_supervisor.py::test_slave_status_records_trade_allowed_on_handle manager/tests/test_supervisor.py::test_master_status_records_trade_allowed_on_handle -v`
Expected: FAIL (missing `trade_allowed` field / `got_status` never set for master).

- [ ] **Step 3: Add `trade_allowed` to `StatusMsg`**

In `manager/ipc/messages.py`, update `StatusMsg` (lines 73-83) to add the field after `server`:

```python
@dataclass(frozen=True)
class StatusMsg:
    source_id: str
    role: str          # "master" | "slave"
    connected: bool
    login: int
    balance: float
    equity: float
    currency: str
    server: str
    trade_allowed: bool = True
    KIND = "status"
```

No change to `encode`/`decode` — the generic scalar dump (`for f in fields(msg)`) and the generic scalar reconstruct (`kwargs = {f.name: d[f.name] for f in fields(cls)}`) handle the new field automatically.

- [ ] **Step 4: Populate `trade_allowed` in worker `_status`**

In `manager/worker/mt5_worker.py`, update `_status` (lines 74-80):

```python
def _status(adapter, source_id: str, role: str, connected: bool) -> StatusMsg:
    acc = adapter.account_info()
    ti = adapter.terminal_info()
    return StatusMsg(source_id=source_id, role=role, connected=connected,
                    login=int(acc.get("login", 0)), balance=float(acc.get("balance", 0.0)),
                    equity=float(acc.get("equity", 0.0)),
                    currency=str(acc.get("currency", "")),
                    server=str(acc.get("server", "")),
                    trade_allowed=bool(ti.get("trade_allowed", True)))
```

- [ ] **Step 5: Add `trade_allowed` to `WorkerHandle`**

In `manager/supervisor.py`, add the field to `WorkerHandle` (after `fatal`, line 33):

```python
    fatal: bool = False  # set on a fatal ErrorMsg; _health_check won't restart
    trade_allowed: bool = True  # from the worker's initial StatusMsg (Algo Trading)
    first_msg_seen: bool = False  # False until first message -> grace window
```

- [ ] **Step 6: Record `trade_allowed` on slave status in `_dispatch_slave`**

In `manager/supervisor.py` `_dispatch_slave` (the `elif isinstance(msg, StatusMsg):` branch, lines 173-174):

```python
        elif isinstance(msg, StatusMsg):
            if h is not None:
                h.trade_allowed = msg.trade_allowed
            self._engine.apply_status(slave_id, msg)
```

- [ ] **Step 7: Record `trade_allowed` + `got_status` on master status in `_read_master`**

In `manager/supervisor.py` `_read_master` (the `elif isinstance(msg, StatusMsg):` branch, lines 227-228 — currently `pass`):

```python
            elif isinstance(msg, StatusMsg):
                h.got_status = True
                h.trade_allowed = msg.trade_allowed
```

- [ ] **Step 8: Run the new tests to verify they pass**

Run: `pytest manager/tests/test_messages.py::test_status_round_trip_carries_trade_allowed manager/tests/test_mt5_worker.py::test_status_carries_trade_allowed_from_adapter manager/tests/test_supervisor.py::test_slave_status_records_trade_allowed_on_handle manager/tests/test_supervisor.py::test_master_status_records_trade_allowed_on_handle -v`
Expected: PASS

- [ ] **Step 9: Run the supervisor + worker + messages suites for regressions**

Run: `pytest manager/tests/test_supervisor.py manager/tests/test_mt5_worker.py manager/tests/test_messages.py manager/tests/test_mt5_adapter.py -q`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add manager/ipc/messages.py manager/worker/mt5_worker.py manager/supervisor.py manager/tests/test_messages.py manager/tests/test_mt5_worker.py manager/tests/test_supervisor.py
git commit -m "feat(ipc): carry trade_allowed in StatusMsg; supervisor records it" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Supervisor preflight accessors + `wait_for_master_status`

**Files:**
- Modify: `manager/supervisor.py:86-108` (add accessors near `slave_ready`/`wait_for_slaves_ready`)
- Test: `manager/tests/test_supervisor.py`

**Interfaces:**
- Produces: `Supervisor.slave_trade_allowed(slave_id: str) -> bool` (reads `h.trade_allowed`, False if no handle). `Supervisor.master_trade_allowed() -> bool` (reads the master handle's `trade_allowed`, False if no master). `Supervisor.wait_for_master_status(timeout: float = 90.0) -> bool` (ticks until the master handle's `got_status` is True or timeout, returns `got_status`). Task 4's controller consumes all three.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_supervisor.py`:

```python
def test_slave_trade_allowed_reads_handle():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    assert sup.slave_trade_allowed("s1") is False  # no handle -> False
    state = dict(_slave_state())
    state["terminal_info"] = {"trade_allowed": False}
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake", fake_state=state)
    try:
        _tick_until(sup, lambda: sup._handles["s1"].got_status)
        assert sup.slave_trade_allowed("s1") is False
    finally:
        sup.shutdown()


def test_master_trade_allowed_and_wait_for_master_status():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    assert sup.master_trade_allowed() is False  # no master -> False
    master_state = {
        "positions": [], "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 60000},
                     adapter_kind="fake", fake_state=master_state)
    try:
        assert sup.wait_for_master_status(timeout=5.0) is True
        assert sup._handles["master"].got_status is True
        assert sup.master_trade_allowed() is True
    finally:
        sup.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest manager/tests/test_supervisor.py::test_slave_trade_allowed_reads_handle manager/tests/test_supervisor.py::test_master_trade_allowed_and_wait_for_master_status -v`
Expected: FAIL (`AttributeError: ... has no attribute 'slave_trade_allowed'`).

- [ ] **Step 3: Add the accessors and `wait_for_master_status`**

In `manager/supervisor.py`, add after `wait_for_slaves_ready` (after line 108):

```python
    def slave_trade_allowed(self, slave_id) -> bool:
        """True iff this slave's terminal reports Algo Trading enabled
        (mt5.terminal_info().trade_allowed). False if the handle is missing.
        Read AFTER wait_for_slaves_ready so got_status implies trade_allowed
        has been set from the worker's initial StatusMsg."""
        h = self._handles.get(slave_id)
        return bool(h.trade_allowed) if h is not None else False

    def master_trade_allowed(self) -> bool:
        """True iff the master terminal reports Algo Trading enabled. False if
        no master has been spawned (or it hasn't reported status yet)."""
        h = self._handles.get("master")
        return bool(h.trade_allowed) if h is not None else False

    def wait_for_master_status(self, timeout: float = 90.0) -> bool:
        """Tick until the master handle has reported its first StatusMsg
        (which carries trade_allowed) or the timeout elapses. Call AFTER
        spawn_master and BEFORE sup.start() so the controller can preflight
        Algo Trading before the copy loop runs. Returns got_status."""
        h = self._handles.get("master")
        if h is None:
            return False
        deadline = self._time_fn() + timeout
        while self._time_fn() < deadline:
            self.tick(timeout=0.02)
            if h.got_status:
                return True
        return h.got_status
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest manager/tests/test_supervisor.py::test_slave_trade_allowed_reads_handle manager/tests/test_supervisor.py::test_master_trade_allowed_and_wait_for_master_status -v`
Expected: PASS

- [ ] **Step 5: Run the supervisor suite for regressions**

Run: `pytest manager/tests/test_supervisor.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/supervisor.py manager/tests/test_supervisor.py
git commit -m "feat(supervisor): add trade_allowed accessors + wait_for_master_status" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Controller preflight — `AlgoTradingDisabledError` + slave + master block

**Files:**
- Modify: `manager/app/controller.py:14-17` (exception), `:156-200` (`start`)
- Test: `manager/tests/test_controller.py`

**Interfaces:**
- Produces: `manager.app.controller.AlgoTradingDisabledError(ControllerError)` with `.terminals: list[str]`. `start()` raises it (after shutting down workers) when any slave (post-readiness-gate) or the master reports `trade_allowed=False`. The GUI (Task 5) catches this specific exception.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_controller.py` (note the existing import line already imports `ControllerError`; add `AlgoTradingDisabledError` there in Step 3). Append:

```python
def test_start_blocks_when_slave_algo_trading_disabled():
    """A slave reporting trade_allowed=False blocks Start before the master is
    spawned: AlgoTradingDisabledError is raised, no 'ready'/'copying started'
    status, and the supervisor is shut down (is_running False)."""
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, statuses, logs = _controller(insts)
    slave_state = dict(_slave_state())
    slave_state["terminal_info"] = {"trade_allowed": False}
    with pytest.raises(AlgoTradingDisabledError) as ei:
        c.start(_master(), [_slave()],
                master_fake_state={
                    "positions": [], "symbol_infos": {"EURUSD": SI},
                    "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                                "currency": "USD", "server": "Demo"}},
                slave_fake_state=slave_state)
    assert ei.value.terminals, "error must name the offending terminal(s)"
    assert not any(s.kind == "ready" for s in statuses), "must not reach master"
    assert not any(s.kind == "info" and "copying started" in s.message
                   for s in statuses)
    assert not c.is_running()


def test_start_blocks_when_master_algo_trading_disabled():
    """Slaves ready (Algo Trading on) but master reports trade_allowed=False:
    Start blocks AFTER spawn_master but BEFORE the copy loop runs."""
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, statuses, logs = _controller(insts)
    master_state = {
        "positions": [], "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"},
        "terminal_info": {"trade_allowed": False}}
    with pytest.raises(AlgoTradingDisabledError):
        c.start(_master(), [_slave()],
                master_fake_state=master_state,
                slave_fake_state=_slave_state())
    assert not c.is_running()


def test_start_proceeds_when_all_algo_trading_enabled():
    """Sanity: with Algo Trading on everywhere (FakeMt5 default), Start proceeds
    exactly as before — no false block."""
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, statuses, logs = _controller(insts)
    master_state = {
        "positions": [Position(42, "EURUSD", BUY, 1.10000, 0.5, 1.095, 1.105,
                               NOW, 0.00001, "")],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    c.start(_master(), [_slave()],
            master_fake_state=master_state, slave_fake_state=_slave_state())
    try:
        assert _tick_until(
            lambda: c._engine._slaves["s1"].table.get(42) is not None
            and c._engine._slaves["s1"].table.get(42).slave_ticket != 0)
        assert any(s.kind == "ready" for s in statuses)
    finally:
        c.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest manager/tests/test_controller.py::test_start_blocks_when_slave_algo_trading_disabled manager/tests/test_controller.py::test_start_blocks_when_master_algo_trading_disabled manager/tests/test_controller.py::test_start_proceeds_when_all_algo_trading_enabled -v`
Expected: FAIL — `NameError: AlgoTradingDisabledError` not defined / tests don't block.

- [ ] **Step 3: Add `AlgoTradingDisabledError`**

In `manager/app/controller.py`, update the import line (line 8 area) is unchanged. Add the exception after `ControllerError` (after line 17):

```python
class AlgoTradingDisabledError(ControllerError):
    """Raised by start() when one or more terminals report Algo Trading
    disabled (mt5.terminal_info().trade_allowed is False). Start is blocked
    before any copy is attempted; the GUI shows a modal message box so the
    user enables the Algo Trading button and retries."""
    def __init__(self, terminals: list[str]):
        self.terminals = list(terminals)
        names = ", ".join(self.terminals)
        super().__init__(
            f"Algo Trading is disabled on: {names}. Enable the 'Algo Trading' "
            f"button in each MetaTrader terminal (toolbar, or Tools -> Options "
            f"-> Expert Advisors -> Allow algorithmic trading), then click Start.")
```

- [ ] **Step 4: Add the slave preflight block in `start()`**

In `manager/app/controller.py` `start()`, find the readiness-gate block (lines 186-193):

```python
        ready = sup.wait_for_slaves_ready(timeout=90.0)
        if not ready:
            self._status("error", "one or more slaves did not become ready")
            sup.shutdown()
            self._supervisor = None
            raise ControllerError("slaves not ready within timeout")
        self._status("ready", "slaves ready; starting master")
```

Replace it with (inserting the Algo-Trading preflight between the gate and the "ready" status):

```python
        ready = sup.wait_for_slaves_ready(timeout=90.0)
        if not ready:
            self._status("error", "one or more slaves did not become ready")
            sup.shutdown()
            self._supervisor = None
            raise ControllerError("slaves not ready within timeout")
        # Algo-Trading preflight (slaves): block before any copy is attempted.
        # A slave with Algo Trading off would silently accept commands whose
        # order_send is blocked (retcode 10030/invalid) — surface it now.
        disabled = [(s.id, cfgs[s.id]["terminal_path"])
                    for s in slaves if not sup.slave_trade_allowed(s.id)]
        if disabled:
            names = [f"{sid} ({path})" for sid, path in disabled]
            self._status("error", "Algo Trading is disabled on: " + ", ".join(names))
            sup.shutdown()
            self._supervisor = None
            raise AlgoTradingDisabledError(names)
        self._status("ready", "slaves ready; starting master")
```

- [ ] **Step 5: Add the master preflight block in `start()`**

In `manager/app/controller.py` `start()`, find the master spawn + `sup.start()` (lines 194-200):

```python
        mcfg = cfgs[master.id]
        sup.spawn_master(mcfg,
                         adapter_kind="real" if master_fake_state is None else "fake",
                         fake_state=master_fake_state)
        sup.start()
        self._supervisor = sup
        self._status("info", "copying started")
```

Replace it with:

```python
        mcfg = cfgs[master.id]
        sup.spawn_master(mcfg,
                         adapter_kind="real" if master_fake_state is None else "fake",
                         fake_state=master_fake_state)
        # Algo-Trading preflight (master): wait for the master's first StatusMsg
        # (it sends status before any snapshot) and require trade_allowed=True.
        if not sup.wait_for_master_status(timeout=90.0):
            self._status("error", "master did not report status in time")
            sup.shutdown()
            self._supervisor = None
            raise ControllerError("master did not report status within timeout")
        if not sup.master_trade_allowed():
            name = f"master ({cfgs[master.id]['terminal_path']})"
            self._status("error", "Algo Trading is disabled on: " + name)
            sup.shutdown()
            self._supervisor = None
            raise AlgoTradingDisabledError([name])
        sup.start()
        self._supervisor = sup
        self._status("info", "copying started")
```

- [ ] **Step 6: Update the test import**

In `manager/tests/test_controller.py`, update the existing import (line 8 area) to add `AlgoTradingDisabledError`:

```python
from manager.app.controller import (
    CopyController, AccountSpec, StatusUpdate, ControllerError,
    AlgoTradingDisabledError,
)
```

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `pytest manager/tests/test_controller.py::test_start_blocks_when_slave_algo_trading_disabled manager/tests/test_controller.py::test_start_blocks_when_master_algo_trading_disabled manager/tests/test_controller.py::test_start_proceeds_when_all_algo_trading_enabled -v`
Expected: PASS

- [ ] **Step 8: Run the full controller suite for regressions**

Run: `pytest manager/tests/test_controller.py -q`
Expected: all PASS (existing tests, including `test_start_runs_readiness_gate_then_master_and_copies` and `test_start_readiness_gate_uses_90s_timeout`, still pass — FakeMt5 defaults `trade_allowed=True`, and the master sends status quickly so `wait_for_master_status` returns fast).

- [ ] **Step 9: Commit**

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(controller): block Start when a terminal has Algo Trading off" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: GUI modal message box for `AlgoTradingDisabledError`

**Files:**
- Modify: `manager/gui/main_window.py:8-12` (import `QMessageBox`), `:14` (import `AlgoTradingDisabledError`), `:323-334` (`_on_start`)
- Test: `manager/tests/test_main_window.py`

**Interfaces:**
- Consumes: `AlgoTradingDisabledError` from Task 4.
- Produces: `_on_start` shows `QMessageBox.warning(self, "Algo Trading disabled", str(exc))` and logs `start blocked: {exc}` when the controller raises `AlgoTradingDisabledError`; other exceptions still log `start failed: {exc}`.

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_main_window.py`:

```python
def test_start_blocked_by_algo_trading_shows_message_box(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui import main_window as mw
    from manager.app.controller import AlgoTradingDisabledError
    c = FakeController()
    c.start = lambda master, slaves, **kw: (_ for _ in ()).throw(
        AlgoTradingDisabledError(["s1 (C:/t/terminal64.exe)"]))
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    shown = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda parent, title, text: shown.append((title, text)) or 0)
    w.start_button.click()
    assert shown, "Algo Trading block must raise a modal message box"
    assert "Algo Trading" in shown[0][0]
    assert "Algo Trading" in w.log_view.toPlainText()
```

- [ ] **Step 2: Run the test to verify it fails**

Run (with the PySide6 venv): `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py::test_start_blocked_by_algo_trading_shows_message_box -v`
Expected: FAIL (`AttributeError: module 'manager.gui.main_window' has no attribute 'QMessageBox'`).

- [ ] **Step 3: Import `QMessageBox` and `AlgoTradingDisabledError`**

In `manager/gui/main_window.py`, update the PySide6 widgets import (lines 8-12) to add `QMessageBox`:

```python
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
    QProgressBar, QMessageBox,
)
```

Update the controller import (line 14) to add `AlgoTradingDisabledError`:

```python
from manager.app.controller import AccountSpec, StatusUpdate, AlgoTradingDisabledError
```

- [ ] **Step 4: Handle `AlgoTradingDisabledError` in `_on_start`**

In `manager/gui/main_window.py` `_on_start` (lines 323-334), split the `except`:

```python
    def _on_start(self):
        terminal_path = self.master_terminal.currentText().strip()
        if not terminal_path:
            self.append_log("select a master terminal first")
            return
        master = AccountSpec(id="master", terminal_path=terminal_path)
        try:
            self._controller.start(master, list(self._slaves))
            self.set_running(True)
            self._save_config()
        except AlgoTradingDisabledError as exc:
            self.append_log(f"start blocked: {exc}")
            QMessageBox.warning(self, "Algo Trading disabled", str(exc))
        except Exception as exc:
            self.append_log(f"start failed: {exc}")
```

- [ ] **Step 5: Run the new test to verify it passes**

Run (with the PySide6 venv): `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py::test_start_blocked_by_algo_trading_shows_message_box -v`
Expected: PASS

- [ ] **Step 6: Run the full GUI suite for regressions**

Run (with the PySide6 venv): `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py manager/tests/test_main_window_updates.py -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "feat(gui): show a modal warning when Start is blocked by Algo Trading" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: README Quick Start + Algo Trading step + Troubleshooting row

**Files:**
- Modify: `README.md` (insert Quick Start after the arch diagram, line 23 area; add a line to Usage step 3; add a Troubleshooting row)
- No test (documentation).

- [ ] **Step 1: Add the Quick Start section**

In `README.md`, insert a new `## Quick Start` section immediately after the `---` that follows the architecture diagram (after line 23, before `## Features`). The exact block:

```markdown
## Quick Start

1. **Install & launch** (end users):
   ```powershell
   irm https://github.com/resname/copy-trades-MT5/releases/latest/download/install.ps1 | iex
   ```
   Then launch **CopyTrades MT5** from the Start Menu (or run `copytrades`). From
   source: `pip install -e .[test]` then `python -m manager` — see
   [Installation](#installation).
2. **Install/log in to terminals**: one MetaTrader 5 terminal per account, each
   logged in to a **DEMO account** (never a real account). Use the manager's
   **Install MetaTrader** button if you need more (choose a custom install path
   per terminal).
3. **Enable Algo Trading on every terminal** ⚠️ — in each MetaTrader terminal,
   click the **Algo Trading** toolbar button so it is ON (or Tools → Options →
   Expert Advisors → *Allow algorithmic trading*). The copier places slave trades
   through the MT5 Python API; if Algo Trading is off, `order_send` is blocked and
   **nothing copies**. The manager refuses to Start until every terminal reports
   Algo Trading enabled.
4. **Select the master terminal** in the manager and **Add Slave…** for each
   slave (per-slave terminal, symbol map, lot sizing, normalization).
5. **Click Start** — the manager connects to every terminal, gates on readiness
   and Algo Trading, then copies opens/modifies/partial-closes/closes from master
   to slaves.
6. Close the window to tray (workers keep running); tray **Quit** for an orderly
   stop. The app auto-checks for updates hourly.

For the full run-through, see [Usage](#usage). For demo setup, see
[`docs/smoke-test.md`](docs/smoke-test.md).
```

- [ ] **Step 2: Add a one-line Algo Trading note to Usage step 3**

In `README.md` Usage step 3 (lines 147-149), append a sentence so the step reads:

```markdown
3. **Master**: in the manager, select the master terminal from the dropdown.
   Click **Start** (the manager connects to that terminal's saved account —
   no login/server/password entered in the manager). **Algo Trading must be
   enabled on every terminal** (see Quick Start step 3) or Start is blocked.
```

- [ ] **Step 3: Add a Troubleshooting row**

In `README.md` Troubleshooting table (after line 257, before the final `---`), add:

```markdown
| Start blocked: "Algo Trading is disabled on: …" / trades don't copy | The Algo Trading toolbar button is off on one or more terminals (the MT5 Python API's `order_send` is blocked) | Enable the **Algo Trading** button in each named terminal (or Tools → Options → Expert Advisors → Allow algorithmic trading), then click Start again |
```

- [ ] **Step 4: Verify the markdown renders sensibly (sanity)**

Run: `grep -n "Quick Start\|Algo Trading" README.md`
Expected: shows the new Quick Start heading, the step 3 line, the Usage note, and the troubleshooting row.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: add Quick Start guide + Algo Trading requirement to README" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Step 1: Run the full non-GUI suite**

Run: `pytest -q`
Expected: all PASS (180 passed, 5 skipped on a headless host — the skip count is unchanged; new non-GUI tests all run).

- [ ] **Step 2: Run the GUI suite in the PySide6 venv**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py manager/tests/test_main_window_updates.py -q`
Expected: all PASS (215 passed with PySide6 overall).

- [ ] **Step 3: Push so the release workflow builds a new version the in-app updater can pull**

```bash
git push origin main
```

- [ ] **Step 4: Confirm the release published**

Run: `& "C:\Program Files\GitHub CLI\gh.exe" release list -L 3`
Expected: a new `v0.1.<n>` release at the top with `manager-latest.whl`.

- [ ] **Step 5: (User) reinstall the wheel in the app venv and verify live**

The user runs the in-app updater (or the one-liner) to pull the new wheel, then confirms: with Algo Trading off on a terminal, Start is blocked with a message box naming it; with Algo Trading on, Start proceeds and copies normally. (Per standing practice, the assistant does live verification itself using the user's installed copytrades program and terminals where possible.)