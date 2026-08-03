from __future__ import annotations

import sys

# Tests force the offscreen Qt platform via the `qapp` fixture in conftest.py;
# production runs a real GUI (no env override here).

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from manager.app.controller import CopyController
from manager.gui.main_window import MainWindow
from manager.gui.tray import TrayIcon
from manager.settings.store import SettingsStore
from manager.terminal.manager import TerminalManager


class _StatusBridge(QObject):
    """Marshals controller status/log callbacks (which arrive on the
    supervisor's daemon thread) onto the GUI thread via a Qt signal."""
    status = Signal(object)
    log = Signal(str)


def build_app_graph(app: QApplication):
    store = SettingsStore()
    terminal_manager = TerminalManager(store=store)
    bridge = _StatusBridge()
    controller = CopyController(
        terminal_manager=terminal_manager, store=store,
        on_status=lambda s: bridge.status.emit(s),
        on_log=lambda m: bridge.log.emit(m))
    window = MainWindow(controller)
    bridge.status.connect(window.append_status)
    bridge.log.connect(window.append_log)
    tray = TrayIcon(controller)
    tray.install(window)
    return window, tray, controller, bridge


def main(argv=None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None
                                                   else sys.argv)
    window, tray, controller, bridge = build_app_graph(app)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())