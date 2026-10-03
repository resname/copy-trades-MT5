"""Countdown popup for the 'Auto restart when update is available' toggle.

Non-modal, always-on-top QDialog: counts down (default 60 s) and fires
``on_now`` at zero, the same ``_on_update_restart`` path a manual click uses.
The user can restart early ("Restart now"), push it back ("Delay 5 minutes" —
the main window re-shows the popup via an in-memory timer), or close it
(window X / Esc) — closing routes through ``reject()``, which fires
``on_dismiss`` so the manager can revert its once-per-version auto-restart
mark and re-prompt at the next update check.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class UpdateRestartPrompt(QDialog):
    def __init__(self, latest_version, on_now, on_delay, on_dismiss,
                 countdown_s: int = 60, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — update ready")
        # stays on top so an unattended manager still surfaces the prompt;
        # non-modal (show(), not exec()) so it never blocks the app
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.on_now = on_now
        self.on_delay = on_delay
        self.on_dismiss = on_dismiss
        self._remaining = max(1, int(countdown_s))

        lay = QVBoxLayout(self)
        self.message = QLabel(
            f"Update v{latest_version} is ready.\n"
            f"Restarting in {self._remaining} s — any active copying resumes "
            f"automatically after the restart.")
        lay.addWidget(self.message)
        buttons = QHBoxLayout()
        self.now_button = QPushButton("Restart now")
        self.delay_button = QPushButton("Delay 5 minutes")
        buttons.addWidget(self.now_button)
        buttons.addWidget(self.delay_button)
        lay.addLayout(buttons)

        self.now_button.clicked.connect(self._fire_now)
        self.delay_button.clicked.connect(self._delay)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -- countdown / buttons --
    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining > 0:
            self.message.setText(
                f"Restarting in {self._remaining} s — any active copying "
                f"resumes automatically after the restart.")
            return
        self._fire_now()

    def _fire_now(self) -> None:
        self._timer.stop()
        self.accept()  # closes without routing through reject/on_dismiss
        self.on_now()

    def _delay(self) -> None:
        self._timer.stop()
        self.accept()
        self.on_delay()

    def reject(self) -> None:
        # window-X and Esc both land here on a QDialog
        self._timer.stop()
        self.on_dismiss()
        super().reject()