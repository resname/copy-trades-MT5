# manager/tests/test_terminal_manager.py
from pathlib import Path

import pytest

from manager.terminal.discovery import TerminalInstance
from manager.terminal.manager import TerminalManager, TerminalManagerError
from manager.settings.store import SettingsStore


class FakeStore:
    """Minimal store double: tracks provisioned-instance registry."""
    def __init__(self):
        self._insts = []
    def list_provisioned_instances(self):
        return list(self._insts)
    def add_provisioned_instance(self, d):
        if d not in self._insts:
            self._insts.append(d)
    def remove_provisioned_instance(self, d):
        self._insts = [x for x in self._insts if x != d]


class FakeProc:
    def __init__(self, exe, alive=True):
        self._exe = exe
        self._alive = alive
        self.terminated = False
        self.pid = hash(exe) & 0xFFFF
    def exe(self):
        return self._exe if self._alive else None
    def terminate(self):
        self._alive = False
        self.terminated = True
    def wait(self, timeout=None):
        return 0
    def kill(self):
        self._alive = False
        self.terminated = True


def _mgr(**kw):
    store = kw.pop("store", FakeStore())
    kw.setdefault("discover_fn", lambda **k: [])
    kw.setdefault("process_iter_fn", lambda attrs=None: [])
    kw.setdefault("sleep_fn", lambda s: None)
    kw.setdefault("time_fn", lambda: 0.0)
    return TerminalManager(store=store, **kw)


def test_required_count_is_one_plus_slaves():
    m = _mgr()
    assert m.required_count(0) == 1
    assert m.required_count(3) == 4


