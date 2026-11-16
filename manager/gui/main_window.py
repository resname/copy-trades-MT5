from __future__ import annotations

import dataclasses
import subprocess
import sys
import webbrowser

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QCheckBox, QComboBox, QPushButton, QListWidget, QListWidgetItem,
    QPlainTextEdit, QLabel,
    QGroupBox, QProgressBar, QMessageBox,
)

from manager.app.controller import (
    AccountSpec, CopyProfile, StatusUpdate, AlgoTradingDisabledError,
    apply_profile, coerce_profiles, migrate_legacy_profiles,
    unique_profile_name,
)
from manager.engine.transform import parse_symbol_map
from manager.gui.profile_editor import fit_combo_width
from manager.platform import autostart, autologon_repair

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

    def __init__(self, controller, store=None, parent=None, resume_copy=False):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — Local Manager")
        self._controller = controller
        self._store = store
        self._slaves: list[AccountSpec] = []
        self._profiles: list[CopyProfile] = []
        self._countdown_timer: QTimer | None = None
        self._countdown_remaining = 0
        # --resume-copy: set by update_helper's relaunch after an update
        # restart, resumes copying via the countdown regardless of the toggle
        self._resume_copy = bool(resume_copy)
        # guard state for the auto-restart toggle (persisted in autostart)
        self._auto_update_last_attempt = ""
        # countdown popup for the auto-restart (see _auto_update_maybe_restart)
        self._update_prompt = None
        self._auto_update_delay_timer: QTimer | None = None
        self._prev_auto_update_last_attempt: str | None = None
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
        self._refresh_autologon_repair(run_repair=True)
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
        fit_combo_width(self.master_terminal, 30)
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
            "— the default path collides with existing terminals.")
        self.install_disclaimer_label.setWordWrap(True)
        mform.addRow("", self.install_disclaimer_label)
        master_box.setLayout(mform)

        # Copy profiles — shared settings attached to any number of slaves
        profile_box = QGroupBox("Copy profiles")
        pl = QVBoxLayout()
        self.profile_list = QListWidget()
        self.add_profile_button = QPushButton("New…")
        self.edit_profile_button = QPushButton("Edit…")
        self.duplicate_profile_button = QPushButton("Duplicate…")
        self.delete_profile_button = QPushButton("Delete")
        self.edit_profile_button.setEnabled(False)
        self.duplicate_profile_button.setEnabled(False)
        self.delete_profile_button.setEnabled(False)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self.add_profile_button)
        profile_row.addWidget(self.edit_profile_button)
        profile_row.addWidget(self.duplicate_profile_button)
        profile_row.addWidget(self.delete_profile_button)
        pl.addWidget(self.profile_list)
        pl.addLayout(profile_row)
        profile_box.setLayout(pl)

        # Slave list
        self.slave_box = QGroupBox("Slaves")
        sl = QVBoxLayout()
        self.slave_list = QListWidget()
        self.add_slave_button = QPushButton("Add Slave…")
        self.remove_slave_button = QPushButton("Remove Slave")
        self.edit_slave_button = QPushButton("Edit Slave…")
        self.open_slave_list_button = QPushButton("Open slave list…")
        self.edit_slave_button.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        row.addWidget(self.edit_slave_button)
        row.addWidget(self.open_slave_list_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        self.slave_box.setLayout(sl)

        # Auto-start (boot + auto-copy + auto-update)
        self.autostart_box = QGroupBox("Auto-start")
        as_layout = QVBoxLayout()
        self.autostart_boot_checkbox = QCheckBox("Launch on Windows startup")
        self.autostart_copy_checkbox = QCheckBox(
            "Auto-start copying on launch (15 s countdown)")
        self.autostart_auto_update_checkbox = QCheckBox(
            "Auto restart when update is available")
        as_layout.addWidget(self.autostart_boot_checkbox)
        as_layout.addWidget(self.autostart_copy_checkbox)
        as_layout.addWidget(self.autostart_auto_update_checkbox)
        # Migration banner for installs still using the old non-elevated
        # Startup .lnk. Deliberately non-modal: it must not block the 15 s
        # auto-copy countdown on an unattended reboot.
        self.autostart_migrate_banner = QWidget()
        banner_layout = QVBoxLayout(self.autostart_migrate_banner)
        banner_layout.setContentsMargins(0, 0, 0, 0)
        self.autostart_migrate_label = QLabel(
            "Autostart uses the old non-elevated Startup shortcut. "
            "Migrate once to an elevated scheduled task (one UAC prompt); "
            "from the next login the app starts with admin rights.")
        self.autostart_migrate_label.setWordWrap(True)
        self.autostart_migrate_button = QPushButton("Migrate now (UAC)")
        banner_layout.addWidget(self.autostart_migrate_label)
        banner_layout.addWidget(self.autostart_migrate_button)
        self.autostart_migrate_banner.setVisible(False)
        as_layout.addWidget(self.autostart_migrate_banner)
        # Self-heal for the legacy ForceAutoLogon=1 / RDP-locking setup. When
        # the app is elevated the repair runs silently at startup; otherwise
        # this banner offers a one-click UAC repair.
        self.autologon_repair_banner = QWidget()
        repair_layout = QVBoxLayout(self.autologon_repair_banner)
        repair_layout.setContentsMargins(0, 0, 0, 0)
        self.autologon_repair_label = QLabel(
            "Unattended autologin needs a repair (ForceAutoLogon / RDP-safe "
            "console lock).")
        self.autologon_repair_label.setWordWrap(True)
        self.autologon_repair_button = QPushButton("Repair now (UAC)")
        repair_layout.addWidget(self.autologon_repair_label)
        repair_layout.addWidget(self.autologon_repair_button)
        self.autologon_repair_banner.setVisible(False)
        as_layout.addWidget(self.autologon_repair_banner)
        self.autologon_setup_button = QPushButton(
            "Set up unattended boot (UAC)")
        as_layout.addWidget(self.autologon_setup_button)
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
        root.addWidget(profile_box)
        root.addWidget(self.slave_box)
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
        self.open_slave_list_button.clicked.connect(self._on_open_slave_list)
        self.slave_list.itemDoubleClicked.connect(
            lambda _item: self._on_edit_slave())
        self.slave_list.itemSelectionChanged.connect(self._update_edit_enabled)
        self.add_profile_button.clicked.connect(self._on_add_profile)
        self.edit_profile_button.clicked.connect(self._on_edit_profile)
        self.duplicate_profile_button.clicked.connect(self._on_duplicate_profile)
        self.delete_profile_button.clicked.connect(self._on_delete_profile)
        self.profile_list.itemDoubleClicked.connect(
            lambda _item: self._on_edit_profile())
        self.profile_list.itemSelectionChanged.connect(
            self._update_profile_buttons)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.install_metatrader_button.clicked.connect(self._on_install_metatrader)
        self.check_update_button.clicked.connect(self.check_for_updates_now)
        self.update_restart_button.clicked.connect(self._on_update_restart)
        self.autostart_boot_checkbox.toggled.connect(
            self._on_autostart_boot_toggled)
        self.autostart_copy_checkbox.toggled.connect(
            self._on_autostart_copy_toggled)
        self.autostart_auto_update_checkbox.toggled.connect(
            self._on_auto_update_toggled)
        self.autostart_migrate_button.clicked.connect(self._migrate_autostart)
        self.autologon_repair_button.clicked.connect(self._repair_autologon)
        self.autologon_setup_button.clicked.connect(self._setup_autologon)
        self.autostart_cancel_button.clicked.connect(self._cancel_autostart_copy)

    def _populate_terminals(self):
        self.master_terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.master_terminal.addItem(inst.exe_path)
                self.master_terminal.setItemData(
                    self.master_terminal.count() - 1, inst.exe_path,
                    Qt.ItemDataRole.ToolTipRole)
        except Exception as exc:
            self.append_log(f"discovery failed: {exc}")

    # ---- config persistence ----
    def _slave_config_dict(self, spec: AccountSpec) -> dict:
        """Materialize an attached slave from its profile immediately before
        writing: the file is internally consistent even if an in-memory spec
        drifted from its profile."""
        profile = (self._profile_by_id(spec.profile_id)
                   if spec.profile_id else None)
        if profile is not None:
            spec = apply_profile(spec, profile)
        return dataclasses.asdict(spec)

    def _config_dict(self) -> dict:
        return {
            "master": {"terminal_path": self.master_terminal.currentText().strip()},
            "slaves": [self._slave_config_dict(s) for s in self._slaves],
            "profiles": [dataclasses.asdict(p) for p in self._profiles],
            "autostart": {
                "on_boot": self.autostart_boot_checkbox.isChecked(),
                "auto_copy": self.autostart_copy_checkbox.isChecked(),
                "auto_update": self.autostart_auto_update_checkbox.isChecked(),
                # restart-loop guard: the newest version this install already
                # auto-tried (set when the auto-restart actually fires)
                "auto_update_last_attempt": self._auto_update_last_attempt,
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
        if not isinstance(cfg, dict):
            cfg = {}
        had_profiles_key = "profiles" in cfg
        if not cfg:
            # Missing or corrupt file: the store returns {}. Do not run the
            # legacy migration (it would overwrite a corrupt file or create
            # one on a fresh install) — leave the file untouched.
            self.append_log(
                "no restorable config in settings (missing or unreadable)")
        try:
            self._profiles = coerce_profiles(cfg.get("profiles"),
                                             log=self.append_log)
        except Exception as exc:
            # Defense in depth: no stored-profile value may crash __init__.
            self.append_log(f"profiles load failed: {exc}")
            self._profiles = []
        master = cfg.get("master")
        if isinstance(master, dict):
            mpath = str(master.get("terminal_path", "")).strip()
            if mpath:
                self.master_terminal.setEditText(mpath)
        for s in cfg.get("slaves") or []:
            if not isinstance(s, dict):
                continue
            if str(s.get("platform") or "").lower() == "ib":
                self.append_log(
                    f"slave {s.get('id', '?')} dropped: platform 'ib' no "
                    "longer supported (saved by an older manager version)")
                continue
            fields = AccountSpec.__dataclass_fields__
            kwargs = {k: s[k] for k in fields if k in s}
            try:
                spec = AccountSpec(**kwargs)
            except TypeError:
                continue
            if spec.profile_id:
                profile = self._profile_by_id(spec.profile_id)
                if profile is not None:
                    spec = apply_profile(spec, profile)
                else:
                    self.append_log(
                        f"profile {spec.profile_id} for slave {spec.id} "
                        "not found — using stored settings")
                    spec = dataclasses.replace(spec, profile_id="")
            self._slaves.append(spec)
        # autostart toggles: auto_copy from stored value, on_boot synced to the
        # .lnk's existence (reality is the source of truth). blockSignals so
        # syncing does not re-trigger the toggle handler (which would touch the
        # OS or save).
        as_cfg = cfg.get("autostart") or {}
        if not isinstance(as_cfg, dict):
            as_cfg = {}
        self.autostart_copy_checkbox.blockSignals(True)
        self.autostart_copy_checkbox.setChecked(bool(as_cfg.get("auto_copy", False)))
        self.autostart_copy_checkbox.blockSignals(False)
        self.autostart_auto_update_checkbox.blockSignals(True)
        self.autostart_auto_update_checkbox.setChecked(
            bool(as_cfg.get("auto_update", False)))
        self.autostart_auto_update_checkbox.blockSignals(False)
        self._auto_update_last_attempt = \
            str(as_cfg.get("auto_update_last_attempt", "") or "")
        self.autostart_boot_checkbox.blockSignals(True)
        self.autostart_boot_checkbox.setChecked(autostart.is_autostart_enabled())
        self.autostart_boot_checkbox.blockSignals(False)
        # Legacy installs autostart via the non-elevated Startup .lnk. Offer
        # the one-click migration to the elevated scheduled task (one UAC).
        legacy_only = (autostart.legacy_shortcut_exists()
                       and not autostart.task_exists())
        self.autostart_migrate_banner.setVisible(legacy_only)
        if legacy_only:
            self.append_log(
                "autostart uses the legacy Startup shortcut (not elevated) — "
                "click 'Migrate now (UAC)' or toggle 'Launch on Windows "
                "startup' off/on to switch to the elevated scheduled task")
        # One-time migration of pre-profile configs: group identical slaves
        # into copy profiles and persist the result. Runs after the autostart
        # toggles are restored so the immediate save cannot clobber them.
        if not had_profiles_key and cfg:
            original_profiles = list(self._profiles)
            original_slaves = list(self._slaves)
            try:
                migrated, mapping = migrate_legacy_profiles(self._slaves)
                # The grouper copies unvalidated slave fields, so validate the
                # minted profiles before installing them: a corrupt legacy
                # field must not reach the profile tooltip or a slave. A
                # dropped profile leaves its slaves on their own settings.
                minted = coerce_profiles(
                    [dataclasses.asdict(p) for p in migrated],
                    log=self.append_log)
                surviving = {p.id for p in minted}
                mapping = {sid: pid for sid, pid in mapping.items()
                           if pid in surviving}
                if minted:
                    self._profiles.extend(minted)
                    by_id = {p.id: p for p in minted}
                    for i, spec in enumerate(self._slaves):
                        pid = mapping.get(spec.id)
                        if pid is not None:
                            self._slaves[i] = apply_profile(spec, by_id[pid])
                    for profile in minted:
                        ids = [sid for sid, pid in mapping.items()
                               if pid == profile.id]
                        self.append_log(
                            f"migrated slaves {', '.join(ids)} into profile "
                            f"'{profile.name}'")
                # Refresh both lists, then save. The save also writes
                # profiles: [] when nothing qualified (or migration failed),
                # so the next start sees the key and never re-runs it.
                self._refresh_profile_list()
                self._refresh_slave_list()
                self._save_config()
            except Exception as exc:
                # Corrupt legacy configs can still trip the grouper, the
                # validation, or a list refresh; never let that crash
                # MainWindow.__init__. Restore the pre-migration state so the
                # slaves stay on their own settings, then still save (writing
                # profiles: []) so the next launch does not retry.
                self.append_log(f"legacy profile migration failed: {exc}")
                self._profiles = original_profiles
                self._slaves = original_slaves
                try:
                    self._refresh_profile_list()
                    self._refresh_slave_list()
                except Exception:
                    pass
                self._save_config()
        else:
            self._refresh_profile_list()
            self._refresh_slave_list()

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
        """Enable Update & restart whenever a verified wheel is cached —
        copying is allowed too: the quit path orders the stop, and the relaunched
        manager resumes copying (resume flag carried by the relaunch chain)."""
        self.update_restart_button.setEnabled(self._cached_wheel is not None)

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
        self._auto_update_maybe_restart()

    def _on_update_restart(self) -> None:
        from manager import updater
        updater.apply_update_and_restart(
            on_quit=self._do_update_quit, cached_wheel=self._cached_wheel,
            resume=self._controller.is_running())

    _AUTO_UPDATE_PROMPT_COUNTDOWN_S = 60
    _AUTO_UPDATE_PROMPT_DELAY_MS = 5 * 60 * 1000

    def _auto_update_maybe_restart(self) -> None:
        """The 'Auto restart when update is available' toggle. Instead of
        restarting silently, a countdown popup asks for confirmation: it
        restarts itself at zero, or on "Restart now"; "Delay 5 minutes" and
        closing the popup defer/decline.

        The once-per-version guard mark is recorded (and saved) when the popup
        first SHOWS — before any restart can happen — so a pip failure that
        relaunches the old version cannot loop auto-restarts for the same
        release. Closing the popup reverts the mark to its prior value, so the
        next update check prompts again rather than silently never applying."""
        if not self.autostart_auto_update_checkbox.isChecked():
            return
        latest = self._latest_version
        if not latest:
            return
        if self._update_prompt is not None:
            return  # already prompting (delayed re-prompt overlap guard)
        from manager.updater import parse_version
        try:
            if parse_version(self._auto_update_last_attempt) >= parse_version(latest):
                return  # already tried this version (or a newer one)
        except Exception:
            return  # unparsable versions: never auto-restart
        self._prev_auto_update_last_attempt = self._auto_update_last_attempt
        self._auto_update_last_attempt = latest
        self._save_config()
        self._show_update_prompt()

    def _show_update_prompt(self) -> None:
        from manager.gui.update_prompt import UpdateRestartPrompt
        latest = self._latest_version or ""
        self._update_prompt = UpdateRestartPrompt(
            latest,
            on_now=self._auto_update_fire_now,
            on_delay=self._auto_update_delay,
            on_dismiss=self._auto_update_prompt_dismissed,
            countdown_s=self._AUTO_UPDATE_PROMPT_COUNTDOWN_S)
        self._update_prompt.show()
        self.append_log(f"update v{latest} ready — restarting in "
                        f"{self._AUTO_UPDATE_PROMPT_COUNTDOWN_S} s (or delay)")

    def _auto_update_fire_now(self) -> None:
        # countdown reached zero or the user clicked Restart now
        self._update_prompt = None
        self.append_log(f"auto-restarting for update v{self._latest_version}")
        self._on_update_restart()

    def _auto_update_delay(self) -> None:
        self._update_prompt = None  # dialog hid itself (accepted)
        self._auto_update_delay_timer = QTimer(self)
        self._auto_update_delay_timer.setSingleShot(True)
        self._auto_update_delay_timer.timeout.connect(self._show_update_prompt)
        self._auto_update_delay_timer.start(self._AUTO_UPDATE_PROMPT_DELAY_MS)
        self.append_log("update restart delayed by 5 minutes")

    def _auto_update_prompt_dismissed(self) -> None:
        # popup closed (X / Esc): revert the once-per-version mark so the
        # hourly check prompts again, instead of silently never auto-applying
        self._update_prompt = None
        self._auto_update_last_attempt = (
            self._prev_auto_update_last_attempt or "")
        self._save_config()
        self.append_log("update restart dismissed — will re-prompt on the "
                        "next update check")

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
            if checked:
                self.append_log(f"autostart enable failed: {exc}")
                self.autostart_boot_checkbox.blockSignals(True)
                self.autostart_boot_checkbox.setChecked(False)
                self.autostart_boot_checkbox.blockSignals(False)
            else:
                self.append_log(f"autostart disable failed: {exc}")
                # the task/shortcut is still there — keep the UI truthful
                self.autostart_boot_checkbox.blockSignals(True)
                self.autostart_boot_checkbox.setChecked(True)
                self.autostart_boot_checkbox.blockSignals(False)
        self._save_config()

    def _migrate_autostart(self) -> None:
        """Migrate the old non-elevated Startup .lnk to the elevated scheduled
        task. One UAC prompt; applies from the next login (the current session
        keeps its token)."""
        try:
            autostart.enable_autostart(sys.executable, "-m manager")
        except Exception as exc:
            self.append_log(f"autostart migration failed: {exc}")
            return
        self.autostart_boot_checkbox.blockSignals(True)
        self.autostart_boot_checkbox.setChecked(True)
        self.autostart_boot_checkbox.blockSignals(False)
        self.autostart_migrate_banner.setVisible(False)
        self.append_log(
            "autostart migrated to the elevated scheduled task — applies from "
            "the next login")
        self._save_config()

    def _refresh_autologon_repair(self, run_repair: bool) -> None:
        """Run the startup self-heal (or refresh the banner/button state) and
        keep the setup button visibility in sync with the registry."""
        if run_repair:
            result = autologon_repair.repair_on_startup()
            if result.changed:
                self.append_log(
                    "autologon self-heal: " + ", ".join(result.changed))
            if result.error:
                self.append_log(f"autologon self-heal failed: {result.error}")
            self.autologon_repair_banner.setVisible(result.needs_elevation)
        self.autologon_setup_button.setVisible(
            not autologon_repair.setup_configured())

    def _repair_autologon(self) -> None:
        """One-click repair for the legacy ForceAutoLogon/RDP lock setup."""
        result = autologon_repair.repair_now()
        if result.error:
            self.append_log(f"autologon repair failed: {result.error}")
            return
        if result.changed:
            self.append_log(
                "autologon repaired: " + ", ".join(result.changed))
        self.autologon_repair_banner.setVisible(False)
        self.autologon_setup_button.setVisible(
            not autologon_repair.setup_configured())

    def _setup_autologon(self) -> None:
        """Launch the elevated unattended-boot setup. The Windows password is
        entered only in the setup's own secure console window."""
        self.autologon_setup_button.setEnabled(False)
        try:
            path = autologon_repair.download_setup_script()
            autologon_repair.launch_setup_script(path)
            self.append_log("unattended boot setup finished")
        except Exception as exc:
            self.append_log(f"unattended boot setup failed: {exc}")
        finally:
            self.autologon_setup_button.setEnabled(True)
            self._refresh_autologon_repair(run_repair=True)

    def _on_autostart_copy_toggled(self, _checked: bool) -> None:
        # No OS side effect; the countdown only fires on launch, not mid-session.
        self._save_config()

    def _on_auto_update_toggled(self, _checked: bool) -> None:
        # No immediate effect: the next ready update fires the restart
        # (see _auto_update_maybe_restart). Persist the choice now.
        self._save_config()

    def _maybe_begin_autostart_copy(self) -> None:
        """On launch, if the auto-copy toggle is on (or --resume-copy was
        passed by the update relaunch) and a master + slaves are configured,
        start a 15 s countdown to auto-Start. Cancel via the dedicated Cancel
        button. No-op otherwise (toggle off, no resume flag, or config
        incomplete)."""
        if not self.autostart_copy_checkbox.isChecked() \
                and not self._resume_copy:
            return
        terminal_path = self.master_terminal.currentText().strip()
        if not terminal_path or not self._slaves:
            self.append_log("auto-start skipped: no master/slaves configured")
            return
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self._countdown_remaining = 15
        self.autostart_cancel_button.setText("Cancel (15 s)")
        self.autostart_cancel_button.setVisible(True)
        self.append_status(StatusUpdate(kind="info",
            message="Auto-start in 15 s — click Cancel to abort"))
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._autostart_tick)
        self._countdown_timer.start()

    def _autostart_tick(self) -> None:
        self._countdown_remaining -= 1
        if self._countdown_remaining > 0:
            self.autostart_cancel_button.setText(
                f"Cancel ({self._countdown_remaining} s)")
            return
        # countdown reached zero -> fire Start (silent: no modal on failure)
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
        self.autostart_cancel_button.setVisible(False)
        self._start_silent()
        self.set_running(self._controller.is_running())

    def _cancel_autostart_copy(self) -> None:
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
        self.autostart_cancel_button.setVisible(False)
        self.set_running(False)
        self.append_log("auto-start cancelled")

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
        from manager.gui.slave_editor import add_slave
        spec = add_slave(self, profiles=list(self._profiles),
                         create_profile=self._create_profile_from_editor)
        if spec is not None:
            self._slaves.append(spec)
            self._refresh_slave_list()
            self.slave_list.setCurrentRow(len(self._slaves) - 1)
            self._save_config()
            return spec
        return None

    def _remove_slave_at(self, row: int) -> bool:
        if not (0 <= row < len(self._slaves)):
            return False
        del self._slaves[row]
        self._refresh_slave_list()
        self._save_config()
        return True

    def _remove_slave_by_id(self, slave_id: str) -> bool:
        for row, spec in enumerate(self._slaves):
            if spec.id == slave_id:
                return self._remove_slave_at(row)
        return False

    def _on_remove_slave(self):
        self._remove_slave_at(self.slave_list.currentRow())

    def _update_edit_enabled(self) -> None:
        self.edit_slave_button.setEnabled(self.slave_list.currentRow() >= 0)

    def _edit_slave_at(self, row: int) -> bool:
        if not (0 <= row < len(self._slaves)):
            return False
        from manager.gui.slave_editor import edit_slave
        try:
            new = edit_slave(self, self._slaves[row],
                             profiles=list(self._profiles),
                             create_profile=self._create_profile_from_editor)
        except Exception as exc:
            self.append_log(f"edit failed: {exc}")
            return False
        if new is None:
            return False
        self._slaves[row] = new
        self._refresh_slave_list()
        self._save_config()
        if self._controller.is_running():
            self._controller.apply_slave_edit(new.id, new)
        return True

    def _edit_slave_by_id(self, slave_id: str) -> bool:
        for row, spec in enumerate(self._slaves):
            if spec.id == slave_id:
                return self._edit_slave_at(row)
        return False

    def _on_edit_slave(self, *_args) -> None:
        self._edit_slave_at(self.slave_list.currentRow())

    def _on_open_slave_list(self) -> None:
        from manager.gui.slave_list_dialog import open_slave_list
        open_slave_list(self)

    # ---- copy profiles ----
    def _profile_by_id(self, profile_id: str) -> CopyProfile | None:
        for profile in self._profiles:
            if profile.id == profile_id:
                return profile
        return None

    def _profile_names(self, exclude_id: str = "") -> list[str]:
        return [p.name for p in self._profiles if p.id != exclude_id]

    def _profile_label(self, profile: CopyProfile) -> str:
        return profile.name

    def _profile_tooltip(self, profile: CopyProfile) -> str:
        symbols = len(parse_symbol_map(profile.symbol_map_csv or ""))
        mode = (profile.sizing_mode or "").replace("_", " ")
        # Include the lot parameter that the selected mode actually uses.
        if profile.sizing_mode == "fixed_lot":
            mode_detail = f" · fixed {profile.fixed_lot}"
        elif profile.sizing_mode == "balance_step":
            mode_detail = f" · base {profile.master_base_lot}"
        else:
            mode_detail = ""
        normalize = "on" if profile.normalize_sltp else "off"
        return (
            f"{mode}{mode_detail} · step {profile.step_amount}/{profile.step_size} · "
            f"max {profile.max_lot} · age {profile.max_trade_age_minutes} min · "
            f"{symbols} symbols · normalize {normalize}")

    def _slave_label(self, spec: AccountSpec) -> str:
        basename = (spec.terminal_path or spec.id).replace("\\", "/") \
            .rstrip("/").rsplit("/", 1)[-1]
        profile = self._profile_by_id(spec.profile_id) if spec.profile_id else None
        suffix = profile.name if profile is not None else "own settings"
        return f"{spec.id}: {basename} ({suffix})"

    def _refresh_slave_list(self) -> None:
        row = self.slave_list.currentRow()
        self.slave_list.blockSignals(True)
        try:
            self.slave_list.clear()
            for spec in self._slaves:
                label = self._slave_label(spec)
                item = QListWidgetItem(label)
                item.setToolTip(label)
                self.slave_list.addItem(item)
            if 0 <= row < self.slave_list.count():
                self.slave_list.setCurrentRow(row)
        finally:
            self.slave_list.blockSignals(False)
        self._update_edit_enabled()

    def _refresh_profile_list(self, select_id: str | None = None) -> None:
        item = self.profile_list.currentItem()
        current_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        target_id = select_id if select_id is not None else current_id
        self.profile_list.blockSignals(True)
        try:
            self.profile_list.clear()
            for profile in self._profiles:
                item = QListWidgetItem(self._profile_label(profile))
                item.setData(Qt.ItemDataRole.UserRole, profile.id)
                item.setToolTip(
                    f"{profile.name}\n{self._profile_tooltip(profile)}")
                self.profile_list.addItem(item)
                if profile.id == target_id:
                    self.profile_list.setCurrentItem(item)
        finally:
            self.profile_list.blockSignals(False)
        self._update_profile_buttons()

    def _update_profile_buttons(self, *_args) -> None:
        selected = self.profile_list.currentItem() is not None
        self.edit_profile_button.setEnabled(selected)
        self.duplicate_profile_button.setEnabled(selected)
        self.delete_profile_button.setEnabled(selected)

    def _current_profile(self) -> CopyProfile | None:
        item = self.profile_list.currentItem()
        if item is None:
            return None
        return self._profile_by_id(item.data(Qt.ItemDataRole.UserRole))

    def _on_add_profile(self) -> None:
        from manager.gui.profile_editor import new_profile
        profile = new_profile(
            existing_names=self._profile_names(),
            existing_ids={p.id for p in self._profiles},
            parent=self)
        if profile is None:
            return
        self._profiles.append(profile)
        self._refresh_profile_list(profile.id)
        self._save_config()

    def _on_edit_profile(self, *_args) -> None:
        cur = self._current_profile()
        if cur is None:
            return
        from manager.gui.profile_editor import edit_profile
        try:
            updated = edit_profile(
                cur, existing_names=self._profile_names(exclude_id=cur.id),
                parent=self)
        except Exception as exc:
            self.append_log(f"profile edit failed: {exc}")
            return
        if updated is None:
            return
        self._apply_profile_change(updated)

    def _apply_profile_change(self, profile: CopyProfile) -> None:
        for i, existing in enumerate(self._profiles):
            if existing.id == profile.id:
                self._profiles[i] = profile
                break
        else:
            return
        for i, spec in enumerate(self._slaves):
            if spec.profile_id != profile.id:
                continue
            updated = apply_profile(spec, profile)
            self._slaves[i] = updated
            if self._controller.is_running():
                try:
                    self._controller.apply_slave_edit(updated.id, updated)
                except Exception as exc:
                    self.append_log(
                        f"live update failed for {updated.id}: {exc}")
        self._refresh_profile_list(profile.id)
        self._refresh_slave_list()
        self._save_config()

    def _on_duplicate_profile(self) -> None:
        src = self._current_profile()
        if src is None:
            return
        from manager.gui.profile_editor import new_profile
        seed = dataclasses.replace(
            src, id="",
            name=unique_profile_name(f"{src.name} (copy)",
                                     self._profile_names()))
        profile = new_profile(
            seed=seed, existing_names=self._profile_names(),
            existing_ids={p.id for p in self._profiles},
            parent=self)
        if profile is None:
            return
        self._profiles.append(profile)
        self._refresh_profile_list(profile.id)
        self._save_config()

    def _on_delete_profile(self) -> None:
        cur = self._current_profile()
        if cur is None:
            return
        attached = [s.id for s in self._slaves if s.profile_id == cur.id]
        if attached:
            QMessageBox.warning(
                self, "Copy profile in use",
                "Detach these slaves first: " + ", ".join(attached))
            return
        self._profiles = [p for p in self._profiles if p.id != cur.id]
        self._refresh_profile_list()
        self._save_config()

    def _create_profile_from_editor(self, profile: CopyProfile) -> None:
        if self._profile_by_id(profile.id) is None:
            self._profiles.append(profile)
        self._refresh_profile_list(profile.id)
        self._save_config()

    # ---- close = quit ----
    def closeEvent(self, event):
        """Closing the window is the orderly quit path: stop the engine (join
        workers), accept the close, then quit the app — mirrors the update-quit
        path in _do_update_quit."""
        self._controller.stop()
        event.accept()
        QApplication.quit()