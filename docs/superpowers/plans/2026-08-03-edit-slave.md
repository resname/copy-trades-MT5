# Edit an Added Slave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user edit a slave's trading parameters after adding it, with edits applying live to future trades only (open trades never modified).

**Architecture:** A new IPC `ReconfigureMsg` carries symbol-map + normalize changes to a running worker; engine-side params (lot sizing, max age, symbol map) update in-process via a new `CopyEngine.update_slave_config`. The `SlaveEditor` dialog gains a `set_spec` to pre-populate from an existing `AccountSpec` with `id`/`terminal_path` locked. `MainWindow` gets an Edit button + double-click that updates `_slaves[row]`, saves config, and — if running — calls `CopyController.apply_slave_edit`, which fans out to the engine + supervisor. Open trades are untouched because `derive_command` routes `MODIFY`/`PARTIAL_CLOSE`/`CLOSE` via the `RecordTable` (ticket linkage + stored open volumes), and only `NEW` reads the live config / mapper.

**Tech Stack:** Python 3.12, PySide6 6.11, pytest, multiprocessing.Pipe IPC.

## Global Constraints

- **TDD:** every task writes the failing test first, runs it to confirm it fails, implements, runs it to confirm it passes, then commits. No implementation before the test.
- **Test runners:** headless suite `python -m pytest -q` (Tasks 1–5, no PySide6); GUI suite `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q` (Tasks 6–7 need PySide6, and the full suite must stay green in both). Per the `gui-tests-need-pyside6-venv` memory, run the app venv before declaring GUI work done.
- **One commit per task**, on branch `feat/edit-slave`. Commit messages start `feat(...)`, `refactor(...)`, etc., and end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- **No placeholders:** every code step contains the actual code to write.
- **Security (unchanged, inherited):** no credentials are involved in this feature — workers still connect to the terminal's saved account; `id`/`terminal_path` are locked (read-only) in the editor, so a slave's identity never changes mid-run.
- **Single-focus:** do not bundle the pre-existing editor numeric-input-validation issue (affects Add too) into this feature.

---

## File Structure

- `manager/ipc/messages.py` — add `ReconfigureMsg` + register in `_REGISTRY`. (Task 1)
- `manager/engine/copy_loop.py` — add `CopyEngine.update_slave_config`. (Task 2)
- `manager/worker/mt5_worker.py` — handle `ReconfigureMsg` in `_slave_loop`. (Task 3)
- `manager/supervisor.py` — add `Supervisor.reconfigure_slave`. (Task 4)
- `manager/app/controller.py` — add `CopyController.apply_slave_edit`. (Task 5)
- `manager/gui/slave_editor.py` — add `SlaveEditor.set_spec` + `edit_slave` module function. (Task 6)
- `manager/gui/main_window.py` — add Edit button + double-click + `_on_edit_slave`. (Task 7)
- Tests (one per task, appended to the matching existing test file):
  - `manager/tests/test_messages.py` (Task 1) — **create** (new file; messages currently has no dedicated test file)
  - `manager/tests/test_copy_loop.py` (Task 2)
  - `manager/tests/test_mt5_worker.py` (Task 3)
  - `manager/tests/test_supervisor.py` (Task 4)
  - `manager/tests/test_controller.py` (Task 5)
  - `manager/tests/test_slave_editor.py` (Task 6)
  - `manager/tests/test_main_window.py` (Task 7)

---

### Task 1: IPC `ReconfigureMsg`

**Files:**
- Modify: `manager/ipc/messages.py` (add dataclass after `ErrorMsg`, register in `_REGISTRY`)
- Test: `manager/tests/test_messages.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `ReconfigureMsg(source_id: str, symbol_map_csv: str, normalize_sltp: bool)` with `KIND = "reconfigure"`; round-trips through `encode`/`decode` (scalar-only path — no special-case branch needed, same as `ErrorMsg`).

- [ ] **Step 1: Write the failing test**

Create `manager/tests/test_messages.py`:

```python
from manager.ipc.messages import ReconfigureMsg, encode, decode


def test_reconfigure_msg_round_trips_through_encode_decode():
    msg = ReconfigureMsg(source_id="s1", symbol_map_csv="EURUSD=GBPUSD",
                         normalize_sltp=False)
    d = encode(msg)
    assert d["_kind"] == "reconfigure"
    assert d["source_id"] == "s1"
    assert d["symbol_map_csv"] == "EURUSD=GBPUSD"
    assert d["normalize_sltp"] is False
    back = decode(d)
    assert isinstance(back, ReconfigureMsg)
    assert back.source_id == "s1"
    assert back.symbol_map_csv == "EURUSD=GBPUSD"
    assert back.normalize_sltp is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_messages.py -q`
Expected: FAIL with `ImportError: cannot import name 'ReconfigureMsg'`.

- [ ] **Step 3: Implement `ReconfigureMsg` + register it**

In `manager/ipc/messages.py`, add after the `ErrorMsg` class (before `_REGISTRY`):

```python
@dataclass(frozen=True)
class ReconfigureMsg:
    """Manager -> slave: live-update the worker's symbol map and normalize-SL/TP
    flag without restarting it. The worker re-reports SymbolInfoMsg for the new
    map's slave symbols. Open positions are unaffected (MODIFY/CLOSE route by
    slave_ticket, not the map)."""
    source_id: str
    symbol_map_csv: str
    normalize_sltp: bool
    KIND = "reconfigure"
