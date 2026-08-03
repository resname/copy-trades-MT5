from __future__ import annotations

import dataclasses
import subprocess
import webbrowser

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
)

from manager.app.controller import AccountSpec, StatusUpdate

MT5_DOWNLOAD_URL = "https://www.metatrader5.com/en/download"


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

    def __init__(self, controller, store=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — Local Manager")
        self._controller = controller
        self._store = store
        self._slaves: list[AccountSpec] = []
        self._build_ui()
        self._populate_terminals()
        self._load_config()
        app = QApplication.instance()
        if app is not None and self._store is not None:
            app.aboutToQuit.connect(self._save_config)

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

        # Master pane — terminal-path only (manual login)
        master_box = QGroupBox("Master")
        mform = QFormLayout()
        self.master_terminal = QComboBox()
        self.master_terminal.setEditable(True)
        mform.addRow("Terminal", self.master_terminal)
        term_row = QHBoxLayout()
        self.install_metatrader_button = QPushButton("Install MetaTrader")
        self.launch_terminal_button = QPushButton("Open terminal for login")
        term_row.addWidget(self.install_metatrader_button)
        term_row.addWidget(self.launch_terminal_button)
        self.term_row = term_row
        mform.addRow("", term_row)
        self.install_disclaimer_label = QLabel(
            "Install MetaTrader opens the download page. Download and run "
            "mt5setup.exe, and choose a CUSTOM install path for each terminal "
            "— the default path collides with existing terminals. Log in to a "
            "DEMO account only.")
        self.install_disclaimer_label.setWordWrap(True)
        mform.addRow("", self.install_disclaimer_label)
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
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.install_metatrader_button.clicked.connect(self._on_install_metatrader)
        self.check_update_button.clicked.connect(self.check_for_updates_now)
        self.update_restart_button.clicked.connect(self._on_update_restart)

    def _populate_terminals(self):
        self.master_terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.master_terminal.addItem(inst.exe_path)
        except Exception as exc:
            self.append_log(f"discovery failed: {exc}")

    # ---- config persistence ----
    def _config_dict(self) -> dict:
        return {
            "master": {"terminal_path": self.master_terminal.currentText().strip()},
            "slaves": [dataclasses.asdict(s) for s in self._slaves],
        }

    def _save_config(self) -> None:
        if self._store is None:
            return
        try:
            self._store.save_config(self._config_dict())
        except Exception as exc:
            self.append_log(f"config save failed: {exc}")

    def _load_config(self) -> None:
        if self._store is None:
            return
        try:
            cfg = self._store.load_config()
        except Exception as exc:
            self.append_log(f"config load failed: {exc}")
            return
        master = cfg.get("master") if isinstance(cfg, dict) else None
        if isinstance(master, dict):
            mpath = str(master.get("terminal_path", "")).strip()
            if mpath:
                self.master_terminal.setEditText(mpath)
        for s in (cfg.get("slaves") if isinstance(cfg, dict) else None) or []:
            if not isinstance(s, dict):
                continue
            fields = AccountSpec.__dataclass_fields__
            kwargs = {k: s[k] for k in fields if k in s}
            try:
                spec = AccountSpec(**kwargs)
            except TypeError:
                continue
            self._slaves.append(spec)
            label = (spec.terminal_path or spec.id).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            self.slave_list.addItem(f"{spec.id}: {label}")

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
        terminal_path = self.master_terminal.currentText().strip()
        if not terminal_path:
            self.append_log("select a master terminal first")
            return
        master = AccountSpec(id="master", terminal_path=terminal_path)
        try:
            self._controller.start(master, list(self._slaves))
            self.set_running(True)
            self._save_config()
        except Exception as exc:
            self.append_log(f"start failed: {exc}")

    def _on_stop(self):
        self._controller.stop()
        self.set_running(False)

    def _on_launch_terminal(self):
        exe = self.master_terminal.currentText().strip()
        if not exe:
            self.append_log("select a terminal first")
            return
        try:
            subprocess.Popen([exe])
        except OSError as exc:
            self.append_log(f"failed to launch terminal: {exc}")

    def _on_install_metatrader(self):
        webbrowser.open(MT5_DOWNLOAD_URL)

    def _on_add_slave(self):
        from manager.gui.slave_editor import SlaveEditor, add_slave
        spec = add_slave(self)
        if spec is not None:
            self._slaves.append(spec)
            label = spec.terminal_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            self.slave_list.addItem(f"{spec.id}: {label}")
            self._save_config()

    def _on_remove_slave(self):
        row = self.slave_list.currentRow()
        if row < 0:
            return
        self.slave_list.takeItem(row)
        del self._slaves[row]
        self._save_config()

    # ---- close-to-tray ----
    def closeEvent(self, event):
        """Intercept the window close: hide to tray instead of quitting. The
        tray menu's Quit is the real orderly shutdown path."""
        event.ignore()
        self.hide()
        self.close_to_tray.emit()