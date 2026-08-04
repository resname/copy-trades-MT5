# Startup / Restart Patience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each worker a 90-second grace window after (re)spawn to deliver its first message/snapshot before any impatient timeout/restart fires, then revert to the existing tight steady-state thresholds.

**Architecture:** A per-worker `first_msg_seen` flag widens the stale-restart threshold from `stale_seconds` (30s) to `STARTUP_GRACE_SECONDS` (90s) until the first message arrives; a supervisor-level `_master_first_snapshot_seen` flag does the same for the master heartbeat warning (10s → 90s) until the first `SnapshotMsg`. Both flags reset on (re)spawn (`spawn_master` / `_restart`), so restarts get a fresh window. The controller's one-shot slave-readiness gate is widened from 15s to 90s.

**Tech Stack:** Python 3, `multiprocessing.Pipe` IPC, `dataclasses`, `pytest` with injected `time_fn` clocks. Pure-Python fake MT5 adapter drives deterministic tests (no real terminal).

## Global Constraints

- Grace length is **90 seconds**, hardcoded as `Supervisor.STARTUP_GRACE_SECONDS = 90.0` — not exposed in `settings.json` (decision: hardcoded constants).
- Steady-state thresholds are **unchanged**: `heartbeat_seconds=5` (warn at 2×=10s), `stale_seconds=30`, `consecutive_failures=3`.
- The master heartbeat grace specifically requires a `SnapshotMsg` (not just a `StatusMsg`) — a snapshot proves the master is actually producing positions.
- Fatal-error handling is untouched: a worker that reports a FATAL `mt5.initialize` failure is never restarted.
- Exponential backoff (`BASE_BACKOFF`/`MAX_BACKOFF`) and slave readiness definition (SymbolInfo + first StatusMsg) are untouched.
- Tests use the existing injected-clock pattern (`time_fn=lambda: fake_now[0]`) and the existing `_StubProc` / direct-`WorkerHandle` construction patterns from `manager/tests/test_supervisor.py` and `test_supervisor_readiness.py`. No real terminal, no real subprocess in the new unit tests.

---

## File Structure

- **Modify:** `manager/supervisor.py` — add `STARTUP_GRACE_SECONDS` + `startup_grace_seconds` param; add `first_msg_seen` to `WorkerHandle`; set `first_msg_seen` on message receipt; grace-aware stale threshold in `_health_check`; add `_master_first_snapshot_seen` + grace-aware heartbeat threshold in `_read_master`; reset heartbeat state in `spawn_master` and `_restart`.
- **Modify:** `manager/app/controller.py:187` — `wait_for_slaves_ready(timeout=15.0)` → `90.0`.
- **Test (append):** `manager/tests/test_supervisor_readiness.py` — Tasks 1 & 2 tests (stale grace, heartbeat grace + reset). This file already owns the readiness/health unit tests, the `_StubProc`/`_FakeNow` helpers, and the `WorkerHandle` direct-construction pattern.
- **Test (append):** `manager/tests/test_controller.py` — Task 3 test (readiness timeout is 90s). This file already owns the controller `start` tests and the `_controller`/`_master`/`_slave`/`_slave_state` helpers.

---

## Task 1: Stale-restart grace window (`first_msg_seen`)

Give each worker 90s after (re)spawn to send its first message before `_health_check` counts it stale. Once any message arrives, revert to `stale_seconds` (30s).

**Files:**
- Modify: `manager/supervisor.py` — `WorkerHandle` dataclass (line ~18-33), `Supervisor.__init__` (line ~44-62), `Supervisor._spawn` (line ~96-105), `Supervisor._drain_slaves` (line ~114-137), `Supervisor._read_master` (line ~160-212), `Supervisor._health_check` (line ~249-267)
- Test: `manager/tests/test_supervisor_readiness.py` (append two tests)

