# manager/tests/test_controller.py
import time
import pytest

from manager.engine.models import SymbolInfo, BUY, Position
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.app.controller import (
    CopyController, AccountSpec, StatusUpdate, ControllerError,
)
from manager.terminal.discovery import TerminalInstance
from manager.settings import credentials

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = int(time.time())


class FakeTerminalManager:
    """Stub TerminalManager: no real install/discover/kill. Returns a fixed
    pool of instances and records calls."""
    def __init__(self, instances):
        self._instances = instances
        self.provisioned = []
        self.killed = []
    def discover_all(self):
        return list(self._instances)
    def provision_shortfall(self, num_slaves, setup_path=None):
        # pretend to provision the gap; return synthetic install dirs
        required = 1 + num_slaves
        gap = max(0, required - len(self._instances))
        new = [f"C:/prov/instance_{i}" for i in range(len(self._instances),
                                                      len(self._instances) + gap)]
        self.provisioned.append((num_slaves, gap, new))
        for d in new:
            self._instances.append(TerminalInstance(d, f"{d}/terminal64.exe",
                                                     "provisioned"))
        return new
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


def _master(spec_id="master", terminal_path=None):
    return AccountSpec(id=spec_id, login=1, server="Demo", password="pw",
                       terminal_path=terminal_path)


def _slave(sid="s1", terminal_path=None):
    return AccountSpec(id=sid, login=2, server="Demo", password="pw",
                       terminal_path=terminal_path, symbol_map_csv="EURUSD=EURUSD")


def _slave_cfg():
    return {"slave_id": "s1", "terminal_path": "C:/t/s.exe", "login": 2,
            "server": "Demo", "symbol_map_csv": "EURUSD=EURUSD",
            "normalize_sltp": True, "retry_count": 1, "retry_delay_ms": 0,
            "slave_status_interval_ms": 60000}


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


def test_prepare_provisions_and_assigns_one_per_account():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    assert set(assigned) == {"master", "s1"}
    assert assigned["master"].exe_path == "C:/i0/terminal64.exe"
    # one slave short -> provision_shortfall installed one more instance
    assert len(c._terminal_manager._instances) >= 2
    assert c._terminal_manager.provisioned  # shortfall was requested


def test_prepare_rejects_duplicate_terminal_path_overrides():
    insts = []
    c, _, _ = _controller(insts)
    with pytest.raises(ControllerError):
        c.prepare(AccountSpec(id="m", login=1, server="D", password="p",
                               terminal_path="C:/same/terminal64.exe"),
                  [AccountSpec(id="s1", login=2, server="D", password="p",
                               terminal_path="C:/same/terminal64.exe")])


def test_prepare_normalizes_override_dir_to_exe_path():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(terminal_path="C:/override"), [_slave()])
    assert assigned["master"].exe_path == "C:/override/terminal64.exe"
    assert assigned["master"].source == "override"


def test_start_runs_readiness_gate_then_master_and_copies():
    """End-to-end: start() provisions, spawns slaves, waits for readiness,
    THEN spawns the master — and the first OPEN is not skipped (the race
    the gate prevents). Uses the real Supervisor + FakeMt5 workers. The
    controller routes to the fake adapter when *_fake_state is provided, so
    no monkeypatch is needed."""
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "provisioned"),
             TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "provisioned")]
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
        # the readiness gate ran: a 'ready' status was emitted before the master
        assert any(s.kind == "ready" for s in statuses)
    finally:
        c.stop()


