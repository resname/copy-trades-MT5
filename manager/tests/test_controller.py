# manager/tests/test_controller.py
import time
import pytest

from manager.engine.models import SymbolInfo, BUY, Position
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.app.controller import (
    CopyController, AccountSpec, StatusUpdate, ControllerError,
)
from manager.terminal.discovery import TerminalInstance

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = int(time.time())


class FakeTerminalManager:
    """Stub TerminalManager: no real install/discover/kill. Returns a fixed
    pool of instances and records calls. No provision_shortfall (removed)."""
    def __init__(self, instances):
        self._instances = instances
        self.killed = []
    def discover_all(self):
        return list(self._instances)
    def assign(self, accounts):
        pool = list(self._instances)
        out = {}
        for a in accounts:
            ov = a.get("terminal_path")
            if ov:
                exe = ov if ov.endswith("terminal64.exe") else f"{ov}/terminal64.exe"
                out[a["id"]] = TerminalInstance(exe.rsplit("/", 1)[0], exe, "override")
                pool = [p for p in pool if p.exe_path != exe]
                continue
            if not pool:
                raise ControllerError("not enough")
            out[a["id"]] = pool.pop(0)
        return out
    def kill_terminal(self, exe_path):
        self.killed.append(exe_path)
        return 0


def _master(spec_id="master", terminal_path="C:/m/terminal64.exe"):
    return AccountSpec(id=spec_id, terminal_path=terminal_path)


def _slave(sid="s1", terminal_path="C:/s/terminal64.exe"):
    return AccountSpec(id=sid, terminal_path=terminal_path,
                       symbol_map_csv="EURUSD=EURUSD")


def _slave_state():
    return {"symbol_infos": {"EURUSD": SI},
            "account": {"login": 2, "balance": 1000.0, "equity": 1000.0,
                        "currency": "USD", "server": "Demo"},
            "ticks": {"EURUSD": (1.10000, 1.10010)}}


def _controller(instances):
    statuses, logs = [], []
    c = CopyController(
        terminal_manager=FakeTerminalManager(instances),
        on_status=lambda s: statuses.append(s),
        on_log=lambda m: logs.append(m),
    )
    return c, statuses, logs


def _tick_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_prepare_assigns_one_terminal_per_account():
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    assert set(assigned) == {"master", "s1"}
    assert assigned["master"].exe_path == "C:/m/terminal64.exe"
    assert assigned["s1"].exe_path == "C:/s/terminal64.exe"


def test_prepare_rejects_duplicate_terminal_path_overrides():
    c, _, _ = _controller([])
    with pytest.raises(ControllerError):
        c.prepare(AccountSpec(id="m", terminal_path="C:/same/terminal64.exe"),
                  [AccountSpec(id="s1", terminal_path="C:/same/terminal64.exe")])


def test_prepare_normalizes_override_dir_to_exe_path():
    c, _, _ = _controller([])
    assigned = c.prepare(_master(terminal_path="C:/override"),
                         [_slave(terminal_path="C:/s/terminal64.exe")])
    assert assigned["master"].exe_path == "C:/override/terminal64.exe"
    assert assigned["master"].source == "override"


def test_build_worker_configs_omits_credentials_and_portable():
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    cfgs = c.build_worker_configs(_master(), [_slave()], assigned)
    assert cfgs["master"]["terminal_path"] == "C:/m/terminal64.exe"
    assert cfgs["s1"]["terminal_path"] == "C:/s/terminal64.exe"
    assert cfgs["s1"]["symbol_map_csv"] == "EURUSD=EURUSD"
    # no credential/portable keys in any config
    for cfg in cfgs.values():
        assert "login" not in cfg
        assert "server" not in cfg
        assert "portable" not in cfg


def test_start_runs_readiness_gate_then_master_and_copies():
    """End-to-end: start() spawns slaves, waits for readiness, THEN spawns the
    master — and the first OPEN is not skipped. Real Supervisor + FakeMt5."""
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
            master_fake_state=master_state,
            slave_fake_state=_slave_state())
    try:
        ok = _tick_until(
            lambda: c._engine._slaves["s1"].table.get(42) is not None
            and c._engine._slaves["s1"].table.get(42).slave_ticket != 0)
        assert ok, "first OPEN did not flow (readiness gate failed)"
        assert any(s.kind == "ready" for s in statuses)
    finally:
        c.stop()


def test_start_passes_kill_terminal_to_supervisor():
    c, _, _ = _controller([TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
                           TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")])
    sup = c.build_supervisor(heartbeat_seconds=5)
    assert sup._kill_terminal == c._terminal_manager.kill_terminal


def test_is_running_reflects_start_stop():
    c, _, _ = _controller([TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
                           TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")])
    assert not c.is_running()
    c.start(_master(), [_slave()],
            master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                            "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    assert c.is_running()
    c.stop()
    assert not c.is_running()