import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(autouse=True)
def _no_real_scheduled_task(monkeypatch):
    """GUI tests must not depend on a CopyTradesMT5 task registered on the
    machine running the suite (we register one for real on the user's PC)."""
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "task_exists", lambda: False)


@pytest.fixture(autouse=True)
def _no_real_autologon_repair(monkeypatch):
    """Keep GUI tests away from the host's real Winlogon/task state."""
    from manager.platform import autologon_repair
    monkeypatch.setattr(autologon_repair, "repair_on_startup",
                        lambda: autologon_repair.RepairResult())
    monkeypatch.setattr(autologon_repair, "setup_configured", lambda: True)


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


def test_load_config_drops_saved_ib_slave_and_keeps_mt5(qapp, tmp_path):
    """A v0.1.26 settings.json with an IB slave loads on the MT5-only build:
    the IB slave is dropped in memory (one log line, no crash); mt5 slaves
    survive. The file predates the one-time profiles migration, so the load
    rewrites it: the dead IB slave is gone and profiles: [] marks it done."""
    import json
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    path = tmp_path / "settings.json"
    slaves = [
        {"id": "ib1", "terminal_path": None,
         "symbol_map_csv": "US30=YM", "platform": "ib",
         "ib_host": "127.0.0.1", "ib_port": 4002, "ib_client_id": 7,
         "contract_map": {}},
        {"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
         "symbol_map_csv": "", "step_amount": 100.0,
         "step_size": 0.01, "max_lot": 10.0,
         "max_trade_age_minutes": 10.0, "normalize_sltp": True}]
    # SettingsStore nests the GUI config under a "config" key
    # (save_config/load_config), so write it pre-nested exactly as saved.
    path.write_text(
        json.dumps({"config": {"master": {"terminal_path": "C:/m/terminal64.exe"},
                               "slaves": slaves}}),
        encoding="utf-8")
    store = SettingsStore(path=path)
    w = MainWindow(FakeController(), store=store)
    assert [x.id for x in w._slaves] == ["s1"]
    assert w.slave_list.count() == 1
    assert "ib1" in w.log_view.toPlainText()
    # the one-time legacy migration rewrites the file: the dead IB slave is
    # gone and profiles: [] marks the migration as done
    assert "ib1" not in path.read_text(encoding="utf-8")
    assert store.load_config()["profiles"] == []


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
                        lambda on_quit, cached_wheel=None, resume=False:
                        captured.update({"on_quit": on_quit,
                                         "cached_wheel": cached_wheel,
                                         "resume": resume}))
    w = MainWindow(FakeController())
    w._cached_wheel = Path("C:/cached/manager-latest.whl")
    w._on_update_restart()
    assert captured["cached_wheel"] == Path("C:/cached/manager-latest.whl")


def test_update_restart_passes_resume_true_while_running(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager import updater
    captured = {}
    c = FakeController()
    c.started = True  # copying is live
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        captured.update({"resume": resume}))
    w = MainWindow(c)
    w._on_update_restart()
    assert captured["resume"] is True


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
    monkeypatch.setattr(se, "edit_slave", lambda parent, spec, **kw: edited)
    w._on_edit_slave()
    assert w._slaves[0].step_amount == 200.0
    assert w.slave_list.item(0).text() == "s1: terminal64.exe (own settings)"
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
    monkeypatch.setattr(se, "edit_slave", lambda parent, spec, **kw: None)  # cancel
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
    monkeypatch.setattr(se, "edit_slave", lambda parent, spec, **kw: edited)
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