def test_start_sets_portable_true_only_for_provisioned_instances():
    insts = [TerminalInstance("C:/disc", "C:/disc/terminal64.exe", "appdata"),
             TerminalInstance("C:/prov", "C:/prov/terminal64.exe", "provisioned")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    cfgs = c.build_worker_configs(_master(), [_slave()], assigned)
    # master got the discovered (appdata) instance -> portable False
    assert cfgs["master"]["portable"] is False
    # slave got the provisioned instance -> portable True
    assert cfgs["s1"]["portable"] is True


def test_start_passes_kill_terminal_to_supervisor():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata"),
             TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    # don't actually start workers; just check the supervisor is built with
    # the terminal manager's kill_terminal wired
    sup = c.build_supervisor(heartbeat_seconds=5)
    # bound methods compare by (func, instance); == is the identity test here
    assert sup._kill_terminal == c._terminal_manager.kill_terminal


def test_is_running_reflects_start_stop():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata"),
             TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assert not c.is_running()
    c.start(_master(), [_slave()], master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                            "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    assert c.is_running()
    c.stop()
    assert not c.is_running()


def test_load_password_decrypts_from_store(monkeypatch):
    # store with a base64 password blob; controller.decrypt_password recovers it
    from manager.settings import credentials
    fake_crypto_calls = []

    class FakeCrypto:
        def CryptProtectData(self, data, desc, *r):
            fake_crypto_calls.append(("p", data))
            return b"ENC:" + data
        def CryptUnprotectData(self, blob, *r):
            fake_crypto_calls.append(("u", blob))
            if not blob.startswith(b"ENC:"):
                raise ValueError("bad")
            return ("d", blob[len(b"ENC:"):])
    crypto = FakeCrypto()
    # The controller's load_password decrypts via credentials.decrypt_password
    # with no crypto arg, which lazy-imports win32crypt. Stub the loader so the
    # test runs without pywin32 and uses the same FakeCrypto for both halves.
    monkeypatch.setattr(credentials, "_load_crypto", lambda: crypto)
    blob = credentials.encrypt_password("s3cret", crypto=crypto)
    from manager.settings.store import SettingsStore
    import tempfile, os
    d = tempfile.mkdtemp()
    store = SettingsStore(path=os.path.join(d, "s.json"))
    store.save({"accounts": {"s1": {"login": 2, "password_blob": blob}},
                "provisioned_instances": [], "global": {}})
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store,
                       credentials=credentials)
    assert c.load_password("s1") == "s3cret"


def test_load_password_reraises_decrypt_error_to_prompt_reentry():
    # a corrupt blob -> CredentialDecryptError surfaces (GUI re-prompts)
    from manager.settings.store import SettingsStore
    import tempfile, os
    d = tempfile.mkdtemp()
    store = SettingsStore(path=os.path.join(d, "s.json"))
    store.save({"accounts": {"s1": {"login": 2, "password_blob": "!!!bad!!!"}},
                "provisioned_instances": [], "global": {}})
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    with pytest.raises(credentials.CredentialDecryptError):
        c.load_password("s1")


def test_worker_status_records_learned_server_once(tmp_path):
    from manager.settings.store import SettingsStore
    from manager.brokers import learned
    from manager.ipc.messages import StatusMsg
    store = SettingsStore(path=tmp_path / "settings.json")
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    msg = StatusMsg(source_id="master", role="master", connected=True,
                    login=1, balance=0.0, equity=0.0, currency="USD",
                    server="ICMarketsSC-Demo")
    c._on_worker_status("master", "master", msg)
    assert learned.load(store) == ["ICMarketsSC-Demo"]
    # a second status for the same server does not duplicate / re-record
    c._on_worker_status("master", "master", msg)
    assert learned.load(store) == ["ICMarketsSC-Demo"]


def test_worker_status_records_distinct_servers(tmp_path):
    from manager.settings.store import SettingsStore
    from manager.brokers import learned
    from manager.ipc.messages import StatusMsg
    store = SettingsStore(path=tmp_path / "settings.json")
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    c._on_worker_status("s1", "slave", StatusMsg(
        source_id="s1", role="slave", connected=True, login=2, balance=0.0,
        equity=0.0, currency="USD", server="A-Demo"))
    c._on_worker_status("master", "master", StatusMsg(
        source_id="master", role="master", connected=True, login=1, balance=0.0,
        equity=0.0, currency="USD", server="B-Demo"))
    assert learned.load(store) == ["A-Demo", "B-Demo"]


def test_worker_status_ignores_empty_server(tmp_path):
    from manager.settings.store import SettingsStore
    from manager.brokers import learned
    from manager.ipc.messages import StatusMsg
    store = SettingsStore(path=tmp_path / "settings.json")
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    c._on_worker_status("master", "master", StatusMsg(
        source_id="master", role="master", connected=True, login=1, balance=0.0,
        equity=0.0, currency="USD", server=""))
    assert learned.load(store) == []


def test_build_supervisor_wires_status_hook():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    sup = c.build_supervisor(heartbeat_seconds=5)
    # bound-method equality is identity, like the existing kill_terminal test
    assert sup.on_status_msg == c._on_worker_status