```

In `_REGISTRY`, add the entry:

```python
    "reconfigure": ReconfigureMsg,
```

No changes to `encode`/`decode` — `ReconfigureMsg` has only scalar fields, so it uses the existing default field-dump encode path and the scalar-only decode path (same as `ErrorMsg`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_messages.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add manager/ipc/messages.py manager/tests/test_messages.py
git commit -m "feat(ipc): add ReconfigureMsg for live slave reconfigure

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `CopyEngine.update_slave_config` (+ open-trade safety)

**Files:**
- Modify: `manager/engine/copy_loop.py` (add method to `CopyEngine`)
- Test: `manager/tests/test_copy_loop.py` (append)

**Interfaces:**
- Consumes: `SymbolMapper` (already imported in `copy_loop.py` via `from manager.engine.transform import SymbolMapper`).
- Produces: `CopyEngine.update_slave_config(slave_id, *, step_amount, step_size, max_lot, max_trade_age_minutes, symbol_map_csv, normalize_sltp) -> bool` (returns whether `symbol_map_csv` changed, so the caller can decide whether to ask the worker to re-report symbol info). `SlaveConfig` is a mutable `@dataclass` (not frozen), so fields are assigned in place.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_copy_loop.py`:

```python
def test_update_slave_config_updates_fields_without_rebuilding_mapper():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    changed = eng.update_slave_config(
        "s1", step_amount=200.0, step_size=0.02, max_lot=20.0,
        max_trade_age_minutes=5, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=False)
    assert changed is False  # symbol_map_csv unchanged -> no mapper rebuild
    cfg = eng._slaves["s1"].config
    assert cfg.step_amount == 200.0 and cfg.step_size == 0.02
    assert cfg.max_lot == 20.0 and cfg.max_trade_age_minutes == 5
    assert cfg.normalize_sltp is False


def test_update_slave_config_rebuilds_mapper_when_map_changes():
    eng = _engine(infos={"s1": {"EURUSD": SI, "GBPUSD": SI}})
    changed = eng.update_slave_config(
        "s1", step_amount=100.0, step_size=0.01, max_lot=10.0,
        max_trade_age_minutes=10, symbol_map_csv="EURUSD=GBPUSD",
        normalize_sltp=True)
    assert changed is True
    # master EURUSD now resolves to slave GBPUSD on the next NEW
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "OPEN"
    assert cmds[0].symbol == "GBPUSD"


def test_update_slave_config_new_open_uses_new_lots():
    eng = _engine(infos={"s1": {"EURUSD": SI}})  # balance 1000
    eng.update_slave_config(
        "s1", step_amount=500.0, step_size=0.02, max_lot=99.0,
        max_trade_age_minutes=10, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=True)
    cmds = eng.ingest_snapshot(_snap([_pos(99)]), now=NOW)["s1"]
    # steps=floor(1000/500)=2; lots=2*0.02=0.04 (volume_step 0.01)
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.04, abs=1e-8)


def test_update_slave_config_does_not_affect_open_trades():
    """An edit must not alter MODIFY/PARTIAL_CLOSE/CLOSE for an already-open
    position: those route via the RecordTable (slave_ticket + stored open
    volumes), not the live config. Only NEW reads the config/mapper."""
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # establish prev (NEW skipped, in table)
    # edit everything the engine holds
    eng.update_slave_config(
        "s1", step_amount=999.0, step_size=0.5, max_lot=99.0,
        max_trade_age_minutes=1, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=False)
    # MODIFY on the open position: still routed to slave_ticket 777
    cmds = eng.ingest_snapshot(
        _snap([_pos(42, sl=1.09000, tp=1.11000)]), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "MODIFY"
    assert cmds[0].slave_ticket == 777
    # PARTIAL_CLOSE uses stored open volumes (0.5 / 0.10), NOT the new step params
    cmds2 = eng.ingest_snapshot(_snap([_pos(42, volume=0.30)]), now=NOW)["s1"]
    assert cmds2[0].action == "PARTIAL_CLOSE"
    assert cmds2[0].master_open_volume == 0.5 and cmds2[0].slave_open_volume == 0.10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_copy_loop.py -k update_slave_config -q`
Expected: FAIL with `AttributeError: 'CopyEngine' object has no attribute 'update_slave_config'`.

- [ ] **Step 3: Implement `update_slave_config`**

In `manager/engine/copy_loop.py`, add this method to `CopyEngine` (after `reset_slave`, before `ingest_snapshot`):

```python
    def update_slave_config(self, slave_id: str, *, step_amount: float,
                            step_size: float, max_lot: float,
                            max_trade_age_minutes: int,
                            symbol_map_csv: str,
                            normalize_sltp: bool) -> bool:
        """Live-update a running slave's config in place. Returns whether
        symbol_map_csv changed (caller may then ask the worker to re-report
        SymbolInfo). Safe for open trades: derive_command routes
        MODIFY/PARTIAL_CLOSE/CLOSE via the RecordTable (slave_ticket + stored
        open volumes), and only NEW reads these fields / the mapper."""
        state = self._slaves[slave_id]
        cfg = state.config
        map_changed = cfg.symbol_map_csv != symbol_map_csv
        cfg.step_amount = step_amount
        cfg.step_size = step_size
        cfg.max_lot = max_lot
        cfg.max_trade_age_minutes = max_trade_age_minutes
        cfg.normalize_sltp = normalize_sltp
        cfg.symbol_map_csv = symbol_map_csv
        if map_changed:
            state.mapper = SymbolMapper(
                symbol_map_csv, lambda s: s in state.symbol_infos)
        return map_changed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_copy_loop.py -q`
