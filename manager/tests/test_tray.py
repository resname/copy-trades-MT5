# manager/tests/test_tray.py
import pytest

pytest.importorskip("PySide6")


class FakeController:
    def __init__(self):
        self.stopped = False
    def stop(self):
        self.stopped = True


def test_tray_constructs(qapp):
    from manager.gui.tray import TrayIcon
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    tray = TrayIcon(c)
    tray.install(w)
    assert tray.menu is not None
    assert tray.show_action is not None
    assert tray.quit_action is not None


def test_quit_action_stops_controller_then_quits(qapp, monkeypatch):
    from manager.gui.tray import TrayIcon
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    tray = TrayIcon(c)
    tray.install(w)
    quit_called = []
    monkeypatch.setattr("manager.gui.tray.QApplication.quit",
                        lambda: quit_called.append(True))
    tray.quit_action.trigger()
    assert c.stopped
    assert quit_called


def test_show_action_unhides_window(qapp):
    from manager.gui.tray import TrayIcon
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    tray = TrayIcon(c)
    tray.install(w)
    w.hide()
    assert not w.isVisible()
    tray.show_action.trigger()
    assert w.isVisible()