def test_autostart_checkboxes_default_off(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert not w.autostart_boot_checkbox.isChecked()
    assert not w.autostart_copy_checkbox.isChecked()
    assert not w.autostart_cancel_button.isVisible()


def test_autostart_config_round_trip(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": True, "auto_copy": True}})
    lnk = tmp_path / "CopyTradesMT5.lnk"
    lnk.touch()
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    c = FakeController()
    w = MainWindow(c, store=store)
    if w._countdown_timer is not None:
        w._countdown_timer.stop()
    # auto_copy restored from stored value; on_boot synced to .lnk existence
    assert w.autostart_copy_checkbox.isChecked() is True
    assert w.autostart_boot_checkbox.isChecked() is True


def test_boot_checkbox_syncs_to_lnk_existence_on_load(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    lnk = tmp_path / "CopyTradesMT5.lnk"
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    store = SettingsStore(path=tmp_path / "settings.json")
    # stored on_boot True but no .lnk on disk -> checkbox unchecked (reality wins)
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [], "autostart": {"on_boot": True, "auto_copy": False}})
    w = MainWindow(FakeController(), store=store)
    assert w.autostart_boot_checkbox.isChecked() is False
    # now create the .lnk and reload via a fresh window -> checked
    lnk.touch()
    w2 = MainWindow(FakeController(), store=store)
    assert w2.autostart_boot_checkbox.isChecked() is True


def test_legacy_shortcut_without_task_logs_migration_hint(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    lnk = tmp_path / "CopyTradesMT5.lnk"
    lnk.touch()
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    w = MainWindow(FakeController(),
                   store=SettingsStore(path=tmp_path / "settings.json"))
    assert "elevated" in w.log_view.toPlainText().lower()


def _window_with_legacy_shortcut(tmp_path, monkeypatch, store=None):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    lnk = tmp_path / "CopyTradesMT5.lnk"
    lnk.touch()
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    return MainWindow(FakeController(), store=store or SettingsStore(
        path=tmp_path / "settings.json"))


def test_legacy_shortcut_shows_migrate_banner(qapp, tmp_path, monkeypatch):
    w = _window_with_legacy_shortcut(tmp_path, monkeypatch)
    assert w.autostart_migrate_banner.isHidden() is False


def test_no_migrate_banner_when_elevated_task_exists(
        qapp, tmp_path, monkeypatch):
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "task_exists", lambda: True)
    w = _window_with_legacy_shortcut(tmp_path, monkeypatch)
    assert w.autostart_migrate_banner.isHidden() is True


def test_no_migrate_banner_when_autostart_is_off(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    w = MainWindow(FakeController(),
                   store=SettingsStore(path=tmp_path / "settings.json"))
    assert w.autostart_migrate_banner.isHidden() is True


def test_migrate_banner_button_registers_elevated_task(
        qapp, tmp_path, monkeypatch):
    from manager.platform import autostart
    calls = {}

    def fake_enable(exe, arguments="-m manager", working_dir=None):
        calls["exe"] = exe
        calls["arguments"] = arguments

    monkeypatch.setattr(autostart, "enable_autostart", fake_enable)
    w = _window_with_legacy_shortcut(tmp_path, monkeypatch)

    w.autostart_migrate_button.click()

    assert calls["exe"] == sys.executable
    assert calls["arguments"] == "-m manager"
    assert w.autostart_migrate_banner.isHidden() is True
    assert w.autostart_boot_checkbox.isChecked() is True
    log = w.log_view.toPlainText().lower()
    assert "migrated" in log


def test_migrate_banner_failure_keeps_banner_and_logs(
        qapp, tmp_path, monkeypatch):
    from manager.platform import autostart

    def boom(*args, **kwargs):
        raise autostart.AutostartError("UAC cancelled")

    monkeypatch.setattr(autostart, "enable_autostart", boom)
    w = _window_with_legacy_shortcut(tmp_path, monkeypatch)

    w.autostart_migrate_button.click()

    assert w.autostart_migrate_banner.isHidden() is False
    assert "migration failed" in w.log_view.toPlainText().lower()


def _patch_needs_elevation(monkeypatch):
    from manager.platform import autologon_repair
    monkeypatch.setattr(
        autologon_repair, "repair_on_startup",
        lambda: autologon_repair.RepairResult(
            reasons=("ForceAutoLogon=1",), needs_elevation=True))


def test_autologon_repair_banner_shown_when_needs_elevation(
        qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    _patch_needs_elevation(monkeypatch)
    w = MainWindow(FakeController())
    assert w.autologon_repair_banner.isHidden() is False


def test_autologon_repair_button_hides_banner_and_logs(
        qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.platform import autologon_repair
    _patch_needs_elevation(monkeypatch)
    w = MainWindow(FakeController())
    monkeypatch.setattr(
        autologon_repair, "repair_now",
        lambda: autologon_repair.RepairResult(changed=("ForceAutoLogon=1",)))
    w.autologon_repair_button.click()
    assert w.autologon_repair_banner.isHidden() is True
    assert "autologon repaired" in w.log_view.toPlainText().lower()


def test_autologon_startup_self_heal_is_logged(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.platform import autologon_repair
    monkeypatch.setattr(
        autologon_repair, "repair_on_startup",
        lambda: autologon_repair.RepairResult(changed=("ForceAutoLogon=1",)))
    w = MainWindow(FakeController())
    assert "autologon self-heal" in w.log_view.toPlainText().lower()


def test_autologon_setup_button_visible_when_not_configured(
        qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.platform import autologon_repair
    monkeypatch.setattr(autologon_repair, "setup_configured", lambda: False)
    w = MainWindow(FakeController())
    assert w.autologon_setup_button.isHidden() is False


def test_autologon_setup_button_downloads_and_launches(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.platform import autologon_repair
    monkeypatch.setattr(autologon_repair, "setup_configured", lambda: False)
    calls = {}
    monkeypatch.setattr(autologon_repair, "download_setup_script",
                        lambda: Path("C:/tmp/enable.ps1"))
    monkeypatch.setattr(autologon_repair, "launch_setup_script",
                        lambda path: calls.setdefault("path", path))
    w = MainWindow(FakeController())
    w.autologon_setup_button.click()
    assert calls["path"] == Path("C:/tmp/enable.ps1")
    assert "setup finished" in w.log_view.toPlainText().lower()


def test_autologon_setup_failure_is_logged(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.platform import autologon_repair
    monkeypatch.setattr(autologon_repair, "setup_configured", lambda: False)

    def boom():
        raise autologon_repair.AutologonRepairError("download failed")

    monkeypatch.setattr(autologon_repair, "download_setup_script", boom)
    w = MainWindow(FakeController())
    w.autologon_setup_button.click()
    assert "setup failed" in w.log_view.toPlainText().lower()


def test_autostart_disable_failure_keeps_checkbox_checked(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    lnk = tmp_path / "CopyTradesMT5.lnk"
    lnk.touch()
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    w = MainWindow(FakeController(),
                   store=SettingsStore(path=tmp_path / "settings.json"))
    assert w.autostart_boot_checkbox.isChecked() is True

    def boom():
        raise autostart.AutostartError("nope")

    monkeypatch.setattr(autostart, "disable_autostart", boom)
    w.autostart_boot_checkbox.setChecked(False)
    assert w.autostart_boot_checkbox.isChecked() is True
    assert "autostart disable failed" in w.log_view.toPlainText()


def test_start_silent_logs_on_algo_trading_disabled_no_modal(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui import main_window as mw
    from manager.app.controller import AlgoTradingDisabledError, AccountSpec
    c = FakeController()
    c.start = lambda master, slaves, **kw: (_ for _ in ()).throw(
        AlgoTradingDisabledError(["s1 (C:/t/terminal64.exe)"]))
    shown = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda parent, title, text: shown.append((title, text)) or 0)
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe")]
    w._start_silent()
    assert not shown, "auto-start path must not show a modal"
    assert "auto-start failed" in w.log_view.toPlainText().lower()


def test_do_start_manual_shows_modal_on_algo_trading_disabled(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui import main_window as mw
    from manager.app.controller import AlgoTradingDisabledError
    c = FakeController()
    c.start = lambda master, slaves, **kw: (_ for _ in ()).throw(
        AlgoTradingDisabledError(["s1 (C:/t/terminal64.exe)"]))
    shown = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda parent, title, text: shown.append((title, text)) or 0)
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w.start_button.click()  # manual path -> _on_start -> _do_start(show_modal=True)
    assert shown, "manual Start must still show the Algo Trading modal"
    assert "Algo Trading" in shown[0][0]


def test_autostart_copy_countdown_skipped_when_toggle_off(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False}})
    c = FakeController()
    w = MainWindow(c, store=store)
    assert w._countdown_timer is None
    assert c.started is False


def test_resume_copy_flag_begins_countdown_even_when_toggle_off(
        qapp, tmp_path, monkeypatch):
    """--resume-copy (set by update_helper's relaunch after an update-restart)
    resumes copying via the same countdown machinery regardless of the saved
    auto_copy toggle. The flag is one-shot argv — normal launches are idle."""
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False}})
    c = FakeController()
    w = MainWindow(c, store=store, resume_copy=True)
    assert w._countdown_timer is not None  # countdown began like boot auto-copy
    # cancel works and returns the app to idle
    w._cancel_autostart_copy()
    assert w._countdown_timer is None
    assert c.started is False
    assert "auto-start cancelled" in w.log_view.toPlainText().lower()


def test_autostart_copy_countdown_skipped_when_config_incomplete(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    # auto_copy on, master set, but NO slaves -> skip
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [], "autostart": {"on_boot": False, "auto_copy": True}})
    c = FakeController()
    w = MainWindow(c, store=store)
    assert w._countdown_timer is None
    assert c.started is False
    assert "auto-start skipped" in w.log_view.toPlainText().lower()


def test_autostart_copy_countdown_fires_start_at_zero(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": True}})
    c = FakeController()
    w = MainWindow(c, store=store)
    # countdown began on construction
    assert w._countdown_timer is not None
    # drive it deterministically: stop the real timer, set 1 s left, tick once
    w._countdown_timer.stop()
    w._countdown_remaining = 1
    w._autostart_tick()
    assert c.started is True
    assert w._countdown_timer is None
    assert not w.autostart_cancel_button.isVisible()


def test_autostart_cancel_button_aborts(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": True}})
    c = FakeController()
    w = MainWindow(c, store=store)
    assert w._countdown_timer is not None
    w._cancel_autostart_copy()
    assert w._countdown_timer is None
    assert c.started is False
    assert "auto-start cancelled" in w.log_view.toPlainText().lower()
    # buttons re-enabled to the not-running state
    assert w.start_button.isEnabled()
    assert not w.stop_button.isEnabled()


def _copy_profile(pid="p1", name="A", **kw):
    from manager.app.controller import CopyProfile
    return CopyProfile(id=pid, name=name, **kw)


def test_profiles_group_constructs(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.profile_list is not None
    assert w.add_profile_button.text() == "New…"
    assert w.edit_profile_button.text() == "Edit…"
    assert w.duplicate_profile_button.text() == "Duplicate…"
    assert w.delete_profile_button.text() == "Delete"


def test_add_profile_appends_selects_and_saves(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    w = MainWindow(FakeController(), store=store)
    monkeypatch.setattr("manager.gui.profile_editor.new_profile",
                        lambda **kw: _copy_profile())
    w._on_add_profile()
    assert [p.id for p in w._profiles] == ["p1"]
    assert w.profile_list.currentRow() == 0
    assert store.load_config()["profiles"][0]["name"] == "A"


def test_edit_profile_updates_attached_slaves_and_applies_live(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    c = FakeController()
    w = MainWindow(c, store=store)
    w._profiles = [_copy_profile(step_amount=100.0)]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0, profile_id="p1")]
    w._refresh_profile_list("p1")
    w._refresh_slave_list()
    c.started = True  # live apply path
    monkeypatch.setattr("manager.gui.profile_editor.edit_profile",
                        lambda profile, **kw: _copy_profile(step_amount=500.0))
    w._on_edit_profile()
    assert w._profiles[0].step_amount == 500.0
    assert w._slaves[0].step_amount == 500.0
    assert [sid for sid, _ in c.applied_edits] == ["s1"]
    assert store.load_config()["slaves"][0]["step_amount"] == 500.0


def test_duplicate_profile_gets_new_id_and_unique_copy_name(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    w = MainWindow(FakeController(), store=SettingsStore(tmp_path / "s.json"))
    w._profiles = [_copy_profile(name="A")]
    w._refresh_profile_list("p1")
    seen = {}
    def fake_new(**kw):
        seen.update(kw)
        return _copy_profile(pid="p2", name="A (copy)")
    monkeypatch.setattr("manager.gui.profile_editor.new_profile", fake_new)
    w._on_duplicate_profile()
    assert seen["seed"].name == "A (copy)"
    assert [p.id for p in w._profiles] == ["p1", "p2"]


def test_delete_profile_blocked_while_attached(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from PySide6.QtWidgets import QMessageBox
    w = MainWindow(FakeController())
    w._profiles = [_copy_profile()]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             profile_id="p1")]
    w._refresh_profile_list("p1")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    w._on_delete_profile()
    assert warned and [p.id for p in w._profiles] == ["p1"]


def test_delete_profile_allowed_when_unused(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._profiles = [_copy_profile()]
    w._refresh_profile_list("p1")
    w._on_delete_profile()
    assert w._profiles == []


def test_slave_label_shows_profile_name_or_own_settings(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    w = MainWindow(FakeController())
    w._profiles = [_copy_profile(name="A")]
    p = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                    profile_id="p1")
    assert w._slave_label(p) == "s1: terminal64.exe (A)"
    p.profile_id = ""
    assert w._slave_label(p) == "s1: terminal64.exe (own settings)"


def test_config_round_trip_restores_profiles_and_attachments(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    w = MainWindow(FakeController(), store=store)
    w.master_terminal.addItem("C:/m/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w._profiles = [_copy_profile(step_amount=500.0)]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=500.0, profile_id="p1")]
    w._save_config()
    w2 = MainWindow(FakeController(), store=store)
    assert [p.id for p in w2._profiles] == ["p1"]
    assert w2._slaves[0].profile_id == "p1"
    assert w2.slave_list.item(0).text() == "s1: terminal64.exe (A)"


def test_load_migrates_identical_legacy_slaves_once(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "slaves": [
            {"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
             "step_amount": 100.0},
            {"id": "s2", "terminal_path": "C:/s2/terminal64.exe",
             "step_amount": 100.0},
            {"id": "s3", "terminal_path": "C:/s3/terminal64.exe",
             "step_amount": 500.0},
        ],
    })
    w = MainWindow(FakeController(), store=store)
    assert len(w._profiles) == 1
    assert w._profiles[0].name == "s1 + s2"
    assert w._slaves[0].profile_id == w._profiles[0].id
    assert w._slaves[1].profile_id == w._profiles[0].id
    assert w._slaves[2].profile_id == ""
    assert "migrated slaves s1, s2" in w.log_view.toPlainText()
    cfg = store.load_config()
    assert len(cfg["profiles"]) == 1
    assert cfg["master"]["terminal_path"] == "C:/m/terminal64.exe"
    w2 = MainWindow(FakeController(), store=store)
    assert len(w2._profiles) == 1  # second start does not migrate again


def test_malformed_profile_drops_and_slave_falls_back(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "profiles": [{"id": "p1", "name": "A", "step_amount": "abc"}],
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "profile_id": "p1", "step_amount": 123.0}],
    })
    w = MainWindow(FakeController(), store=store)
    assert w._profiles == []
    assert w._slaves[0].profile_id == ""
    assert w._slaves[0].step_amount == 123.0
    assert "dropped" in w.log_view.toPlainText()


def test_unknown_profile_id_falls_back_to_stored_settings(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "profiles": [],
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "profile_id": "gone", "step_amount": 123.0}],
    })
    w = MainWindow(FakeController(), store=store)
    assert w._slaves[0].profile_id == ""
    assert w._slaves[0].step_amount == 123.0
    assert "not found" in w.log_view.toPlainText()


def test_legacy_config_with_stale_profile_id_is_treated_as_legacy(
        qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "profile_id": "gone", "step_amount": 100.0}],
    })
    w = MainWindow(FakeController(), store=store)
    assert w._profiles == []
    assert w._slaves[0].profile_id == ""
    assert store.load_config()["profiles"] == []  # migration marked as done


