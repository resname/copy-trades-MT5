import time

from manager.engine.models import Position, SymbolInfo, BUY
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


def test_end_to_end_open_through_subprocesses():
    eng = _engine()
    sup = Supervisor(eng, heartbeat_seconds=5, stale_seconds=30,
                     consecutive_failures=3, poll_timeout=0.02)
    master_state = {
        "positions": [Position(42, "EURUSD", BUY, 1.10000, 0.5, 1.095, 1.105,
                               NOW, 0.00001, "")],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    # Spawn the slave first and wait for its SymbolInfoMsg to be applied before
    # spawning the master. The master sends its first SnapshotMsg immediately on
    # startup (no initial delay); the slave does more init work (recovery +
    # symbol-info + status) and loses the race, so the master's first snapshot
    # would be ingested with an empty symbol-info table, the OPEN skipped, and
    # the engine's prev-snapshot set -- never re-NEW'ing the position. Ordering
    # the spawn guarantees the SI is present for the first ingest.
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake",
                    fake_state=_slave_state())
    _tick_until(sup, lambda: bool(eng._slaves["s1"].symbol_infos))
    sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 20},
                     adapter_kind="fake", fake_state=master_state)
    try:
        ok = _tick_until(
            sup,
            lambda: eng._slaves["s1"].table.get(42) is not None
            and eng._slaves["s1"].table.get(42).slave_ticket != 0)
        assert ok, "OPEN did not flow end-to-end"
        rec = eng._slaves["s1"].table.get(42)
        assert rec.slave_open_volume == 0.10
        assert rec.master_open_volume == 0.5
    finally:
        sup.shutdown()


def test_restart_on_process_death():
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=1000, consecutive_failures=5,
                     poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake",
                    fake_state=_slave_state())
    try:
        _tick_until(sup, lambda: sup._handles["s1"].proc.is_alive())
        old = sup._handles["s1"].proc
        old.terminate(); old.join(2.0)
        assert not old.is_alive()
        ok = _tick_until(
            sup,
            lambda: sup._handles["s1"].proc is not old
            and sup._handles["s1"].proc.is_alive())
        assert ok, "slave was not restarted after death"
    finally:
        sup.shutdown()


def test_master_death_restarts_master():
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=1000, consecutive_failures=5,
                     poll_timeout=0.02)
    master_state = {
        "positions": [],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 20},
                     adapter_kind="fake", fake_state=master_state)
    try:
        _tick_until(sup, lambda: sup._handles["master"].proc.is_alive())
        old = sup._handles["master"].proc
        old.terminate(); old.join(2.0)
        assert not old.is_alive()
        ok = _tick_until(
            sup,
            lambda: sup._handles["master"].proc is not old
            and sup._handles["master"].proc.is_alive())
        assert ok, "master was not restarted after death"
    finally:
        sup.shutdown()


class _StubProc:
    def __init__(self): self._alive = True
    def is_alive(self): return self._alive
    def terminate(self): self._alive = False
    def join(self, timeout=None): pass


def test_consecutive_stale_failures_restart():
    fake_now = [0.0]
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=10.0, consecutive_failures=3,
                    time_fn=lambda: fake_now[0], poll_timeout=0.0)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    # stub _spawn so restart doesn't create a real subprocess
    spawned = []
    def fake_spawn(name, role, config, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                        config=config, adapter_kind=adapter_kind,
                        fake_state=fake_state, last_msg_ts=fake_now[0])
        spawned.append(h)
        return h
    sup._spawn = fake_spawn

    fake_now[0] = 100.0  # 100 - 0 > 10 -> stale
    sup._health_check(); assert sup._handles["s1"].fail_count == 1
    sup._health_check(); assert sup._handles["s1"].fail_count == 2
    sup._health_check()  # 3rd consecutive -> restart
    assert spawned, "restart should have spawned a new worker"
    assert sup._handles["s1"] is spawned[-1]
    assert sup._handles["s1"].fail_count == 0  # reset on restart


def test_message_resets_fail_count():
    fake_now = [0.0]
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=10.0, consecutive_failures=5,
                    time_fn=lambda: fake_now[0], poll_timeout=0.0)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    fake_now[0] = 100.0
    sup._health_check(); assert sup._handles["s1"].fail_count == 1
    # a fresh message resets the staleness window
    sup._handles["s1"].last_msg_ts = 100.0
    fake_now[0] = 105.0  # 5 < 10 -> not stale
    sup._health_check(); assert sup._handles["s1"].fail_count == 0


def test_shutdown_terminates_workers():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake",
                    fake_state=_slave_state())
    _tick_until(sup, lambda: sup._handles["s1"].proc.is_alive())
    sup.shutdown()
    assert sup._handles == {}