Expected: PASS (all copy_loop tests, including the 4 new ones).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/copy_loop.py manager/tests/test_copy_loop.py
git commit -m "feat(engine): live-update slave config (open trades untouched)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Worker handles `ReconfigureMsg`

**Files:**
- Modify: `manager/worker/mt5_worker.py` (`_slave_loop` reconfigure branch; import `ReconfigureMsg`)
- Test: `manager/tests/test_mt5_worker.py` (append)

**Interfaces:**
- Consumes: `ReconfigureMsg` (Task 1), `build_symbol_info_msg` (already in `mt5_worker.py`).
- Produces: `_slave_loop` now responds to a `ReconfigureMsg` by updating its local `normalize` + `symbol_map_csv`, re-emitting a `SymbolInfoMsg` (built from the new map), and continuing the loop. `MODIFY`/`CLOSE` commands already in flight are unaffected (they route by `slave_ticket`).

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_mt5_worker.py`:

```python
def test_slave_loop_reconfigure_re_emits_symbol_info_and_updates_normalize():
    """On ReconfigureMsg the slave loop must (1) re-emit a SymbolInfoMsg for the
    NEW map's slave symbols and (2) apply the new normalize_sltp to subsequent
    commands. Open positions are untouched (MODIFY routes by slave_ticket)."""
    import multiprocessing
    import threading
    from manager.ipc.messages import (
        ReconfigureMsg, CommandMsg, RecoveryMsg, SymbolInfoMsg, StatusMsg, AckMsg,
    )
    from manager.ipc.pipe_framing import send_msg, recv_msg
    from manager.worker.mt5_worker import _slave_loop

    cmt = encode_comment(1, 0.50, 0.10)
    mt = FakeMt5(
        positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 1.095, 1.105, 0,
                             0.00001, comment=cmt)],
        symbol_infos={"EURUSD": SI, "GBPUSD": SI},
        account={"login": 2, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks={"EURUSD": (1.10000, 1.10010), "GBPUSD": (1.30000, 1.30010)},
    )
    cfg = {"slave_id": "s1", "symbol_map_csv": "EURUSD=EURUSD",
           "normalize_sltp": True, "retry_count": 1, "retry_delay_ms": 0,
           "slave_status_interval_ms": 60000}

    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop, args=(child, mt, cfg), daemon=True)
    t.start()
    try:
        # drain init: RecoveryMsg, SymbolInfoMsg, StatusMsg
        init = [recv_msg(parent) for _ in range(3)]
        assert isinstance(init[0], RecoveryMsg)
        assert isinstance(init[1], SymbolInfoMsg) and "EURUSD" in init[1].infos
        assert isinstance(init[2], StatusMsg)

        # reconfigure: new map EURUSD->GBPUSD, normalize OFF
        send_msg(parent, ReconfigureMsg(source_id="s1", symbol_map_csv="EURUSD=GBPUSD",
                                        normalize_sltp=False))
        si = recv_msg(parent)
        assert isinstance(si, SymbolInfoMsg)
        assert set(si.infos.keys()) == {"GBPUSD"}  # new map's slave symbol

        # a MODIFY on the open position with raw master SL/TP: because normalize
        # is now False, the slave applies the RAW sl/tp (no re-centering).
        send_msg(parent, CommandMsg(
            slave_id="s1", action="MODIFY", master_ticket=1, slave_ticket=777,
            sl=1.09400, tp=1.10600, master_open_price=1.10000, side=BUY))
        ack = recv_msg(parent)
        assert isinstance(ack, AckMsg) and ack.ok and ack.action == "MODIFY"
        _status_after = recv_msg(parent)  # loop sends a StatusMsg after each ack
        pos = mt.position_by_ticket(777)
        assert pos.sl == 1.09400 and pos.tp == 1.10600  # raw, not normalized
    finally:
        parent.close()  # -> worker reads EOFError -> graceful return
        t.join(timeout=2.0)
        assert not t.is_alive(), "slave loop must exit when the pipe closes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_mt5_worker.py::test_slave_loop_reconfigure_re_emits_symbol_info_and_updates_normalize -q`