def test_migration_failure_is_logged_and_does_not_crash(
        qapp, tmp_path, monkeypatch):
    """A corrupt legacy config can make migrate_legacy_profiles raise (the
    loader does no type enforcement). MainWindow must still construct, leave
    the slaves on their own settings, and still persist profiles: []."""
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    import manager.gui.main_window as mw
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "step_amount": [1, 2]}],
    })

    def boom(slaves):
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(mw, "migrate_legacy_profiles", boom)
    w = MainWindow(FakeController(), store=store)
    assert w._slaves[0].profile_id == ""
    assert "migration failed" in w.log_view.toPlainText()
    cfg = store.load_config()
    assert cfg["profiles"] == []
    assert cfg["master"]["terminal_path"] == "C:/m/terminal64.exe"


def test_corrupt_but_hashable_legacy_fields_do_not_crash(qapp, tmp_path):
    """A legacy config whose identical slaves carry a corrupt-but-hashable
    shared field (symbol_map_csv=5) must not crash: the grouper succeeds, but
    the minted profile is invalid and must be dropped before the list refresh
    builds its tooltip (parse_symbol_map would raise AttributeError)."""
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "slaves": [
            {"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
             "symbol_map_csv": 5, "step_amount": 100.0},
            {"id": "s2", "terminal_path": "C:/s2/terminal64.exe",
             "symbol_map_csv": 5, "step_amount": 100.0},
        ],
    })
    w = MainWindow(FakeController(), store=store)
    assert w._profiles == []
    assert [s.profile_id for s in w._slaves] == ["", ""]
    assert w.log_view.toPlainText()  # the drop is reported
    cfg = store.load_config()
    assert cfg["profiles"] == []
    assert cfg["master"]["terminal_path"] == "C:/m/terminal64.exe"


