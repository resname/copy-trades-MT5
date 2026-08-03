import pytest

pytest.importorskip("PySide6")


class FakeTerminalManager:
    def __init__(self): self._instances = []
    def discover_all(self): return []
    def provision_shortfall(self, n, setup_path=None): return []
    def assign(self, accounts): return {}
    def kill_terminal(self, exe): return 0


class _FakeStore:
    # The real SettingsStore exposes a ``path`` the broker cache lives next to;
    # ``CopyController.get_catalog`` reads ``self._store.path.parent``. Point at a
    # temp file so the read-only cache lookup finds nothing (load_cache never
    # raises on a missing file) and nothing is written in the worktree.
    def __init__(self):
        import pathlib
        import tempfile
        self.path = pathlib.Path(tempfile.gettempdir()) / "copytrades_test_settings.json"
    def load(self): return {}
    def save(self, d): pass


def test_main_assembles_window_tray_controller(qapp, monkeypatch):
    # patch the real TerminalManager + SettingsStore so assembly needs no disk/MT5
    import manager.__main__ as entry
    monkeypatch.setattr(entry, "TerminalManager", lambda *a, **k: FakeTerminalManager())
    monkeypatch.setattr(entry, "SettingsStore", lambda *a, **k: _FakeStore())
    # don't run the event loop; just build the graph (returns 4: window, tray, ctrl, bridge)
    w, tray, controller, bridge = entry.build_app_graph(qapp)
    assert w is not None
    assert tray is not None
    assert controller is not None
    assert bridge is not None
    # the controller's status callback is wired to the window via the bridge
    assert hasattr(w, "append_status")


def test_main_returns_zero_before_event_loop(qapp, monkeypatch):
    import manager.__main__ as entry
    monkeypatch.setattr(entry, "TerminalManager", lambda *a, **k: FakeTerminalManager())
    monkeypatch.setattr(entry, "SettingsStore", lambda *a, **k: _FakeStore())
    # short-circuit the event loop
    monkeypatch.setattr(entry.QApplication, "exec", lambda self: 0)
    rc = entry.main([])
    assert rc == 0


def test_main_version_flag(capsys):
    import manager.__main__ as entry
    from manager._version import __version__
    rc = entry.main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert __version__ in out


def test_main_update_subcommand(monkeypatch):
    import manager.__main__ as entry
    import manager.updater as updater
    called = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit: called.append(on_quit))
    rc = entry.main(["update"])
    assert rc == 0
    assert len(called) == 1  # on_quit passed through, not invoked here