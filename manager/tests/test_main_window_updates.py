import pytest

pytest.importorskip("PySide6")

from pathlib import Path
from manager.updater import UpdateInfo


class FakeController:
    def __init__(self, running=False):
        self._running = running
        self.stopped = []
    def is_running(self):
        return self._running
    def stop(self):
        # Mirror production CopyController.stop(), which sets supervisor=None
        # so is_running() returns False. Lets tests model _on_stop's real
        # controller.stop() -> set_running(False) flow.
        self._running = False
        self.stopped.append(True)
    def discover_instances(self):
        return []


class _StubSignal:
    """Stand-in for a Qt Signal: accepts .connect() and does nothing."""
    def connect(self, *args, **kwargs):
        pass


class _NoThreadUpdateWorker:
    """Test double for manager.gui.main_window._UpdateWorker.

    _on_update_checked(available=True) starts a real fire-and-forget QThread
    running updater.download_update(). In the real app that is fine (process
    exit reaps the thread), but in the pytest session an unjoined QThread is
    destroyed while still running at interpreter shutdown → the process exits
    non-zero (on CI: no "X passed" summary line). These tests only check the
    label/button state, so they substitute this no-op worker to avoid leaking
    a running thread (and a real network download) into the test session.
    """
    done = _StubSignal()
    def __init__(self, fn, parent=None):
        self._fn = fn
    def start(self):
        pass  # no real thread, no network I/O


class _NoThreadDownloadWorker:
    """Test double for manager.gui.main_window._DownloadWorker. The real
    worker runs updater.download_update(progress=cb) on a QThread (a real
    network download); tests only check label/button/bar state, so this
    no-op worker avoids leaking a running thread and network I/O. Its
    done/progress signals are never emitted, so _on_predownload_done is never
    called (button stays disabled = the 'still downloading' state)."""
    done = _StubSignal()
    progress = _StubSignal()
    def __init__(self, fn, parent=None):
        self._fn = fn
    def start(self):
        pass


def test_update_ui_exists(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.check_update_button.text().lower().startswith("check")
    assert w.update_restart_button.isVisibleTo(w) is False


def test_update_available_disables_restart_until_downloaded(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert "0.1.2" in w.update_label.text()
    assert w.update_restart_button.isVisibleTo(w) is True
    # greyed out until the predownloaded wheel is verified ready
    assert w.update_restart_button.isEnabled() is False
    # the progress bar is shown while the download is in progress
    assert w.update_progress.isVisibleTo(w) is True


def test_update_available_disables_restart_while_running(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=True))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert w.update_restart_button.isEnabled() is False


def test_up_to_date_hides_restart(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._on_update_checked(UpdateInfo(available=False, current="0.1.1", latest="0.1.1"))
    assert "up to date" in w.update_label.text().lower()
    assert w.update_restart_button.isVisibleTo(w) is False


def test_check_failed_label(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._on_update_checked(UpdateInfo(available=False, current="0.1.1", latest=None))
    assert "couldn't" in w.update_label.text().lower()


def test_update_ready_enables_restart_when_idle(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert w.update_restart_button.isEnabled() is False
    # simulate the predownload finishing with a verified wheel
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert w.update_restart_button.isEnabled() is True
    # the bar hides once the wheel is ready
    assert w.update_progress.isVisibleTo(w) is False


def test_ready_update_stays_disabled_while_running_and_enables_on_stop(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=True))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))  # wheel ready
    assert w.update_restart_button.isEnabled() is False  # still copying
    w._controller.stop()   # mirrors _on_stop's controller.stop()
    w.set_running(False)   # mirrors _on_stop's set_running(False)
    assert w.update_restart_button.isEnabled() is True


def test_download_progress_updates_bar(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_download_progress(50, 200)   # 50 of 200 bytes -> 25%
    assert w.update_progress.maximum() == 100
    assert w.update_progress.value() == 25
    w._on_download_progress(0, -1)     # unknown total -> indeterminate
    assert w.update_progress.maximum() == 0  # setRange(0,0) makes max 0


def test_predownload_failure_keeps_button_disabled(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(None)  # download/verify failed
    assert w.update_restart_button.isEnabled() is False
    assert "failed" in w.update_label.text().lower()
    assert w.update_progress.isVisibleTo(w) is False


def test_update_restart_calls_updater_and_quits(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None: calls.append(on_quit))
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