Expected: FAIL (the loop currently treats `ReconfigureMsg` as a `CommandMsg` with an unknown action → `execute_command` returns a failed Ack, no `SymbolInfoMsg` emitted; the test's `recv_msg(parent)` for the `SymbolInfoMsg` raises `EOFError`/hangs when the pipe closes).

- [ ] **Step 3: Implement the reconfigure branch in `_slave_loop`**

In `manager/worker/mt5_worker.py`, add `ReconfigureMsg` to the existing import from `manager.ipc.messages` (currently imports `AckMsg, ErrorMsg, SnapshotMsg, StatusMsg, SymbolInfoMsg, RecoveryMsg`):

```python
from manager.ipc.messages import (
    AckMsg, ErrorMsg, SnapshotMsg, StatusMsg, SymbolInfoMsg, RecoveryMsg,
    ReconfigureMsg,
)
```

In `_slave_loop`, change the `if pipe.poll(poll_timeout):` body so it branches on `ReconfigureMsg` before treating the message as a command. The current body is:

```python
        if pipe.poll(poll_timeout):
            cmd = recv_msg(pipe)  # raises EOFError on manager close
            ack = execute_command(adapter, cmd, normalize, retry_count, retry_delay)
            try:
                send_msg(pipe, ack)
                send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
            except (EOFError, OSError):
                return  # manager gone
            last_status = time.time()
```

Replace it with:

```python
        if pipe.poll(poll_timeout):
            cmd = recv_msg(pipe)  # raises EOFError on manager close
            if isinstance(cmd, ReconfigureMsg):
                # Live reconfigure: update this loop's params and re-report the
                # symbol info for the NEW map's slave symbols. Open positions
                # are unaffected (MODIFY/CLOSE route by slave_ticket). No ack.
                normalize = cmd.normalize_sltp
                symbol_map_csv = cmd.symbol_map_csv
                try:
                    send_msg(pipe, build_symbol_info_msg(adapter, slave_id,
                                                         symbol_map_csv))
                except (EOFError, OSError):
                    return  # manager gone
                last_status = time.time()
                continue
            ack = execute_command(adapter, cmd, normalize, retry_count, retry_delay)
            try:
                send_msg(pipe, ack)
                send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
            except (EOFError, OSError):
                return  # manager gone
            last_status = time.time()
```

(`slave_id`, `normalize`, `symbol_map_csv` are already local variables in `_slave_loop`; `build_symbol_info_msg` is already imported at module top.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_mt5_worker.py -q`
Expected: PASS (all worker tests, including the new one).

- [ ] **Step 5: Commit**

```bash
git add manager/worker/mt5_worker.py manager/tests/test_mt5_worker.py
git commit -m "feat(worker): handle ReconfigureMsg (live map + normalize, no restart)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: `Supervisor.reconfigure_slave`

**Files:**
- Modify: `manager/supervisor.py` (add method; import `ReconfigureMsg`)
- Test: `manager/tests/test_supervisor.py` (append)

**Interfaces:**
- Consumes: `ReconfigureMsg` (Task 1), `_send` (already in `Supervisor`).
- Produces: `Supervisor.reconfigure_slave(slave_id, symbol_map_csv, normalize_sltp) -> None`. Always updates `h.config` (so a later `_restart` spawns with the new params); sends `ReconfigureMsg` only when the handle exists and the pipe is open and the worker is not fatal. No-op when the handle is missing.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_supervisor.py`:

```python
def test_reconfigure_slave_sends_message_and_updates_config():
    from manager.ipc.messages import ReconfigureMsg
    from manager.ipc.pipe_framing import recv_msg
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.0)
    parent, child = multiprocessing.Pipe(duplex=True)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=parent,
        config={"symbol_map_csv": "EURUSD=EURUSD", "normalize_sltp": True,
                "terminal_path": "C:/t/s.exe"},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    sup.reconfigure_slave("s1", "EURUSD=GBPUSD", False)
    msg = recv_msg(parent)
    assert isinstance(msg, ReconfigureMsg)
    assert msg.source_id == "s1"
    assert msg.symbol_map_csv == "EURUSD=GBPUSD"
    assert msg.normalize_sltp is False
    # h.config updated so a later restart spawns with the new params
    assert sup._handles["s1"].config["symbol_map_csv"] == "EURUSD=GBPUSD"
    assert sup._handles["s1"].config["normalize_sltp"] is False
    parent.close()
    child.close()


def test_reconfigure_slave_updates_config_even_when_pipe_gone():
    """A dead (non-fatal) worker has pipe=None and will be restarted by
    _health_check; reconfigure must still update h.config so that restart
    spawns with the new params. The send is skipped (no pipe)."""
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.0)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None,
        config={"symbol_map_csv": "EURUSD=EURUSD", "normalize_sltp": True,
                "terminal_path": "C:/t/s.exe"},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    sup.reconfigure_slave("s1", "EURUSD=GBPUSD", False)  # must not raise
    assert sup._handles["s1"].config["symbol_map_csv"] == "EURUSD=GBPUSD"
    assert sup._handles["s1"].config["normalize_sltp"] is False