def test_stored_profile_with_huge_integer_does_not_crash(qapp, tmp_path):
    """A huge JSON integer in a stored profile makes float() raise
    OverflowError. The loader must drop that profile, install none, log, and
    still construct the window."""
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "profiles": [{"id": "p1", "name": "A",
                      "step_amount": int("9" * 400)}],
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "profile_id": "p1", "step_amount": 100.0}],
    })
    w = MainWindow(FakeController(), store=store)  # must not raise
    assert w._profiles == []
    assert w._slaves[0].profile_id == ""
    assert w._slaves[0].step_amount == 100.0
    assert "step_amount" in w.log_view.toPlainText()


def test_corrupt_settings_file_is_left_untouched_on_startup(qapp, tmp_path):
    """A corrupt settings.json returns {} from the store; startup must not
    rewrite it (no migration branch), and must log that config was unreadable."""
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    path = tmp_path / "settings.json"
    path.write_text("{this is not json", encoding="utf-8")
    before = path.read_bytes()
    w = MainWindow(FakeController(), store=SettingsStore(path=path))
    assert path.read_bytes() == before
    assert w.log_view.toPlainText()


def test_missing_settings_file_is_not_created_on_startup(qapp, tmp_path):
    """A fresh install has no settings.json; constructing the window must not
    create one (no empty-config migration/save)."""
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    path = tmp_path / "settings.json"
    MainWindow(FakeController(), store=SettingsStore(path=path))
    assert not path.exists()