**Interfaces:**
- Consumes: existing `WorkerHandle`, `_health_check`, `_drain_slaves`, `_read_master`, `_spawn`.
- Produces: `WorkerHandle.first_msg_seen: bool` (default `False`); `Supervisor.STARTUP_GRACE_SECONDS = 90.0`; `Supervisor.__init__(..., startup_grace_seconds: float = STARTUP_GRACE_SECONDS, ...)` storing `self._startup_grace_seconds`. Task 2 relies on `self._startup_grace_seconds` existing.

- [ ] **Step 1: Write the two failing tests**

Append to `manager/tests/test_supervisor_readiness.py`:

```python
def test_stale_grace_holds_restart_then_fires_after_grace_window():
    """A freshly-(re)spawned worker that has sent NO message must NOT be
    restarted when stale_seconds (30s) elapses, but only once
    startup_grace_seconds (90s here, set to 30 for a fast test) elapses.
    first_msg_seen=False widens the threshold from stale_seconds to the
    grace window."""
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=10.0, consecutive_failures=2,
                     startup_grace_seconds=30.0,
                     poll_timeout=0.0, time_fn=clock)
    spawned = []

    def fake_spawn(name, role, config, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                         config=config, adapter_kind=adapter_kind,
                         fake_state=fake_state, last_msg_ts=clock.t)
        spawned.append(h)
        return h
    sup._spawn = fake_spawn
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0)  # first_msg_seen=False

    # 15s: past stale_seconds (10) but inside the grace window (30) -> not stale
    clock.t = 15.0
    sup._health_check()
    assert sup._handles["s1"].fail_count == 0, \
        "within grace window, a silent worker must not be counted stale"
    assert spawned == [], "within grace window, worker must not be restarted"

    # 35s: past the grace window (30) -> stale; 2 consecutive -> restart
    clock.t = 35.0
    sup._health_check()
    assert sup._handles["s1"].fail_count == 1
    sup._health_check()  # 2nd consecutive -> restart
    assert spawned, "past grace window, stale worker must be restarted"
    assert sup._handles["s1"] is spawned[-1]
    assert sup._handles["s1"].fail_count == 0  # reset on restart
    assert sup._handles["s1"].first_msg_seen is False  # fresh window on respawn


def test_stale_grace_falls_back_to_stale_seconds_after_first_message():
    """Once a worker has sent a message (first_msg_seen=True), the stale
    threshold reverts to stale_seconds (10s), NOT the grace window (30s).
    A worker that talked then went silent is restarted fast, as before."""
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=10.0, consecutive_failures=2,
                     startup_grace_seconds=30.0,
                     poll_timeout=0.0, time_fn=clock)
    spawned = []

    def fake_spawn(name, role, config, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                         config=config, adapter_kind=adapter_kind,
                         fake_state=fake_state, last_msg_ts=clock.t)
        spawned.append(h)
        return h
    sup._spawn = fake_spawn
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0,
        first_msg_seen=True)  # already talked once

    # 15s: past stale_seconds (10) -> stale (grace no longer applies)
    clock.t = 15.0
    sup._health_check()
    assert sup._handles["s1"].fail_count == 1, \
        "after first message, stale_seconds applies, not the grace window"
    sup._health_check()  # 2nd consecutive -> restart
    assert spawned, "silent-while-talking worker must be restarted fast"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_supervisor_readiness.py::test_stale_grace_holds_restart_then_fires_after_grace_window manager/tests/test_supervisor_readiness.py::test_stale_grace_falls_back_to_stale_seconds_after_first_message -v`
Expected: FAIL — `Supervisor.__init__()` got an unexpected keyword argument `startup_grace_seconds` (TypeError).

- [ ] **Step 3: Write the minimal implementation**

In `manager/supervisor.py`:

1. Add the class constant and `first_msg_seen` field. In the `WorkerHandle` dataclass, add after `fatal: bool = False`:

```python
    first_msg_seen: bool = False  # False until first message -> grace window
```