def test_reconfigure_slave_noop_when_handle_missing():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.0)
    sup.reconfigure_slave("nonexistent", "x", False)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_supervisor.py -k reconfigure_slave -q`
Expected: FAIL with `AttributeError: 'Supervisor' object has no attribute 'reconfigure_slave'`.

- [ ] **Step 3: Implement `reconfigure_slave`**

In `manager/supervisor.py`, add `ReconfigureMsg` to the import from `manager.ipc.messages` (currently imports `AckMsg, StatusMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, ErrorMsg, StartMsg`):

```python
from manager.ipc.messages import (
    AckMsg, StatusMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, ErrorMsg,
    ReconfigureMsg, StartMsg,
)
```

Add this method to `Supervisor` (after `_send`, before `_health_check`):

```python
    def reconfigure_slave(self, slave_id: str, symbol_map_csv: str,
                          normalize_sltp: bool) -> None:
        """Live-update a running slave's symbol map + normalize flag. Always
        updates h.config so a subsequent _restart spawns with the new params
        (a dead non-fatal worker is restarted by _health_check and picks up the
        edit). Sends the ReconfigureMsg only when the pipe is open and the
        worker is not fatal. No-op when the handle is missing."""
        h = self._handles.get(slave_id)
        if h is None:
            return
        h.config["symbol_map_csv"] = symbol_map_csv
        h.config["normalize_sltp"] = normalize_sltp
        if h.pipe is None or h.fatal:
            return  # worker gone/fatal: can't send, but h.config is updated
        self._send(slave_id, ReconfigureMsg(
            source_id=slave_id, symbol_map_csv=symbol_map_csv,
            normalize_sltp=normalize_sltp))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_supervisor.py -q`
Expected: PASS (all supervisor tests, including the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add manager/supervisor.py manager/tests/test_supervisor.py
git commit -m "feat(supervisor): reconfigure_slave (live update + durable config)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: `CopyController.apply_slave_edit`

**Files:**
- Modify: `manager/app/controller.py` (add method)
- Test: `manager/tests/test_controller.py` (append)

**Interfaces:**
- Consumes: `CopyEngine.update_slave_config` (Task 2), `Supervisor.reconfigure_slave` (Task 4).
- Produces: `CopyController.apply_slave_edit(slave_id, spec: AccountSpec) -> None`. If not running (`_supervisor is None` or `_engine is None`): no-op (the edit is already in the GUI's `_slaves` + saved config; it applies on the next Start via `build_worker_configs`). If running: update the engine config in-process (future opens use new step/size/max_lot/max_age + new symbol map) and send `ReconfigureMsg` to the worker (normalize + symbol-info refresh).

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_controller.py`:

```python
def test_apply_slave_edit_updates_engine_and_reconfigures_when_running():
    """While running, apply_slave_edit updates the engine's SlaveConfig
    (future opens use new params) and calls supervisor.reconfigure_slave
    (pushes normalize + symbol-info refresh to the worker)."""
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, statuses, logs = _controller(insts)
    c.start(_master(), [_slave()],
            master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                            "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    try:
        reconfigured = []
        c._supervisor.reconfigure_slave = \
            lambda sid, csv, norm: reconfigured.append((sid, csv, norm))
        new = AccountSpec(id="s1", terminal_path="C:/s/terminal64.exe",
                          symbol_map_csv="EURUSD=GBPUSD", step_amount=500.0,
                          step_size=0.02, max_lot=20.0,
                          max_trade_age_minutes=5.0, normalize_sltp=False)
        c.apply_slave_edit("s1", new)
        cfg = c._engine._slaves["s1"].config
        assert cfg.step_amount == 500.0
        assert cfg.step_size == 0.02 and cfg.max_lot == 20.0
        assert cfg.max_trade_age_minutes == 5
        assert cfg.symbol_map_csv == "EURUSD=GBPUSD"
        assert cfg.normalize_sltp is False
        assert reconfigured == [("s1", "EURUSD=GBPUSD", False)]
    finally:
        c.stop()


def test_apply_slave_edit_noop_when_not_running():
    c, _, _ = _controller([TerminalInstance("C:/m", "C:/m/terminal64.exe",
                                            "appdata")])
    # must not raise (engine/supervisor are None)
    c.apply_slave_edit("s1", AccountSpec(id="s1",
                       terminal_path="C:/s/terminal64.exe", step_amount=500.0))
    assert c._supervisor is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_controller.py -k apply_slave_edit -q`
Expected: FAIL with `AttributeError: 'CopyController' object has no attribute 'apply_slave_edit'`.

- [ ] **Step 3: Implement `apply_slave_edit`**

In `manager/app/controller.py`, add this method to `CopyController` (after `stop`, before `is_running`):

```python
    def apply_slave_edit(self, slave_id: str, spec: AccountSpec) -> None:
        """Apply an edited slave's trading params live. No-op when not running
        (the GUI has already updated _slaves + saved config; it applies on the
        next Start via build_worker_configs). When running, update the engine
        config in-process (future opens use the new step/size/max_lot/max_age +
        new symbol map) and push normalize + symbol-info-refresh to the worker.
        Open trades are never modified: derive_command routes MODIFY/PARTIAL/
        CLOSE via the RecordTable."""
        if self._supervisor is None or self._engine is None:
            return
        self._engine.update_slave_config(
            slave_id, step_amount=spec.step_amount, step_size=spec.step_size,
            max_lot=spec.max_lot, max_trade_age_minutes=spec.max_trade_age_minutes,
            symbol_map_csv=spec.symbol_map_csv, normalize_sltp=spec.normalize_sltp)
        self._supervisor.reconfigure_slave(
            slave_id, spec.symbol_map_csv, spec.normalize_sltp)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_controller.py -q`
Expected: PASS (all controller tests, including the 2 new ones).

- [ ] **Step 5: Commit**

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(controller): apply_slave_edit (live, open trades untouched)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: `SlaveEditor.set_spec` + `edit_slave`

**Files:**
- Modify: `manager/gui/slave_editor.py` (add `set_spec` method, `edit_slave` function, import `parse_symbol_map`)
- Test: `manager/tests/test_slave_editor.py` (append)

