import pytest

pytest.importorskip("PySide6")


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


def test_main_window_constructs(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.windowTitle()  # has a title
    # terminal-only master form: no login/picker/password, terminal + buttons
    assert w.master_terminal is not None
    assert w.launch_terminal_button is not None
    assert w.install_metatrader_button is not None
    assert w.install_disclaimer_label is not None
    assert w.start_button is not None
    assert w.stop_button is not None
    assert w.status_view is not None
    assert w.log_view is not None
    assert w.slave_list is not None


def test_terminal_dropdown_populated_from_controller(qapp):
    from manager.gui.main_window import MainWindow
    from manager.terminal.discovery import TerminalInstance
    c = FakeController()
    c._instances = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata"),
                    TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "default")]
    w = MainWindow(c)
    items = [w.master_terminal.itemText(i) for i in range(w.master_terminal.count())]
    assert "C:/i0/terminal64.exe" in items
    assert "C:/i1/terminal64.exe" in items


def test_start_button_calls_controller_start_with_terminal_path(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w.start_button.click()
    assert c.started
    assert c.last_master.terminal_path == "C:/i0/terminal64.exe"


def test_start_button_refuses_blank_terminal(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    # no terminal selected -> start() not called, a log line is appended
    w.start_button.click()
    assert not c.started
    assert "terminal" in w.log_view.toPlainText().lower()


def test_launch_terminal_button_runs_terminal64_exe(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    popped = []
    monkeypatch.setattr("manager.gui.main_window.subprocess.Popen",
                        lambda cmd, **k: popped.append(cmd) or object())
    w.launch_terminal_button.click()
    assert popped == [["C:/i0/terminal64.exe"]]


def test_install_metatrader_button_opens_download_page(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    opened = []
    monkeypatch.setattr("manager.gui.main_window.webbrowser.open",
                        lambda url: opened.append(url) or True)
    w.install_metatrader_button.click()
    assert len(opened) == 1 and "metatrader5.com" in opened[0]
    assert "custom" in w.install_disclaimer_label.text().lower()


def test_launch_button_labeled_for_login(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.launch_terminal_button.text() == "Open terminal for login"
    assert w.install_metatrader_button.text() == "Install MetaTrader"


def test_install_button_is_left_of_launch_button(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    row = w.term_row
    assert row.indexOf(w.install_metatrader_button) < row.indexOf(w.launch_terminal_button)


def test_stop_button_calls_controller_stop(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    w.stop_button.setEnabled(True)
    w.stop_button.click()
    assert c.stopped


def test_status_update_appends_to_status_view(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import StatusUpdate
    c = FakeController()
    w = MainWindow(c)
    w.append_status(StatusUpdate(kind="info", message="hello"))
    assert "hello" in w.status_view.toPlainText()


def test_config_round_trip_restores_master_and_slaves(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    store = SettingsStore(path=tmp_path / "settings.json")
    c = FakeController()
    w = MainWindow(c, store=store)
    w.master_terminal.setEditText("C:/m/terminal64.exe")
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe")]
    w._save_config()

    c2 = FakeController()
    w2 = MainWindow(c2, store=store)
    assert w2.master_terminal.currentText() == "C:/m/terminal64.exe"
    assert len(w2._slaves) == 1
    assert w2._slaves[0].id == "s1"
    assert w2._slaves[0].terminal_path == "C:/s1/terminal64.exe"
    assert w2.slave_list.count() == 1


def test_load_config_skips_when_store_none(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())  # store=None
    assert w._slaves == []
    # construction did not raise
    assert w.windowTitle()


def test_save_config_noop_when_store_none(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._save_config()  # must not raise


def test_predownload_done_sets_cached_wheel_and_ready_label(qapp):
    from manager.gui.main_window import MainWindow
    from pathlib import Path
    w = MainWindow(FakeController())
    w._latest_version = "0.2.0"
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert w._cached_wheel == Path("C:/cached/manager-latest.whl")
    assert "ready" in w.update_label.text().lower()


def test_update_restart_passes_cached_wheel(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from pathlib import Path
    captured = {}
    from manager import updater
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None: captured.update(
                            {"on_quit": on_quit, "cached_wheel": cached_wheel}))
    w = MainWindow(FakeController())
    w._cached_wheel = Path("C:/cached/manager-latest.whl")
    w._on_update_restart()
    assert captured["cached_wheel"] == Path("C:/cached/manager-latest.whl")


def test_update_restart_refuses_while_running(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    c.started = True
    w = MainWindow(c)
    w._cached_wheel = None
    w._on_update_restart()  # is_running() True -> logs, does not call apply
    assert "stop" in w.log_view.toPlainText().lower()


def test_about_to_quit_persists_config(qapp, tmp_path):
    """Guards the aboutToQuit→_save_config hook: both window-close and
    update-quit reduce to QApplication.quit() → aboutToQuit, so this one test
    covers both quit paths. If the connect line is removed, this fails."""
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    from PySide6.QtWidgets import QApplication
    store = SettingsStore(path=tmp_path / "settings.json")
    w = MainWindow(FakeController(), store=store)
    w.master_terminal.setEditText("C:/m/terminal64.exe")
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe")]
    # emit the signal the way QApplication.quit() would
    QApplication.instance().aboutToQuit.emit()
    cfg = store.load_config()
    assert cfg["master"]["terminal_path"] == "C:/m/terminal64.exe"
    assert len(cfg["slaves"]) == 1
    assert cfg["slaves"][0]["id"] == "s1"


def test_about_to_quit_saves_once_not_twice(qapp, tmp_path):
    """A single aboutToQuit must produce a single save (no double-connect)."""
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from PySide6.QtWidgets import QApplication
    store = SettingsStore(path=tmp_path / "settings.json")
    w = MainWindow(FakeController(), store=store)
    calls = []
    real_save = store.save_config
    def counting_save(cfg):
        calls.append(cfg)
        real_save(cfg)
    store.save_config = counting_save
    QApplication.instance().aboutToQuit.emit()
    assert len(calls) == 1


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


def test_close_event_stops_controller_and_quits(qapp, monkeypatch):
    """closeEvent is the quit path: controller.stop() then event.accept() +
    QApplication.quit(). Replaces the old hide-to-tray behavior."""
    from manager.gui.main_window import MainWindow
    from PySide6.QtWidgets import QApplication
    quit_called = []
    monkeypatch.setattr(QApplication, "quit",
                        lambda *a, **k: quit_called.append(True))
    c = FakeController()
    w = MainWindow(c)

    class _Evt:
        def __init__(self):
            self.accepted = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    evt = _Evt()
    w.closeEvent(evt)
    assert c.stopped is True      # controller.stop() ran
    assert evt.accepted is True   # event accepted (window closes)
    assert quit_called            # QApplication.quit() invoked
