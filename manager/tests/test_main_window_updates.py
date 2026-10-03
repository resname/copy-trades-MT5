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
    # the BUTTON gating no longer considers copying: while the wheel is still
    # downloading it is disabled for any state; once ready it enables even
    # while copying (see test_ready_update_enabled_even_while_running).
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


def test_ready_update_enabled_even_while_running(qapp, monkeypatch):
    # Update & restart is allowed mid-copy: the quit path orders the stop, and
    # the relaunched manager resumes copying (resume flag chain).
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=True))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))  # wheel ready
    assert w.update_restart_button.isEnabled() is True  # while copying


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
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append((on_quit, resume)))
    w = MainWindow(FakeController(running=False))
    w._on_update_restart()
    assert len(calls) == 1
    # the on_quit passed in is the window's _do_update_quit (bound method)
    assert calls[0][0] == w._do_update_quit
    assert calls[0][1] is False  # nothing was copying -> no resume flag


def test_update_restart_while_copying_passes_resume_true(qapp, monkeypatch):
    # was the reason for the old refusal; now the click is allowed mid-copy and
    # records the running state so the relaunched manager resumes it.
    from manager.gui.main_window import MainWindow
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append(resume))
    w = MainWindow(FakeController(running=True))
    w._on_update_restart()
    assert calls == [True]


def test_auto_update_shows_countdown_popup_when_ready(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: tmp_path / "nope.lnk")
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append(resume))
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False,
                                     "auto_update": True}})
    w = MainWindow(FakeController(running=False), store=store)
    assert w.autostart_auto_update_checkbox.isChecked()
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    # the popup appears instead of restarting immediately
    assert w._update_prompt is not None
    assert "0.1.2" in w._update_prompt.message.text()
    assert calls == []  # nothing fires until the countdown runs out / clicked
    # the mark happens when the popup first SHOWS (restart-loop guard), before
    # any restart — so a pip failure relaunching the old version cannot loop
    w2 = MainWindow(FakeController(running=False), store=store)
    assert w2._auto_update_last_attempt == "0.1.2"
    # countdown reaching zero fires the same update-restart path
    w._update_prompt._timer.stop()
    w._update_prompt._remaining = 1
    w._update_prompt._tick()
    assert calls == [False]  # auto-restart fired (not copying -> no resume)
    # cleanup: stop timers so pytest can reap qapp threads cleanly
    w._update_timer.stop(); w2._update_timer.stop()


def test_auto_update_skips_version_already_attempted(qapp, tmp_path, monkeypatch):
    # pip-failed loop guard: the helper relaunched the OLD version, which finds
    # the same update again -> no auto-restart for a version we already tried.
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: tmp_path / "nope.lnk")
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append(resume))
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False,
                                     "auto_update": True,
                                     "auto_update_last_attempt": "0.1.2"}})
    w = MainWindow(FakeController(running=False), store=store)
    assert w._auto_update_last_attempt == "0.1.2"
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    # same version already attempted -> no popup, no auto-restart
    assert w._update_prompt is None
    assert calls == []
    w._update_timer.stop()


def test_auto_update_fires_again_for_newer_version(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: tmp_path / "nope.lnk")
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append(resume))
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False,
                                     "auto_update": True,
                                     "auto_update_last_attempt": "0.1.2"}})
    w = MainWindow(FakeController(running=False), store=store)
    w._on_update_checked(UpdateInfo(available=True, current="0.1.2", latest="0.1.3"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert w._update_prompt is not None
    w._update_prompt._timer.stop()
    w._update_prompt._remaining = 1
    w._update_prompt._tick()
    assert calls == [False]
    w._update_timer.stop()


def test_auto_update_off_never_triggers_restart(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: tmp_path / "nope.lnk")
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append(resume))
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False,
                                     "auto_update": False}})
    w = MainWindow(FakeController(running=False), store=store)
    assert not w.autostart_auto_update_checkbox.isChecked()
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert calls == []
    assert w._update_prompt is None
    w._update_timer.stop()


def test_auto_update_delay_defers_restart_by_five_minutes(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: tmp_path / "nope.lnk")
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append(resume))
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False,
                                     "auto_update": True}})
    w = MainWindow(FakeController(running=False), store=store)
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert w._update_prompt is not None
    # user hits "Delay 5 minutes"
    w._update_prompt.delay_button.click()
    assert calls == []  # no restart
    assert w._update_prompt is None  # popup hidden
    # the guard mark stays in place — a deliberate delay is the user opting in
    # to a repeat prompt, it does not re-arm the once-per-version guard
    assert w._auto_update_last_attempt == "0.1.2"
    # an in-memory single-shot timer re-prompts 5 minutes later
    assert w._auto_update_delay_timer is not None
    assert w._auto_update_delay_timer.isActive()
    assert w._auto_update_delay_timer.interval() == 5 * 60 * 1000
    # simulate the timeout elapsing -> a fresh popup shows, restart still fires
    w._auto_update_delay_timer.stop()
    w._show_update_prompt()
    assert w._update_prompt is not None  # the popup re-appears after 5 min
    assert calls == []  # and the countdown restarts — nothing fires yet
    w._update_prompt._timer.stop()
    w._update_prompt._remaining = 1
    w._update_prompt._tick()
    assert calls == [False]
    w._update_timer.stop()


def test_auto_update_dismiss_reverts_mark_so_check_reprompts(qapp, tmp_path, monkeypatch):
    # Closing the popup (X / Esc) is "not now": the once-per-version mark is
    # reverted so the next hourly check prompts again rather than silently
    # never auto-applying. The PRIOR mark is restored, not wiped.
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: tmp_path / "nope.lnk")
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None, resume=False:
                        calls.append(resume))
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False,
                                     "auto_update": True,
                                     "auto_update_last_attempt": "0.1.2"}})
    w = MainWindow(FakeController(running=False), store=store)
    w._on_update_checked(UpdateInfo(available=True, current="0.1.2", latest="0.1.3"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert w._update_prompt is not None
    w._update_prompt.reject()  # window-X / Esc route through reject()
    assert calls == []  # no restart
    assert w._auto_update_last_attempt == "0.1.2"  # prior mark restored
    # persisted: a fresh window would prompt again for 0.1.3
    w2 = MainWindow(FakeController(running=False), store=store)
    assert w2._auto_update_last_attempt == "0.1.2"
    w._update_timer.stop(); w2._update_timer.stop()