**Interfaces:**
- Consumes: `parse_symbol_map` from `manager.engine.transform`; existing `SlaveEditor` widgets + `spec()`.
- Produces:
  - `SlaveEditor.set_spec(spec: AccountSpec, *, lock_identity: bool = True) -> None` — pre-populates all fields from an existing spec; when `lock_identity`, sets `id_edit` read-only and disables `terminal`; parses `symbol_map_csv` back into the symbol table; sets the window title to `"Edit Slave"`.
  - `edit_slave(parent_window, spec: AccountSpec) -> AccountSpec | None` — opens `SlaveEditor` pre-populated with `spec` (identity locked); returns the edited `AccountSpec` or `None` on cancel.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_slave_editor.py`:

```python
def test_set_spec_pre_populates_and_locks_identity(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController([_inst("C:/s1/terminal64.exe")]))
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD",
                       step_amount=500.0, step_size=0.02, max_lot=20.0,
                       max_trade_age_minutes=5.0, normalize_sltp=False)
    dlg.set_spec(spec, lock_identity=True)
    assert dlg.windowTitle() == "Edit Slave"
    assert dlg.id_edit.text() == "s1"
    assert dlg.id_edit.isReadOnly()
    assert dlg.terminal.currentText() == "C:/s1/terminal64.exe"
    assert not dlg.terminal.isEnabled()
    # symbol table has one row per mapped pair
    assert dlg.symbol_table.rowCount() == 2
    assert dlg.symbol_table.item(0, 0).text() == "EURUSD"
    assert dlg.symbol_table.item(0, 1).text() == "EURUSD"
    assert dlg.symbol_table.item(1, 0).text() == "GBPUSD"
    assert dlg.symbol_table.item(1, 1).text() == "GBPUSD"
    assert dlg.step_amount.text() == "500.0"
    assert dlg.step_size.text() == "0.02"
    assert dlg.max_lot.text() == "20.0"
    assert dlg.max_trade_age_minutes.text() == "5.0"
    assert dlg.normalize_sltp.isChecked() is False


def test_set_spec_round_trips_through_spec(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    spec = AccountSpec(id="s2", terminal_path="C:/s2/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD",
                       step_amount=100.0, step_size=0.01, max_lot=10.0,
                       max_trade_age_minutes=10.0, normalize_sltp=True)
    dlg = SlaveEditor(FakeController([_inst("C:/s2/terminal64.exe")]))
    dlg.set_spec(spec, lock_identity=True)
    dlg.accept()
    out = dlg.spec()
    assert out.id == "s2"
    assert out.terminal_path == "C:/s2/terminal64.exe"
    assert out.symbol_map_csv == "EURUSD=EURUSD,GBPUSD=GBPUSD"
    assert out.step_amount == 100.0 and out.step_size == 0.01
    assert out.max_lot == 10.0 and out.max_trade_age_minutes == 10.0
    assert out.normalize_sltp is True


def test_edit_slave_pre_populates_with_locked_identity(qapp, monkeypatch):
    """edit_slave opens a SlaveEditor pre-populated with `spec` and returns the
    edited spec. We avoid the real modal loop by patching exec to accept
    immediately, so spec() reflects the pre-populated (unchanged) values and
    identity stays locked."""
    from manager.gui.slave_editor import edit_slave, SlaveEditor
    from manager.app.controller import AccountSpec
    from PySide6.QtWidgets import QDialog

    def stub_exec(self):
        self.setResult(QDialog.DialogCode.Accepted)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SlaveEditor, "exec", stub_exec)

    class _Win:
        def __init__(self):
            self._controller = FakeController([_inst("C:/s1/terminal64.exe")])

    win = _Win()
    orig = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD", step_amount=100.0,
                       step_size=0.01, max_lot=10.0, max_trade_age_minutes=10.0,
                       normalize_sltp=True)
    out = edit_slave(win, orig)
    assert out is not None
    # identity locked (unchanged), trading params round-trip from set_spec
    assert out.id == "s1"
    assert out.terminal_path == "C:/s1/terminal64.exe"
    assert out.symbol_map_csv == "EURUSD=EURUSD"
    assert out.step_amount == 100.0 and out.normalize_sltp is True
```

(Note: the third test patches `SlaveEditor.exec` to accept immediately, avoiding a real modal loop; `set_spec` runs before `exec` inside `edit_slave`, so `spec()` returns the pre-populated values.)

- [ ] **Step 2: Run tests to verify they fail**

Run GUI suite: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_slave_editor.py -k "set_spec or edit_slave" -q`
Expected: FAIL with `AttributeError: 'SlaveEditor' object has no attribute 'set_spec'` / `ImportError: cannot import name 'edit_slave'`.

- [ ] **Step 3: Implement `set_spec` + `edit_slave`**

In `manager/gui/slave_editor.py`, add `parse_symbol_map` to the imports (add a new import line near the top, after the existing imports):

```python
from manager.engine.transform import parse_symbol_map
```

Add the `set_spec` method to `SlaveEditor` (after `_spec_from_fields`, before `spec`):