def test_discover_all_merges_appdata_default_and_provisioned():
    store = FakeStore()
    store.add_provisioned_instance(r"C:\prov\instance_0")
    discovered = [
        TerminalInstance(r"C:\Appdata\MT5A", r"C:\Appdata\MT5A\terminal64.exe", "appdata"),
        TerminalInstance(r"C:\Program Files\MetaTrader 5", r"C:\Program Files\MetaTrader 5\terminal64.exe", "default"),
    ]
    m = TerminalManager(store=store, discover_fn=lambda **k: discovered,
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    # stub exe existence: provisioning of provisioned exe_path
    all_insts = m.discover_all()
    by_exe = {i.exe_path: i for i in all_insts}
    assert by_exe[r"C:\Appdata\MT5A\terminal64.exe"].source == "appdata"
    assert by_exe[r"C:\Program Files\MetaTrader 5\terminal64.exe"].source == "default"
    # provisioned instance is included with source="provisioned"
    assert r"C:\prov\instance_0\terminal64.exe" in by_exe
    assert by_exe[r"C:\prov\instance_0\terminal64.exe"].source == "provisioned"


def test_discover_all_dedups_by_exe_path():
    """If the store's provisioned registry overlaps an appdata-discovered
    install, it appears once."""
    store = FakeStore()
    store.add_provisioned_instance(r"C:\overlap")
    discovered = [TerminalInstance(r"C:\overlap", r"C:\overlap\terminal64.exe", "appdata")]
    m = TerminalManager(store=store, discover_fn=lambda **k: discovered,
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    insts = m.discover_all()
    assert len(insts) == 1


def test_provision_shortfall_installs_and_registers():
    store = FakeStore()
    m = TerminalManager(store=store,
                        discover_fn=lambda **k: [],  # nothing installed yet
                        provision_fn=lambda index, setup_path, install_root=None,
                                       **k: fr"C:\prov\instance_{index}",
                        download_fn=lambda cache_path=None: r"C:\cache\mt5setup.exe",
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    new_dirs = m.provision_shortfall(num_slaves=2,
                                     setup_path=r"C:\cache\mt5setup.exe")
    # required = 3, available = 0 -> 3 new
    assert new_dirs == [r"C:\prov\instance_0", r"C:\prov\instance_1",
                       r"C:\prov\instance_2"]
    assert store.list_provisioned_instances() == new_dirs


def test_provision_shortfall_only_installs_the_gap():
    store = FakeStore()
    store.add_provisioned_instance(r"C:\existing\instance_0")
    m = TerminalManager(store=store,
                        discover_fn=lambda **k: [
                            TerminalInstance(r"C:\existing\instance_0",
                                             r"C:\existing\instance_0\terminal64.exe",
                                             "provisioned")],
                        provision_fn=lambda index, setup_path, install_root=None, **k:
                            fr"C:\prov\instance_{index}",
                        download_fn=lambda cache_path=None: r"C:\cache\mt5setup.exe",
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    new_dirs = m.provision_shortfall(num_slaves=1)  # required 2, available 1
    assert len(new_dirs) == 1


def test_assign_one_instance_per_account_auto():
    discovered = [
        TerminalInstance(r"C:\i0", r"C:\i0\terminal64.exe", "appdata"),
        TerminalInstance(r"C:\i1", r"C:\i1\terminal64.exe", "appdata"),
    ]
    m = _mgr(discover_fn=lambda **k: discovered)
    accounts = [{"id": "master"}, {"id": "s1"}]
    assigned = m.assign(accounts)
    assert set(assigned.keys()) == {"master", "s1"}
    assert assigned["master"].exe_path == r"C:\i0\terminal64.exe"
    assert assigned["s1"].exe_path == r"C:\i1\terminal64.exe"


def test_assign_respects_user_terminal_path_override():
    discovered = [TerminalInstance(r"C:\i0", r"C:\i0\terminal64.exe", "appdata")]
    m = _mgr(discover_fn=lambda **k: discovered)
    accounts = [{"id": "master", "terminal_path": r"C:\override\terminal64.exe"}]
    assigned = m.assign(accounts)
    assert assigned["master"].exe_path == r"C:\override\terminal64.exe"
    assert assigned["master"].source == "override"


def test_assign_raises_when_not_enough_instances():
    discovered = [TerminalInstance(r"C:\i0", r"C:\i0\terminal64.exe", "appdata")]
    m = _mgr(discover_fn=lambda **k: discovered)
    with pytest.raises(TerminalManagerError):
        m.assign([{"id": "master"}, {"id": "s1"}])  # need 2, have 1


def test_kill_terminal_matches_by_exe_path_case_insensitive():
    procs = [
        FakeProc(r"C:\Inst\terminal64.exe"),
        FakeProc(r"c:\inst\terminal64.exe"),  # same, case differs -> same instance
        FakeProc(r"C:\Other\terminal64.exe"),  # different instance
    ]
    m = TerminalManager(store=FakeStore(), discover_fn=lambda **k: [],
                        process_iter_fn=lambda attrs=None: procs,
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    n = m.kill_terminal(r"c:\INST\terminal64.exe")
    assert n == 2  # the two matching, case-insensitively
    assert procs[0].terminated and procs[1].terminated
    assert not procs[2].terminated


def test_kill_terminal_terminates_then_kills_on_timeout():
    class SlowProc:
        def __init__(self, exe):
            self._exe = exe
            self.killed = False
            self.pid = 1
        def exe(self): return self._exe
        def terminate(self): pass
        def wait(self, timeout=None):
            raise TimeoutError  # psutil raises psutil.TimeoutExpired; tests use this
        def kill(self):
            self.killed = True
    procs = [SlowProc(r"C:\Inst\terminal64.exe")]
    m = TerminalManager(store=FakeStore(), discover_fn=lambda **k: [],
                        process_iter_fn=lambda attrs=None: procs,
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    m.kill_terminal(r"C:\Inst\terminal64.exe")
    assert procs[0].killed


def test_kill_terminal_handles_missing_and_access_denied():
    class GoneProc:
        def __init__(self, exe): self._exe = exe; self.pid = 2
        def exe(self): return self._exe
        def terminate(self): raise FileNotFoundError  # NoSuchProcess analogue
        def wait(self, timeout=None): return 0
        def kill(self): pass
    class DeniedProc:
        def __init__(self, exe): self._exe = exe; self.pid = 3
        def exe(self): return self._exe
        def terminate(self): raise PermissionError  # AccessDenied analogue
        def wait(self, timeout=None): return 0
        def kill(self): raise PermissionError
    procs = [GoneProc(r"C:\Inst\terminal64.exe"),
             DeniedProc(r"C:\Inst\terminal64.exe")]
    m = TerminalManager(store=FakeStore(), discover_fn=lambda **k: [],
                        process_iter_fn=lambda attrs=None: procs,
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    # best-effort: counts attempts, does not raise
    n = m.kill_terminal(r"C:\Inst\terminal64.exe")
    assert n == 2