2. Add the class constant to `Supervisor` (near `BASE_BACKOFF`/`MAX_BACKOFF`):

```python
    STARTUP_GRACE_SECONDS = 90.0  # first-message grace after (re)spawn
```

3. Extend `__init__` signature and store the value. Change:

```python
    def __init__(self, engine: CopyEngine, heartbeat_seconds: int = 5,
                 stale_seconds: float = 30.0, consecutive_failures: int = 3,
                 poll_timeout: float = 0.2, time_fn=time.time,
                 kill_terminal=None):
```
to:
```python
    def __init__(self, engine: CopyEngine, heartbeat_seconds: int = 5,
                 stale_seconds: float = 30.0, consecutive_failures: int = 3,
                 poll_timeout: float = 0.2, time_fn=time.time,
                 kill_terminal=None,
                 startup_grace_seconds: float = STARTUP_GRACE_SECONDS):
```
and add inside the body (after `self._kill_terminal = ...`):
```python
        self._startup_grace_seconds = startup_grace_seconds
```

4. Make `_health_check` grace-aware. Replace the stale branch:

```python
            if self._time_fn() - h.last_msg_ts > self._stale_seconds:
                h.fail_count += 1
                if h.fail_count >= self._consecutive_failures:
                    self._restart(name)
            else:
                h.fail_count = 0
```
with:
```python
            stale = (self._startup_grace_seconds if not h.first_msg_seen
                     else self._stale_seconds)
            if self._time_fn() - h.last_msg_ts > stale:
                h.fail_count += 1
                if h.fail_count >= self._consecutive_failures:
                    self._restart(name)
            else:
                h.fail_count = 0
```

5. Set `first_msg_seen=True` when a message arrives. In `_drain_slaves`, after:

```python
                self._dispatch_slave(name, msg)
                h.last_msg_ts = self._time_fn()
                h.fail_count = 0
```
add:
```python
                h.first_msg_seen = True
```

6. In `_read_master`, after the successful recv block:

```python
            h.last_msg_ts = self._time_fn()
            h.fail_count = 0
            h.restart_count = 0
            h.next_restart_at = 0.0
```
add:
```python
            h.first_msg_seen = True
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest manager/tests/test_supervisor_readiness.py::test_stale_grace_holds_restart_then_fires_after_grace_window manager/tests/test_supervisor_readiness.py::test_stale_grace_falls_back_to_stale_seconds_after_first_message -v`
Expected: PASS.

- [ ] **Step 5: Run the full supervisor + readiness suites to confirm no regressions**

Run: `python -m pytest manager/tests/test_supervisor.py manager/tests/test_supervisor_readiness.py -v`
Expected: PASS (all pre-existing tests still green — the grace window only widens the threshold; pre-existing tests use `stale_seconds` values and clocks where the outcome is unchanged, or exercise the process-death path which bypasses staleness).

- [ ] **Step 6: Commit**

```bash
git add manager/supervisor.py manager/tests/test_supervisor_readiness.py
git commit -m "feat(supervisor): 90s stale-restart grace window until first message"
```

---

## Task 2: Heartbeat grace window (`_master_first_snapshot_seen`)

Suppress "no heartbeat from master" for 90s after the master is (re)spawned until its first `SnapshotMsg`, then revert to the 10s steady-state threshold. Reset the grace window on master (re)spawn.

**Files:**
- Modify: `manager/supervisor.py` — `Supervisor.__init__` (line ~56-57), `Supervisor.spawn_master` (line ~64-66), `Supervisor._read_master` (line ~192-211), `Supervisor._restart` (line ~307-312)
- Test: `manager/tests/test_supervisor_readiness.py` (append two tests)