def test_save_materializes_attached_slaves_from_profile(qapp, tmp_path):
    """A manually drifted attached spec is written with the profile's values,
    so the store's materialization invariant self-heals on every save."""
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    w = MainWindow(FakeController(), store=store)
    w._profiles = [_copy_profile(step_amount=500.0, max_lot=20.0)]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=999.0, max_lot=1.0, profile_id="p1")]
    w._save_config()
    cfg = store.load_config()
    assert cfg["slaves"][0]["step_amount"] == 500.0
    assert cfg["slaves"][0]["max_lot"] == 20.0
    assert cfg["slaves"][0]["profile_id"] == "p1"


def test_profile_tooltip_includes_mode_relevant_lot(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    fixed = w._profile_tooltip(_copy_profile(sizing_mode="fixed_lot",
                                             fixed_lot=0.07))
    assert "fixed 0.07" in fixed
    balance = w._profile_tooltip(_copy_profile(sizing_mode="balance_step",
                                               master_base_lot=0.2))
    assert "base 0.2" in balance


def test_profile_label_is_the_name(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._profiles = [_copy_profile(name="Propfirm A")]
    w._refresh_profile_list()
    assert w._profile_label(w._profiles[0]) == "Propfirm A"
    assert w.profile_list.item(0).text() == "Propfirm A"


def test_edit_profile_not_running_does_not_live_apply(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    c = FakeController()  # not started
    w = MainWindow(c, store=SettingsStore(tmp_path / "s.json"))
    w._profiles = [_copy_profile(step_amount=100.0)]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0, profile_id="p1")]
    w._refresh_profile_list("p1")
    w._refresh_slave_list()
    monkeypatch.setattr("manager.gui.profile_editor.edit_profile",
                        lambda profile, **kw: _copy_profile(step_amount=500.0))
    w._on_edit_profile()
    assert w._slaves[0].step_amount == 500.0
    assert c.applied_edits == []  # not running -> no live apply


def test_edit_profile_updates_and_live_applies_all_attached_slaves(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    c = FakeController()
    c.started = True  # live apply path
    w = MainWindow(c, store=SettingsStore(tmp_path / "s.json"))
    w._profiles = [_copy_profile(step_amount=100.0)]
    w._slaves = [
        AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                    step_amount=100.0, profile_id="p1"),
        AccountSpec(id="s2", terminal_path="C:/s2/terminal64.exe",
                    step_amount=100.0, profile_id="p1"),
    ]
    w._refresh_profile_list("p1")
    w._refresh_slave_list()
    monkeypatch.setattr("manager.gui.profile_editor.edit_profile",
                        lambda profile, **kw: _copy_profile(step_amount=500.0))
    w._on_edit_profile()
    assert [s.step_amount for s in w._slaves] == [500.0, 500.0]
    assert [sid for sid, _ in c.applied_edits] == ["s1", "s2"]


def test_long_master_terminal_path_does_not_widen_the_window(qapp):
    from manager.gui.main_window import MainWindow
    from manager.terminal.discovery import TerminalInstance
    path = "C:/" + "x" * 240 + "/terminal64.exe"
    c = FakeController()
    c._instances = [TerminalInstance(path.rsplit("/", 1)[0], path, "appdata")]
    w = MainWindow(c)
    w.master_terminal.setCurrentText(path)
    w.layout().activate()
    qapp.processEvents()
    assert w.minimumSizeHint().width() < 1200


def test_long_names_are_available_as_list_tooltips(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    w = MainWindow(FakeController())
    name = "P" * 200
    w._profiles = [_copy_profile(name=name)]
    w._refresh_profile_list("p1")
    w._slaves = [AccountSpec(id="S" * 200,
                             terminal_path="C:/s/terminal64.exe")]
    w._refresh_slave_list()
    assert name in w.profile_list.item(0).toolTip()
    assert "S" * 200 in w.slave_list.item(0).toolTip()


def test_content_splitter_holds_slaves_and_lower_panel(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSplitter
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert isinstance(w.content_splitter, QSplitter)
    assert w.content_splitter.orientation() == Qt.Orientation.Vertical
    assert w.content_splitter.childrenCollapsible() is False
    assert w.content_splitter.widget(0) is w.slave_box
    assert w.content_splitter.widget(1) is w.lower_panel
    assert w.slave_list.minimumHeight() >= 80


def test_splitter_sizes_round_trip(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    w = MainWindow(FakeController(), store=store)
    w.resize(900, 1600)
    w.show()
    qapp.processEvents()
    w.content_splitter.setSizes([600, 400])
    qapp.processEvents()
    chosen = w.content_splitter.sizes()
    w._save_config()
    assert store.load_config()["ui"]["splitter_sizes"] == chosen
    w2 = MainWindow(FakeController(), store=store)
    w2.resize(900, 1600)
    w2.show()
    qapp.processEvents()
    sizes = w2.content_splitter.sizes()
    assert abs(sizes[0] - chosen[0]) <= 40
    assert abs(sizes[1] - chosen[1]) <= 40


def test_malformed_splitter_sizes_fall_back(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "profiles": [], "slaves": [],
                       "ui": {"splitter_sizes": ["x", -1]}})
    w = MainWindow(FakeController(), store=store)
    sizes = w.content_splitter.sizes()
    assert len(sizes) == 2 and sum(sizes) > 0
    assert "splitter" in w.log_view.toPlainText().lower()


def test_giving_the_slaves_pane_more_size_enlarges_the_list(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    w = MainWindow(FakeController())
    w._slaves = [AccountSpec(id=f"s{i}",
                             terminal_path=f"C:/s{i}/terminal64.exe")
                 for i in range(30)]
    w._refresh_slave_list()
    w.resize(900, 1600)
    w.show()
    qapp.processEvents()
    w.content_splitter.setSizes([700, 150])
    qapp.processEvents()
    big = w.slave_list.height()
    w.content_splitter.setSizes([120, 730])
    qapp.processEvents()
    small = w.slave_list.height()
    assert big > small
