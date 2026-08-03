from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
)

from manager.app.controller import AccountSpec, StatusUpdate


class _UpdateWorker(QThread):
    """Runs updater.check_for_update off the GUI thread; emits the UpdateInfo
    on done (Qt marshals the signal back to the GUI thread)."""
    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        self.done.emit(self._fn())


class MainWindow(QMainWindow):
    """The main window. A thin Qt view over CopyController: master account
    form, slave list, Start/Stop, status panel, log view. All actions delegate
    to the controller; status/log arrive via callbacks marshaled onto the GUI
    thread. Close is intercepted to emit close_to_tray (the tray icon hides
    the window instead of quitting)."""

    close_to_tray = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — Local Manager")
        self._controller = controller
        self._slaves: list[AccountSpec] = []
        self._build_ui()
        self._populate_terminals()

        self._update_worker = None
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(3600 * 1000)
        self._update_timer.timeout.connect(self.check_for_updates_now)
        self._update_timer.start()
        QTimer.singleShot(10_000, self.check_for_updates_now)

    # ---- UI construction ----
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Master pane
        master_box = QGroupBox("Master")
        mform = QFormLayout()
        self.master_login = QLineEdit()
        self.master_login.setPlaceholderText("integer login (e.g. 5001)")
        self.master_server = QLineEdit()
        self.master_server.setPlaceholderText("server name")
        self.master_password = QLineEdit()
        self.master_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.master_password.setPlaceholderText("demo account password")
        self.master_terminal = QComboBox()
        self.master_terminal.setEditable(True)
        mform.addRow("Login", self.master_login)
        mform.addRow("Server", self.master_server)
        mform.addRow("Password", self.master_password)
        mform.addRow("Terminal", self.master_terminal)
        master_box.setLayout(mform)

        # Slave list
        slave_box = QGroupBox("Slaves")
        sl = QVBoxLayout()
        self.slave_list = QListWidget()
        self.add_slave_button = QPushButton("Add Slave…")
        self.remove_slave_button = QPushButton("Remove Slave")
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        slave_box.setLayout(sl)

        # Start/Stop
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        controls = QHBoxLayout()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)

        # Updates
        self.update_label = QLabel("")
        self.check_update_button = QPushButton("Check for updates")
        self.update_restart_button = QPushButton("Update & restart")
        self.update_restart_button.setVisible(False)
        updates_row = QHBoxLayout()
        updates_row.addWidget(self.update_label, 1)
        updates_row.addWidget(self.check_update_button)
        updates_row.addWidget(self.update_restart_button)

        # Status + log
        self.status_view = QPlainTextEdit()
        self.status_view.setReadOnly(True)
        self.status_view.setMaximumHeight(160)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        root.addWidget(master_box)
        root.addWidget(slave_box)
        root.addLayout(controls)
        root.addLayout(updates_row)
        root.addWidget(QLabel("Status"))
        root.addWidget(self.status_view)
        root.addWidget(QLabel("Log"))
        root.addWidget(self.log_view)

        # wire buttons
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.add_slave_button.clicked.connect(self._on_add_slave)
        self.remove_slave_button.clicked.connect(self._on_remove_slave)
        self.check_update_button.clicked.connect(self.check_for_updates_now)
        self.update_restart_button.clicked.connect(self._on_update_restart)

    def _populate_terminals(self):
        self.master_terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.master_terminal.addItem(inst.exe_path)
        except Exception as exc:
            self.append_log(f"discovery failed: {exc}")

    # ---- public API (controller / tray) ----
    def append_status(self, update: StatusUpdate) -> None:
        line = update.message if update.slave_id is None \
            else f"[{update.slave_id}] {update.message}"
        self.status_view.appendPlainText(line)

    def append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    # ---- updates ----
    def check_for_updates_now(self) -> None:
        from manager import updater
        self.update_label.setText("Checking for updates…")
        self._update_worker = _UpdateWorker(updater.check_for_update, self)
        self._update_worker.done.connect(self._on_update_checked)
        self._update_worker.start()

    def _on_update_checked(self, info) -> None:
        self._update_worker = None
        if info.latest is None and not info.available:
            self.update_label.setText("Couldn't check for updates")
            self.update_restart_button.setVisible(False)
            return
        if info.available:
            self.update_label.setText(f"Update available: v{info.latest}")
            self.update_restart_button.setVisible(True)
            self.update_restart_button.setEnabled(not self._controller.is_running())
        else:
            self.update_label.setText(f"Up to date (v{info.current})")
            self.update_restart_button.setVisible(False)

    def _on_update_restart(self) -> None:
        if self._controller.is_running():
            self.append_log("stop copying before updating")
            return
        from manager import updater
        updater.apply_update_and_restart(on_quit=self._do_update_quit)

    def _do_update_quit(self) -> None:
        self._controller.stop()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    # ---- handlers ----
    def _on_start(self):
        try:
            login = int(self.master_login.text().strip())
        except ValueError:
            self.append_log("master login must be an integer")
            return
        master = AccountSpec(
            id="master", login=login,
            server=self.master_server.text().strip(),
            password=self.master_password.text(),
            terminal_path=self.master_terminal.currentText().strip() or None)
        try:
            self._controller.start(master, list(self._slaves))
            self.set_running(True)
        except Exception as exc:
            self.append_log(f"start failed: {exc}")

    def _on_stop(self):
        self._controller.stop()
        self.set_running(False)

    def _on_add_slave(self):
        # SlaveEditor (Task 3) is wired here in Task 3; for now a no-op stub
        # keeps construction + Start/Stop testable in isolation.
        from manager.gui.slave_editor import SlaveEditor, add_slave
        spec = add_slave(self)
        if spec is not None:
            self._slaves.append(spec)
            self.slave_list.addItem(f"{spec.id}: login={spec.login} "
                                    f"server={spec.server}")

    def _on_remove_slave(self):
        row = self.slave_list.currentRow()
        if row < 0:
            return
        self.slave_list.takeItem(row)
        del self._slaves[row]

    # ---- close-to-tray ----
    def closeEvent(self, event):
        """Intercept the window close: hide to tray instead of quitting. The
        tray menu's Quit is the real orderly shutdown path."""
        event.ignore()
        self.hide()
        self.close_to_tray.emit()