**Interfaces:**
- Consumes: `self._startup_grace_seconds` from Task 1; existing `spawn_master`, `_read_master`, `_restart`.
- Produces: `Supervisor._master_first_snapshot_seen` flag; heartbeat threshold switches `STARTUP_GRACE_SECONDS` → `heartbeat_seconds * 2` on first snapshot; `spawn_master` and `_restart` (master role) reset `_last_snapshot_ts` / `_master_first_snapshot_seen` / `heartbeat_warning`. The reset in `spawn_master` is also load-bearing for initial start: the controller waits up to 90s for slaves *before* spawning the master, so the heartbeat clock must start at spawn time, not at supervisor construction.

- [ ] **Step 1: Write the two failing tests**

Append to `manager/tests/test_supervisor_readiness.py`. Add the imports at the top of the file (alongside the existing `from manager.engine.models import ...` line):

```python
import multiprocessing

from manager.ipc.messages import SnapshotMsg
from manager.ipc.pipe_framing import send_msg
```

Then append the tests:

```python
def test_heartbeat_grace_suppresses_warning_then_fires_after_grace():
    """After spawn_master, no snapshot within heartbeat_seconds*2 (10s) must
    NOT fire 'no heartbeat' while inside the grace window (30s here); it
    MUST fire once the grace window elapses. Uses an open empty pipe so
    _read_master reaches the heartbeat check (poll returns False)."""
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=1000.0,
                     consecutive_failures=5, startup_grace_seconds=30.0,
                     poll_timeout=0.0, time_fn=clock)
    errors = []
    sup.on_error = lambda name, msg: errors.append((name, msg))
    parent, child = multiprocessing.Pipe(duplex=True)  # peer alive, no data

    def fake_spawn(name, role, config, adapter_kind, fake_state):
        return WorkerHandle(name=name, role=role, proc=_StubProc(),
                            pipe=parent, config=config,
                            adapter_kind=adapter_kind,
                            fake_state=fake_state, last_msg_ts=clock.t)
    sup._spawn = fake_spawn
    try:
        sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 20})
        assert sup._master_first_snapshot_seen is False
        # 12s: past steady-state heartbeat threshold (10s) but inside grace (30s)
        clock.t = 12.0
        sup._read_master(0.0)
        assert sup.heartbeat_warning is False
        assert not any("no heartbeat" in m for _, m in errors)
        # 31s: past the grace window -> warning fires
        clock.t = 31.0
        sup._read_master(0.0)
        assert sup.heartbeat_warning is True
        assert any("no heartbeat" in m for _, m in errors)
    finally:
        parent.close()
        child.close()


def test_heartbeat_grace_resets_on_master_restart():
    """After a master restart, the warning is suppressed again for the grace
    window even though a prior snapshot had been seen: _restart(master)
    resets _master_first_snapshot_seen / heartbeat_warning / _last_snapshot_ts."""
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=1000.0,
                     consecutive_failures=5, startup_grace_seconds=30.0,
                     poll_timeout=0.0, time_fn=clock)
    sup.on_error = lambda name, msg: None
    pipes = []

    def fake_spawn(name, role, config, adapter_kind, fake_state):
        p, c = multiprocessing.Pipe(duplex=True)  # fresh empty pipe per spawn
        pipes.append((p, c))
        return WorkerHandle(name=name, role=role, proc=_StubProc(),
                            pipe=p, config=config,
                            adapter_kind=adapter_kind,
                            fake_state=fake_state, last_msg_ts=clock.t)
    sup._spawn = fake_spawn
    try:
        sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 20})
        # first master produces a snapshot at t=0 -> first_snapshot_seen=True
        send_msg(pipes[0][1], SnapshotMsg(source_id="master", timestamp=0,
                                          heartbeat=1, positions=()))
        sup._read_master(0.0)
        assert sup._master_first_snapshot_seen is True
        # master goes silent past 10s -> warning fires (steady-state threshold)
        clock.t = 11.0
        sup._read_master(0.0)
        assert sup.heartbeat_warning is True
        # restart the master at t=11 -> grace window resets
        sup._restart("master")
        assert sup._master_first_snapshot_seen is False
        assert sup.heartbeat_warning is False
        # 20s: 9s after restart, inside the new grace window -> no warning
        clock.t = 20.0
        sup._read_master(0.0)
        assert sup.heartbeat_warning is False
    finally:
        for p, c in pipes:
            try:
                p.close()
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_supervisor_readiness.py::test_heartbeat_grace_suppresses_warning_then_fires_after_grace manager/tests/test_supervisor_readiness.py::test_heartbeat_grace_resets_on_master_restart -v`
Expected: FAIL — `Supervisor` has no attribute `_master_first_snapshot_seen` (AttributeError), and/or the warning fires at 12s instead of being suppressed.

