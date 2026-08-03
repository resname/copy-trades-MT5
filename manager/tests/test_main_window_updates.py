import pytest

pytest.importorskip("PySide6")

from manager.updater import UpdateInfo


class FakeController:
    def __init__(self, running=False):
        self._running = running
        self.stopped = []
    def is_running(self):
        return self._running
    def stop(self):
        self.stopped.append(True)
    def discover_instances(self):
        return []


def test_update_ui_exists(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.check_update_button.text().lower().startswith("check")
    assert w.update_restart_button.isVisible() is False


def test_update_available_enables_restart_only_when_idle(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert "0.1.2" in w.update_label.text()
    assert w.update_restart_button.isVisible() is True
    assert w.update_restart_button.isEnabled() is True


def test_update_available_disables_restart_while_running(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController(running=True))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert w.update_restart_button.isEnabled() is False


def test_up_to_date_hides_restart(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._on_update_checked(UpdateInfo(available=False, current="0.1.1", latest="0.1.1"))
    assert "up to date" in w.update_label.text().lower()
    assert w.update_restart_button.isVisible() is False


def test_check_failed_label(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._on_update_checked(UpdateInfo(available=False, current="0.1.1", latest=None))
    assert "couldn't" in w.update_label.text().lower()


def test_update_restart_calls_updater_and_quits(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit: calls.append(on_quit))
    w = MainWindow(FakeController(running=False))
    w._on_update_restart()
    assert len(calls) == 1
    # the on_quit passed in is the window's _do_update_quit (bound method)
    assert calls[0] == w._do_update_quit


def test_update_restart_refuses_while_running(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit: calls.append(on_quit))
    w = MainWindow(FakeController(running=True))
    w._on_update_restart()
    assert calls == []  # refused; nothing spawned