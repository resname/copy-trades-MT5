from __future__ import annotations

import dataclasses
import subprocess
import sys
import webbrowser

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QCheckBox, QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel,
    QGroupBox, QProgressBar, QMessageBox,
)

from manager.app.controller import AccountSpec, StatusUpdate, AlgoTradingDisabledError
from manager.platform import autostart

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


class _DownloadWorker(QThread):
    """Runs updater.download_update(progress=cb) off the GUI thread; emits
    progress(bytes_done, bytes_total) during the download and done(wheel) on
    completion (wheel is a Path on success, None on failure)."""
    done = Signal(object)
    progress = Signal(int, int)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        self.done.emit(self._fn(self._emit))

    def _emit(self, done, total):
        self.progress.emit(done, total)


class MainWindow(QMainWindow):
    """The main window. A thin Qt view over CopyController: master account
    form, slave list, Start/Stop, status panel, log view. All actions delegate
    to the controller; status/log arrive via callbacks marshaled onto the GUI
    thread. Closing the window is the orderly quit path (controller.stop() then
    QApplication.quit()); there is no tray."""

    def __init__(self, controller, store=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — Local Manager")
        self._controller = controller
        self._store = store
        self._slaves: list[AccountSpec] = []
        self._countdown_timer: QTimer | None = None
        self._countdown_remaining = 0
        self._build_ui()
        self._populate_terminals()
        self._load_config()
        app = QApplication.instance()
        if app is not None and self._store is not None:
            app.aboutToQuit.connect(self._save_config)

        self._update_worker = None
        self._predownload_worker = None
        self._cached_wheel = None
        self._latest_version = None
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(3600 * 1000)
        self._update_timer.timeout.connect(self.check_for_updates_now)
        self._update_timer.start()
        QTimer.singleShot(10_000, self.check_for_updates_now)
        self._maybe_begin_autostart_copy()

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
        self.edit_slave_button = QPushButton("Edit Slave…")
        self.edit_slave_button.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        row.addWidget(self.edit_slave_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        slave_box.setLayout(sl)

        # Auto-start (boot + auto-copy)
        self.autostart_box = QGroupBox("Auto-start")
        as_layout = QVBoxLayout()
        self.autostart_boot_checkbox = QCheckBox("Launch on Windows startup")
        self.autostart_copy_checkbox = QCheckBox(
            "Auto-start copying on launch (15 s countdown)")
        as_layout.addWidget(self.autostart_boot_checkbox)
        as_layout.addWidget(self.autostart_copy_checkbox)
        self.autostart_box.setLayout(as_layout)

        # Start/Stop + countdown Cancel
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.autostart_cancel_button = QPushButton("Cancel")
        self.autostart_cancel_button.setVisible(False)
        controls = QHBoxLayout()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.autostart_cancel_button)

        # Updates
        self.update_label = QLabel("")
        self.check_update_button = QPushButton("Check for updates")
        self.update_restart_button = QPushButton("Update & restart")
        self.update_restart_button.setVisible(False)
        self.update_progress = QProgressBar()
        self.update_progress.setRange(0, 100)
        self.update_progress.setVisible(False)
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
        root.addWidget(self.autostart_box)
        root.addLayout(controls)
        root.addLayout(updates_row)
        root.addWidget(self.update_progress)
        root.addWidget(QLabel("Status"))
        root.addWidget(self.status_view)
        root.addWidget(QLabel("Log"))
        root.addWidget(self.log_view)

        # wire buttons
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.add_slave_button.clicked.connect(self._on_add_slave)
        self.remove_slave_button.clicked.connect(self._on_remove_slave)
        self.edit_slave_button.clicked.connect(self._on_edit_slave)
        self.slave_list.itemDoubleClicked.connect(
            lambda _item: self._on_edit_slave())
        self.slave_list.itemSelectionChanged.connect(self._update_edit_enabled)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.install_metatrader_button.clicked.connect(self._on_install_metatrader)
        self.check_update_button.clicked.connect(self.check_for_updates_now)
        self.update_restart_button.clicked.connect(self._on_update_restart)
        self.autostart_boot_checkbox.toggled.connect(
            self._on_autostart_boot_toggled)
        self.autostart_copy_checkbox.toggled.connect(
            self._on_autostart_copy_toggled)
        self.autostart_cancel_button.clicked.connect(self._cancel_autostart_copy)

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
            "autostart": {
                "on_boot": self.autostart_boot_checkbox.isChecked(),
                "auto_copy": self.autostart_copy_checkbox.isChecked(),
            },
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
        # autostart toggles: auto_copy from stored value, on_boot synced to the
        # .lnk's existence (reality is the source of truth). blockSignals so
        # syncing does not re-trigger the toggle handler (which would touch the
        # OS or save).
        as_cfg = (cfg.get("autostart") if isinstance(cfg, dict) else None) or {}
        self.autostart_copy_checkbox.blockSignals(True)
        self.autostart_copy_checkbox.setChecked(bool(as_cfg.get("auto_copy", False)))
        self.autostart_copy_checkbox.blockSignals(False)
        self.autostart_boot_checkbox.blockSignals(True)
        self.autostart_boot_checkbox.setChecked(autostart.is_autostart_enabled())
        self.autostart_boot_checkbox.blockSignals(False)

    # ---- public API (controller) ----
    def append_status(self, update: StatusUpdate) -> None:
        line = update.message if update.slave_id is None \
            else f"[{update.slave_id}] {update.message}"
        self.status_view.appendPlainText(line)

    def append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self._apply_update_button_state()

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
            self.update_progress.setVisible(False)
            return
        if info.available:
            self._latest_version = info.latest
            self.update_label.setText(f"Update available: v{info.latest} — downloading…")
            self.update_restart_button.setVisible(True)
            self.update_restart_button.setEnabled(False)  # greyed until ready
            self.update_progress.setRange(0, 100)
            self.update_progress.setValue(0)
            self.update_progress.setVisible(True)
            self._predownload_worker = _DownloadWorker(self._do_predownload, self)
            self._predownload_worker.progress.connect(self._on_download_progress)
            self._predownload_worker.done.connect(self._on_predownload_done)
            self._predownload_worker.start()
        else:
            self.update_label.setText(f"Up to date (v{info.current})")
            self.update_restart_button.setVisible(False)
            self.update_progress.setVisible(False)

    def _do_predownload(self, progress_cb=None):
        from manager import updater
        try:
            return updater.download_update(progress=progress_cb)
        except Exception:
            return None

    def _on_download_progress(self, done: int, total: int) -> None:
        if total < 0:
            self.update_progress.setRange(0, 0)  # indeterminate
        else:
            self.update_progress.setRange(0, 100)
            pct = 0 if total == 0 else min(100, done * 100 // total)
            self.update_progress.setValue(pct)

    def _apply_update_button_state(self) -> None:
        """Enable Update & restart only when a verified wheel is cached AND
        the manager is idle. Called on download-done, on update-checked, and
        from set_running (so stopping a copy job re-enables a ready update)."""
        self.update_restart_button.setEnabled(
            self._cached_wheel is not None
            and not self._controller.is_running())

    def _on_predownload_done(self, wheel) -> None:
        self._predownload_worker = None
        self.update_progress.setVisible(False)
        if wheel is None or self._latest_version is None:
            self._cached_wheel = None
            self.update_label.setText("Update download failed — click Check to retry")
            self.update_restart_button.setEnabled(False)
            return
        self._cached_wheel = wheel
        self.update_label.setText(
            f"Update ready: v{self._latest_version} — restart in seconds")
        self._apply_update_button_state()

    def _on_update_restart(self) -> None:
        if self._controller.is_running():
            self.append_log("stop copying before updating")
            return
        from manager import updater
        updater.apply_update_and_restart(
            on_quit=self._do_update_quit, cached_wheel=self._cached_wheel)

    def _do_update_quit(self) -> None:
        self._controller.stop()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    # ---- handlers ----
    def _on_start(self):
        self._do_start(show_modal=True)

    def _do_start(self, show_modal: bool, label: str = "start") -> None:
        """Shared Start body. show_modal=True (manual click) shows the Algo
        Trading modal on failure; show_modal=False (auto-start) logs only.
        `label` prefixes the failure log line so auto-start attempts are
        distinguishable in the log ("auto-start failed: …" vs "start failed: …")."""
        terminal_path = self.master_terminal.currentText().strip()
        if not terminal_path:
            self.append_log("select a master terminal first")
            return
        master = AccountSpec(id="master", terminal_path=terminal_path)
        try:
            self._controller.start(master, list(self._slaves))
            self.set_running(True)
            self._save_config()
        except AlgoTradingDisabledError as exc:
            self.append_log(f"{label} failed: {exc}")
            if show_modal:
                QMessageBox.warning(self, "Algo Trading disabled", str(exc))
        except Exception as exc:
            self.append_log(f"{label} failed: {exc}")

    def _start_silent(self) -> None:
        """Auto-start path: same Start body, never shows a modal; logs as
        'auto-start failed' so the attempt is distinguishable in the log."""
        self._do_start(show_modal=False, label="auto-start")

    def _on_autostart_boot_toggled(self, checked: bool) -> None:
        try:
            if checked:
                autostart.enable_autostart(sys.executable, "-m manager")
            else:
                autostart.disable_autostart()
        except Exception as exc:
            self.append_log(f"autostart enable failed: {exc}")
            self.autostart_boot_checkbox.blockSignals(True)
            self.autostart_boot_checkbox.setChecked(False)
            self.autostart_boot_checkbox.blockSignals(False)
        self._save_config()

    def _on_autostart_copy_toggled(self, _checked: bool) -> None:
        # No OS side effect; the countdown only fires on launch, not mid-session.
        self._save_config()

    def _maybe_begin_autostart_copy(self) -> None:
        # Countdown implemented in Task 3; no-op here so launch is safe.
        return

    def _cancel_autostart_copy(self) -> None:
        # Wired in Task 3.
        return

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

    def _update_edit_enabled(self) -> None:
        self.edit_slave_button.setEnabled(self.slave_list.currentRow() >= 0)

    def _on_edit_slave(self, *_args) -> None:
        row = self.slave_list.currentRow()
        if row < 0:
            return
        from manager.gui.slave_editor import edit_slave
        try:
            new = edit_slave(self, self._slaves[row])
        except Exception as exc:
            self.append_log(f"edit failed: {exc}")
            return
        if new is None:
            return
        self._slaves[row] = new
        label = (new.terminal_path or new.id).replace("\\", "/").rstrip("/") \
            .rsplit("/", 1)[-1]
        item = self.slave_list.item(row)
        if item is not None:
            item.setText(f"{new.id}: {label}")
        self._save_config()
        if self._controller.is_running():
            self._controller.apply_slave_edit(new.id, new)

    # ---- close = quit ----
    def closeEvent(self, event):
        """Closing the window is the orderly quit path: stop the engine (join
        workers), accept the close, then quit the app — mirrors the update-quit
        path in _do_update_quit."""
        self._controller.stop()
        event.accept()
        QApplication.quit()