- [ ] **Step 3: Write the minimal implementation**

In `manager/supervisor.py`:

1. In `__init__`, after `self.heartbeat_warning = False`, add:

```python
        self._master_first_snapshot_seen = False
```

2. Add a small reset helper near `spawn_master` (after the `spawn_slave` method, before `slave_ready`):

```python
    def _reset_master_heartbeat(self) -> None:
        """Start a fresh heartbeat grace window: the master gets up to
        STARTUP_GRACE_SECONDS to produce its first SnapshotMsg before 'no
        heartbeat' fires. Called on initial spawn and on every master restart."""
        self._last_snapshot_ts = self._time_fn()
        self._master_first_snapshot_seen = False
        self.heartbeat_warning = False
```

3. Call it from `spawn_master`. Change:

```python
    def spawn_master(self, config, adapter_kind="real", fake_state=None):
        self._handles["master"] = self._spawn("master", "master", config,
                                               adapter_kind, fake_state)
```
to:
```python
    def spawn_master(self, config, adapter_kind="real", fake_state=None):
        self._handles["master"] = self._spawn("master", "master", config,
                                               adapter_kind, fake_state)
        self._reset_master_heartbeat()
```

4. Make `_read_master` grace-aware and flip the flag on the first snapshot. In the `SnapshotMsg` branch, change:

```python
            if isinstance(msg, SnapshotMsg):
                self._last_snapshot_ts = self._time_fn()
                self.heartbeat_warning = False
```
to:
```python
            if isinstance(msg, SnapshotMsg):
                self._last_snapshot_ts = self._time_fn()
                self._master_first_snapshot_seen = True
                self.heartbeat_warning = False
```

5. Replace the heartbeat-warning threshold check. Change:

```python
        if self._time_fn() - self._last_snapshot_ts > self._heartbeat_seconds * 2:
            if not self.heartbeat_warning:
                self.heartbeat_warning = True
                self._surface_error("master", "no heartbeat from master")
```
to:
```python
        hb = (self._startup_grace_seconds if not self._master_first_snapshot_seen
              else self._heartbeat_seconds * 2)
        if self._time_fn() - self._last_snapshot_ts > hb:
            if not self.heartbeat_warning:
                self.heartbeat_warning = True
                self._surface_error("master", "no heartbeat from master")
```

6. Reset on master restart. In `_restart`, after:

```python
        self._handles[name] = new_h
        if self.on_restart:
            self.on_restart(name, h.role)
```
insert the reset for the master role (between those two lines):

```python
        self._handles[name] = new_h
        if h.role == "master":
            self._reset_master_heartbeat()
        if self.on_restart:
            self.on_restart(name, h.role)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest manager/tests/test_supervisor_readiness.py::test_heartbeat_grace_suppresses_warning_then_fires_after_grace manager/tests/test_supervisor_readiness.py::test_heartbeat_grace_resets_on_master_restart -v`
Expected: PASS.

- [ ] **Step 5: Run the full supervisor + readiness suites**

Run: `python -m pytest manager/tests/test_supervisor.py manager/tests/test_supervisor_readiness.py -v`
Expected: PASS. In particular `test_master_death_restarts_master` still passes — death-triggered restart bypasses staleness, and the new heartbeat reset does not affect process-death behavior.

