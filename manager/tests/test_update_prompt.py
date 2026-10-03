import pytest

pytest.importorskip("PySide6")


class _Capture:
    """Collects which callback fired: 'now' / 'delay' / 'dismiss'."""

    def __init__(self):
        self.calls = []

    def now(self):
        self.calls.append("now")

    def delay(self):
        self.calls.append("delay")

    def dismiss(self):
        self.calls.append("dismiss")


def _make_prompt(cap, countdown_s=60):
    from manager.gui.update_prompt import UpdateRestartPrompt
    return UpdateRestartPrompt(
        "0.1.4",
        on_now=cap.now, on_delay=cap.delay, on_dismiss=cap.dismiss,
        countdown_s=countdown_s)


def test_prompt_shows_version_countdown_and_buttons(qapp):
    cap = _Capture()
    w = _make_prompt(cap)
    assert "0.1.4" in w.message.text()
    assert "Restarting in 60 s" in w.message.text()
    assert w.now_button.text() == "Restart now"
    assert w.delay_button.text() == "Delay 5 minutes"


def test_countdown_reaches_zero_fires_restart_now(qapp):
    cap = _Capture()
    w = _make_prompt(cap)
    w._timer.stop()
    w._remaining = 1
    w._tick()
    assert cap.calls == ["now"]
    assert not w._timer.isActive()


def test_restart_now_button_fires_immediately(qapp):
    cap = _Capture()
    w = _make_prompt(cap)
    w.now_button.click()
    assert cap.calls == ["now"]
    assert not w._timer.isActive()


def test_delay_button_fires_delay_callback(qapp):
    cap = _Capture()
    w = _make_prompt(cap)
    w.delay_button.click()
    assert cap.calls == ["delay"]
    assert not w._timer.isActive()  # no stray countdown keeps running


def test_close_or_esc_fires_dismiss_callback(qapp):
    # reject() is what the window-X button and Esc both route to on a QDialog
    cap = _Capture()
    w = _make_prompt(cap)
    w.reject()
    assert cap.calls == ["dismiss"]
    assert not w._timer.isActive()


def test_countdown_s_parameter_customizes_duration(qapp):
    cap = _Capture()
    w = _make_prompt(cap, countdown_s=7)
    assert "Restarting in 7 s" in w.message.text()