import pytest

pytest.importorskip("PySide6")


class FakeController:
    """Minimal controller double for construction + wiring smoke tests."""
    def __init__(self):
        self.started = False
        self.stopped = False
        self._instances = []
    def discover_instances(self):
        return self._instances
    def start(self, master, slaves, **kw):
        self.started = True
        self.last_master = master
    def stop(self):
        self.stopped = True
    def is_running(self):
        return self.started and not self.stopped


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