- [ ] **Step 6: Commit**

```bash
git add manager/supervisor.py manager/tests/test_supervisor_readiness.py
git commit -m "feat(supervisor): 90s heartbeat grace window until first snapshot, reset on restart"
```

---

## Task 3: Widen the slave-readiness gate to 90s

`controller.start` waits only 15s for slaves to report SymbolInfo + first Status. On a slow first-launch terminal that is not enough. Widen to 90s. The gate still returns early the moment all slaves are ready, so fast starts are unaffected.

**Files:**
- Modify: `manager/app/controller.py:187` — `wait_for_slaves_ready(timeout=15.0)` → `wait_for_slaves_ready(timeout=90.0)`
- Test: `manager/tests/test_controller.py` (append one test)

**Interfaces:**
- Consumes: existing `Supervisor.wait_for_slaves_ready` (already early-returns on ready); Tasks 1 & 2 are not required for this change to compile, but the 90s value only helps in practice once workers actually get a 90s grace window.
- Produces: a 90s readiness timeout, matching the worker grace window so a slow slave is not declared "not ready" before its own grace window has elapsed.

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_controller.py`. The test monkeypatches `Supervisor.wait_for_slaves_ready` to capture the `timeout` argument and short-circuit to ready, so it does not actually wait 90s. Add the import at the top (alongside the existing `from manager.app.controller import ...`):

```python
from manager.supervisor import Supervisor
```

Then append:

```python
def test_start_readiness_gate_uses_90s_timeout(monkeypatch):
    """On first launch a slave terminal can take 30-90s+ to log in and report
    SymbolInfo + Status; the readiness gate must allow 90s, not 15s. The gate
    still returns early once ready, so fast starts are unaffected."""
    captured = {}

    def fake_wait(self, timeout=10.0, slave_ids=None):
        captured["timeout"] = timeout
        return True  # short-circuit to ready; we only assert the timeout arg
    monkeypatch.setattr(Supervisor, "wait_for_slaves_ready", fake_wait)

    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    c.start(_master(), [_slave()],
            master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                             "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    try:
        assert captured.get("timeout") == 90.0, \
            "readiness gate must wait 90s for slow first-launch slaves"
    finally:
        c.stop()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest manager/tests/test_controller.py::test_start_readiness_gate_uses_90s_timeout -v`
Expected: FAIL — `assert 15.0 == 90.0` (the controller currently passes `timeout=15.0`).

- [ ] **Step 3: Write the minimal implementation**

In `manager/app/controller.py`, change line 187:

```python
        ready = sup.wait_for_slaves_ready(timeout=15.0)
```
to:
```python
        ready = sup.wait_for_slaves_ready(timeout=90.0)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest manager/tests/test_controller.py::test_start_readiness_gate_uses_90s_timeout -v`
Expected: PASS.

- [ ] **Step 5: Run the full controller suite**

Run: `python -m pytest manager/tests/test_controller.py -v`
Expected: PASS (the existing `test_start_runs_readiness_gate_then_master_and_copies` uses fast fake slaves that become ready well under 90s; the gate returns early, so its runtime is unchanged).

- [ ] **Step 6: Commit**

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(controller): widen slave readiness gate to 90s for slow first launch"
```

---

## Final Verification

After all three tasks:

- [ ] Run the whole manager test suite: `python -m pytest manager/tests -v` — all green.
- [ ] Confirm no existing test grew materially slower (the readiness gate test in Task 3 short-circuits; the real `test_start_runs_readiness_gate_then_master_and_copies` still returns early on fast fake slaves).
- [ ] Grep sanity check: `STARTUP_GRACE_SECONDS`, `first_msg_seen`, `_master_first_snapshot_seen`, `_reset_master_heartbeat`, and `timeout=90.0` each appear exactly where intended in `manager/supervisor.py` and `manager/app/controller.py`.