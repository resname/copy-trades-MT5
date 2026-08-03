# manager/gui/tray.py
from __future__ import annotations

from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication, QStyle
from PySide6.QtGui import QIcon, QAction


class TrayIcon(QSystemTrayIcon):
    """System-tray icon: close-to-tray target + Show/Quit menu. Quit does the
    orderly shutdown (controller.stop() → workers mt5.shutdown on pipe EOF →
    QApplication.quit()). Close-to-tray: MainWindow.close_to_tray connects to
    on_hide, which just leaves the window hidden with the process alive."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._window = None
        self.menu = QMenu()
        self.show_action = QAction("Show", self.menu)
        self.quit_action = QAction("Quit", self.menu)
        self.menu.addAction(self.show_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)
        # generic system icon; production may supply a real one. QSystemTrayIcon
        # has no style() of its own — use the application's style.
        style = QApplication.style()
        self.setIcon(style.standardIcon(QStyle.SP_ComputerIcon) if style
                     else QIcon())
        self.setToolTip("CopyTrades MT5")
        self.show_action.triggered.connect(self.on_show)
        self.quit_action.triggered.connect(self.on_quit)
        self.activated.connect(self._on_activated)

    def install(self, main_window) -> None:
        self._window = main_window
        # close-to-tray: the window hides instead of quitting
        main_window.close_to_tray.connect(self.on_hide)
        self.setParent(main_window)
        self.show()

    def on_show(self) -> None:
        if self._window is not None:
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()

    def on_hide(self) -> None:
        # window is already hidden by its closeEvent; nothing more to do
        pass

    def on_quit(self) -> None:
        self._controller.stop()
        QApplication.quit()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.on_show()