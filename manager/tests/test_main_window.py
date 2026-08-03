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
    def stop(self):
        self.stopped = True
    def is_running(self):
        return self.started and not self.stopped


def test_main_window_constructs(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.windowTitle()  # has a title
    # core controls exist
    assert w.master_login is not None
    assert w.master_server is not None
    assert w.master_terminal is not None
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


def test_start_button_calls_controller_start(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    # provide a master login + server so start() is callable
    w.master_login.setText("5001")
    w.master_server.setText("Demo")
    w.start_button.click()
    assert c.started


def test_stop_button_calls_controller_stop(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    # Stop is disabled until a session is running; enable it to mimic that
    # state so the click fires the handler.
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