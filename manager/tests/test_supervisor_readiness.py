# manager/tests/test_supervisor_readiness.py
import time

from manager.engine.models import SymbolInfo, BUY, Position
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.supervisor import Supervisor, WorkerHandle

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = int(time.time())


def _engine():
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                              step_amount=100.0, step_size=0.01, max_lot=10.0,
                              max_trade_age_minutes=999999, normalize_sltp=True))
    return eng


def _slave_cfg():
    return {"slave_id": "s1", "terminal_path": "C:/t/s.exe",
            "symbol_map_csv": "EURUSD=EURUSD",
            "normalize_sltp": True, "retry_count": 1, "retry_delay_ms": 0,
            "slave_status_interval_ms": 60000}


def _slave_state():
    return {"symbol_infos": {"EURUSD": SI},
            "account": {"login": 2, "balance": 1000.0, "equity": 1000.0,
                        "currency": "USD", "server": "Demo"},
            "ticks": {"EURUSD": (1.10000, 1.10010)}}


def _tick_until(sup, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.tick(timeout=0.02)
        if predicate():
            return True
    return predicate()


def test_slave_ready_requires_symbol_info_and_status():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake",
                    fake_state=_slave_state())
    try:
        # not ready immediately after spawn
        assert not sup.slave_ready("s1")
        ok = _tick_until(sup, lambda: sup.slave_ready("s1"))
        assert ok, "slave never became ready (SI + Status)"
        assert sup.slave_ready("s1")
    finally:
        sup.shutdown()


def test_wait_for_slaves_ready_returns_true_when_ready():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake",
                    fake_state=_slave_state())
    try:
        assert sup.wait_for_slaves_ready(timeout=5.0)
        assert sup.slave_ready("s1")
    finally:
        sup.shutdown()


def test_wait_for_slaves_ready_times_out_when_status_missing():
    """A slave that reported SymbolInfo but never Status (e.g. crashed
    mid-init) must cause wait_for_slaves_ready to return False, not hang.
    Pure unit test: construct the handle directly so the outcome is
    deterministic rather than racing a real fake worker's init."""
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.0)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        adapter_kind="fake", fake_state=None,
        got_symbol_info=True, got_status=False, last_msg_ts=time.time())
    # _StubProc is alive and last_msg_ts is now, so _health_check never
    # restarts; tick is a fast no-op. The gate polls until the wall-clock
    # timeout elapses and returns False because got_status stays False.
    ok = sup.wait_for_slaves_ready(timeout=0.1)
    assert ok is False


def test_readiness_gate_prevents_permanent_skip_of_first_snapshot():
    """Plan 2 deferred MUST #1: with the readiness gate, the master is spawned
    AFTER slaves are ready, so the first NEW is not skipped for no-info."""
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    master_state = {
        "positions": [Position(42, "EURUSD", BUY, 1.10000, 0.5, 1.095, 1.105,
                               NOW, 0.00001, "")],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake",
                    fake_state=_slave_state())
    assert sup.wait_for_slaves_ready(timeout=5.0)
    sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 20},
                     adapter_kind="fake", fake_state=master_state)
    try:
        ok = _tick_until(
            sup,
            lambda: eng._slaves["s1"].table.get(42) is not None
            and eng._slaves["s1"].table.get(42).slave_ticket != 0)
        assert ok, "first OPEN was skipped (readiness gate did not prevent the race)"
    finally:
        sup.shutdown()


class _FakeNow:
    """Mutable clock for backoff tests."""
    def __init__(self, t=0.0):
        self.t = t
    def __call__(self):
        return self.t


class _StubProc:
    def __init__(self): self._alive = True
    def is_alive(self): return self._alive
    def terminate(self): self._alive = False
    def join(self, timeout=None): pass


def test_restart_backoff_delays_respawn_of_dead_worker():
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=1000.0, consecutive_failures=5,
                     poll_timeout=0.0, time_fn=clock)
    sup.MAX_BACKOFF = 30.0
    sup.BASE_BACKOFF = 1.0
    spawned = []

    def fake_spawn(name, role, config, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                         config=config,
                         adapter_kind=adapter_kind, fake_state=fake_state,
                         last_msg_ts=clock.t)
        spawned.append(h)
        return h
    sup._spawn = fake_spawn
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    # kill it
    sup._handles["s1"].proc.terminate()
    assert not sup._handles["s1"].proc.is_alive()
    sup._restart("s1")
    assert spawned, "first restart should spawn immediately"
    first = spawned[-1]
    # kill again -> should schedule backoff, NOT spawn this time
    first.proc.terminate()
    clock.t = 0.5  # < next_restart_at (1.0) -> skip
    count_before = len(spawned)
    sup._restart("s1")
    assert len(spawned) == count_before, "restart should be skipped within backoff window"
    # advance past backoff -> spawns
    clock.t = 1.5
    sup._restart("s1")
    assert len(spawned) == count_before + 1


def test_restart_backoff_resets_on_message():
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=1000.0, consecutive_failures=5,
                     poll_timeout=0.0, time_fn=clock)
    sup.MAX_BACKOFF = 30.0
    sup.BASE_BACKOFF = 1.0
    spawned = []

    def fake_spawn(name, role, config, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                         config=config,
                         adapter_kind=adapter_kind, fake_state=fake_state,
                         last_msg_ts=clock.t)
        spawned.append(h)
        return h
    sup._spawn = fake_spawn
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    # First death: immediate respawn; backoff (1.0s) scheduled for the NEXT death.
    sup._handles["s1"].proc.terminate()
    sup._restart("s1")
    assert len(spawned) == 1
    first = spawned[-1]
    assert first.restart_count == 1 and first.next_restart_at == 1.0
    # A message arrives on the new worker -> backoff resets to zero.
    first.restart_count = 0
    first.next_restart_at = 0.0
    # Second death: only 0.5s later, but backoff was reset so respawn is
    # immediate (no skip). Without the reset, 0.5 < 1.0 would have skipped.
    first.proc.terminate()
    clock.t = 0.5
    sup._restart("s1")
    assert len(spawned) == 2
    # And the next backoff window is the BASE (1.0s), not doubled (2.0s),
    # because restart_count was 0 going in.
    second = spawned[-1]
    assert second.restart_count == 1
    assert second.next_restart_at == 1.5  # 0.5 + base 1.0, not 0.5 + 2.0


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