```python
    def set_spec(self, spec: AccountSpec, *, lock_identity: bool = True) -> None:
        """Pre-populate the editor from an existing AccountSpec (edit mode).
        When lock_identity is True, the slave id and terminal path are shown
        read-only/disabled so the slave's identity cannot change mid-edit."""
        self.setWindowTitle("Edit Slave")
        self.id_edit.setText(spec.id)
        if lock_identity:
            self.id_edit.setReadOnly(True)
        if spec.terminal_path:
            if self.terminal.findText(spec.terminal_path) < 0:
                self.terminal.addItem(spec.terminal_path)
            self.terminal.setCurrentText(spec.terminal_path)
        if lock_identity:
            self.terminal.setEnabled(False)
        self.symbol_table.setRowCount(0)
        for master, slave in parse_symbol_map(spec.symbol_map_csv).items():
            r = self.symbol_table.rowCount()
            self.symbol_table.insertRow(r)
            self.symbol_table.setItem(r, 0, QTableWidgetItem(master))
            self.symbol_table.setItem(r, 1, QTableWidgetItem(slave))
        self.step_amount.setText(str(spec.step_amount))
        self.step_size.setText(str(spec.step_size))
        self.max_lot.setText(str(spec.max_lot))
        self.max_trade_age_minutes.setText(str(spec.max_trade_age_minutes))
        self.normalize_sltp.setChecked(spec.normalize_sltp)
```

Add the `edit_slave` module function after the existing `add_slave` function at the end of the file:

```python
def edit_slave(parent_window, spec: AccountSpec) -> AccountSpec | None:
    """Open the SlaveEditor modally, pre-populated with `spec` (identity
    locked). Returns the edited AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, parent=parent_window)
    dlg.set_spec(spec, lock_identity=True)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run GUI suite: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_slave_editor.py -q`
Expected: PASS (all slave_editor tests, including the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add manager/gui/slave_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): SlaveEditor.set_spec + edit_slave (pre-populate, lock id)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: `MainWindow` Edit button + double-click + `_on_edit_slave`

**Files:**
- Modify: `manager/gui/main_window.py` (add Edit button + selection-enable + double-click wiring + handler)
- Test: `manager/tests/test_main_window.py` (append; extend `FakeController` with `apply_slave_edit`)

**Interfaces:**
- Consumes: `edit_slave` (Task 6), `CopyController.apply_slave_edit` (Task 5), existing `_slaves`/`slave_list`/`_save_config`.
- Produces: an **"Edit Slave…"** button (initially disabled; enabled when a list row is selected), **double-click** on a list row opening the editor, and `_on_edit_slave` which updates `_slaves[row]` + the row label, saves config, and — if running — calls `controller.apply_slave_edit(new.id, new)`.

- [ ] **Step 1: Write the failing tests**

In `manager/tests/test_main_window.py`, extend `FakeController` (add the `apply_slave_edit` method used by the running-edit test and the no-row tests):

```python
class FakeController:
    """Minimal controller double for construction + wiring smoke tests."""
    def __init__(self):
        self.started = False
        self.stopped = False
        self._instances = []
        self.applied_edits = []
    def discover_instances(self):
        return self._instances
    def start(self, master, slaves, **kw):
        self.started = True
        self.last_master = master
    def stop(self):
        self.stopped = True
    def is_running(self):
        return self.started and not self.stopped
    def apply_slave_edit(self, slave_id, spec):
        self.applied_edits.append((slave_id, spec))
```

Append these tests:

```python
def test_edit_button_present_and_disabled_with_no_selection(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.edit_slave_button.text().startswith("Edit Slave")
    assert not w.edit_slave_button.isEnabled()


def test_edit_button_enabled_when_row_selected(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    w = MainWindow(FakeController())
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s/terminal64.exe")]
    w.slave_list.addItem("s1: terminal64.exe")
    w.slave_list.setCurrentRow(0)
    assert w.edit_slave_button.isEnabled()
    w.slave_list.setCurrentRow(-1)
    assert not w.edit_slave_button.isEnabled()


def test_on_edit_slave_updates_row_label_and_saves_config(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    import manager.gui.slave_editor as se
    store = SettingsStore(path=tmp_path / "settings.json")
    c = FakeController()
    w = MainWindow(c, store=store)
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             symbol_map_csv="EURUSD=EURUSD", step_amount=100.0)]
    w.slave_list.addItem("s1: terminal64.exe")
    w.slave_list.setCurrentRow(0)
    edited = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                         symbol_map_csv="EURUSD=EURUSD", step_amount=200.0)
    monkeypatch.setattr(se, "edit_slave", lambda parent, spec: edited)
    w._on_edit_slave()
    assert w._slaves[0].step_amount == 200.0
    assert w.slave_list.item(0).text() == "s1: terminal64.exe"
    cfg = store.load_config()
    assert cfg["slaves"][0]["step_amount"] == 200.0


def test_on_edit_slave_cancel_is_noop(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    import manager.gui.slave_editor as se
    w = MainWindow(FakeController())
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0)]
    w.slave_list.addItem("s1: terminal64.exe")
    w.slave_list.setCurrentRow(0)
    monkeypatch.setattr(se, "edit_slave", lambda parent, spec: None)  # cancel
    w._on_edit_slave()
    assert w._slaves[0].step_amount == 100.0  # unchanged


def test_on_edit_slave_applies_live_when_running(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    import manager.gui.slave_editor as se
    c = FakeController()
    c.started = True  # is_running() -> True
    w = MainWindow(c)
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0)]
    w.slave_list.addItem("s1: terminal64.exe")
    w.slave_list.setCurrentRow(0)
    edited = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                          step_amount=200.0)
    monkeypatch.setattr(se, "edit_slave", lambda parent, spec: edited)
    w._on_edit_slave()
    assert c.applied_edits == [("s1", edited)]


def test_on_edit_slave_no_selection_is_noop(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._on_edit_slave()  # no row -> must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run GUI suite: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py -k "edit_button or edit_slave" -q`
Expected: FAIL with `AttributeError: 'MainWindow' object has no attribute 'edit_slave_button'` / `'_on_edit_slave'`.

- [ ] **Step 3: Implement the Edit button + handler in `MainWindow`**

In `manager/gui/main_window.py`, in `_build_ui`, in the **Slave list** section, add the Edit button to the existing button row. The current code is:

```python
        self.add_slave_button = QPushButton("Add Slave…")
        self.remove_slave_button = QPushButton("Remove Slave")
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        slave_box.setLayout(sl)
```

Replace it with:

```python
        self.add_slave_button = QPushButton("Add Slave…")
        self.remove_slave_button = QPushButton("Remove Slave")
        self.edit_slave_button = QPushButton("Edit Slave…")
        self.edit_slave_button.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        row.addWidget(self.edit_slave_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        slave_box.setLayout(sl)
```

In the **wire buttons** section, add the edit + double-click + selection wiring. The current section is:

```python
        self.add_slave_button.clicked.connect(self._on_add_slave)
        self.remove_slave_button.clicked.connect(self._on_remove_slave)
```

Replace it with:

```python
        self.add_slave_button.clicked.connect(self._on_add_slave)
        self.remove_slave_button.clicked.connect(self._on_remove_slave)
        self.edit_slave_button.clicked.connect(self._on_edit_slave)
        self.slave_list.itemDoubleClicked.connect(
            lambda _item: self._on_edit_slave())
        self.slave_list.itemSelectionChanged.connect(self._update_edit_enabled)
```

Add the `_update_edit_enabled` and `_on_edit_slave` handlers. Place them right after `_on_remove_slave`:

```python
    def _update_edit_enabled(self) -> None:
        self.edit_slave_button.setEnabled(self.slave_list.currentRow() >= 0)

    def _on_edit_slave(self, *_args) -> None:
        row = self.slave_list.currentRow()
        if row < 0:
            return
        from manager.gui.slave_editor import edit_slave
        try:
            new = edit_slave(self, self._slaves[row])
        except Exception as exc:
            self.append_log(f"edit failed: {exc}")
            return
        if new is None:
            return
        self._slaves[row] = new
        label = (new.terminal_path or new.id).replace("\\", "/").rstrip("/") \
            .rsplit("/", 1)[-1]
        item = self.slave_list.item(row)
        if item is not None:
            item.setText(f"{new.id}: {label}")
        self._save_config()
        if self._controller.is_running():
            self._controller.apply_slave_edit(new.id, new)
```

- [ ] **Step 4: Run tests to verify they pass**

Run GUI suite: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py -q`
Expected: PASS (all main_window tests, including the 6 new ones).

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "feat(gui): edit-an-added-slave button + double-click + live apply

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Final verification

After Task 7:

- [ ] **Run the full headless suite:** `python -m pytest -q` — all green (Tasks 1–5 tests + the rest).
- [ ] **Run the full GUI suite (PySide6 venv):** `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q` — all green (Tasks 6–7 + the rest).
- [ ] **Self-review the diff** for dead imports, unused variables, and that no `id`/`terminal_path` mutation path was introduced.

## Self-review notes (plan author)

- **Spec coverage:** every spec component has a task — `ReconfigureMsg` (T1), `update_slave_config` (T2), worker handler (T3), `reconfigure_slave` (T4), `apply_slave_edit` (T5), `set_spec`/`edit_slave` (T6), Edit button/handler (T7). Open-trade safety is a dedicated test in T2 and restated in T3/T5.
- **Type consistency:** `update_slave_config` keyword args match across T2 (definition), T5 (caller). `reconfigure_slave(slave_id, symbol_map_csv, normalize_sltp)` matches across T4 (definition), T5 (caller). `apply_slave_edit(slave_id, spec)` matches across T5 (definition), T7 (caller). `set_spec(spec, *, lock_identity=True)` matches T6 definition + `edit_slave` caller. `edit_slave(parent_window, spec)` matches T6 definition + T7 caller (lazy import).
- **Edge cases from spec:** not-running no-op (T5 test), no-selection no-op (T7 test), worker dead/fatal config-durability (T4 test), brand-new symbol via re-reported SymbolInfo (T3 test asserts GBPUSD info flows), invalid numeric input handled by `try/except` in `_on_edit_slave` (T7; pre-existing validation issue deliberately not bundled), identity locked (T6 tests), modal reentrancy (dialog `exec()`).
- **Refinement vs spec:** `reconfigure_slave` updates `h.config` even when the pipe is gone (so a dead non-fatal worker's restart picks up the edit), only skipping the *send* — this matches the spec's edge-case bullet ("no-op send ... h.config ... updated") and is verified by `test_reconfigure_slave_updates_config_even_when_pipe_gone`.