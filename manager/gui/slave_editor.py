# manager/gui/slave_editor.py
from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QHBoxLayout, QLabel,
)

from manager.app.controller import AccountSpec, CopyProfile, new_profile_id
from manager.gui.profile_editor import (
    CopySettingsWidget, ProfileEditor, fit_combo_width,
)

_NUMERIC_FIELDS = (
    "step_amount", "step_size", "max_lot", "max_trade_age_minutes",
    "master_base_lot", "fixed_lot",
)


class SlaveEditor(QDialog):
    """A modal dialog to add/edit one slave account: terminal-path dropdown
    (auto-populated, required — the user manually logs in to the terminal),
    an Open-terminal-for-login button, a copy-profile picker, the shared
    copy-trading settings (symbol map, lot sizing, normalize SL/TP), and a
    hint when the settings come from a profile. ``spec()`` returns the
    configured AccountSpec (None if cancelled)."""

    def __init__(self, controller, profiles: Iterable[CopyProfile] = (),
                 create_profile: Callable[[CopyProfile], None] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Slave")
        self._controller = controller
        # Local copy: creating a profile must not mutate the caller's list.
        self.profiles = list(profiles or [])
        self._create_profile = create_profile
        self._own_snapshot: dict[str, object] | None = None
        self._own_texts: dict[str, str] = {}
        self._started_attached = False
        self._current_pid = ""
        self._last_pid = ""
        self._build_ui()
        self._populate_terminals()
        self._reload_profiles("")

    # ---- delegating properties (kept for existing callers/tests) ----
    @property
    def symbol_table(self):
        return self.copy_settings.symbol_table

    @symbol_table.setter
    def symbol_table(self, widget) -> None:
        self.copy_settings.symbol_table = widget

    @property
    def sizing_mode(self):
        return self.copy_settings.sizing_mode

    @sizing_mode.setter
    def sizing_mode(self, widget) -> None:
        self.copy_settings.sizing_mode = widget

    @property
    def step_amount(self):
        return self.copy_settings.step_amount

    @step_amount.setter
    def step_amount(self, widget) -> None:
        self.copy_settings.step_amount = widget

    @property
    def step_size(self):
        return self.copy_settings.step_size

    @step_size.setter
    def step_size(self, widget) -> None:
        self.copy_settings.step_size = widget

    @property
    def max_lot(self):
        return self.copy_settings.max_lot

    @max_lot.setter
    def max_lot(self, widget) -> None:
        self.copy_settings.max_lot = widget

    @property
    def max_trade_age_minutes(self):
        return self.copy_settings.max_trade_age_minutes

    @max_trade_age_minutes.setter
    def max_trade_age_minutes(self, widget) -> None:
        self.copy_settings.max_trade_age_minutes = widget

    @property
    def master_base_lot(self):
        return self.copy_settings.master_base_lot

    @master_base_lot.setter
    def master_base_lot(self, widget) -> None:
        self.copy_settings.master_base_lot = widget

    @property
    def fixed_lot(self):
        return self.copy_settings.fixed_lot

    @fixed_lot.setter
    def fixed_lot(self, widget) -> None:
        self.copy_settings.fixed_lot = widget

    @property
    def follow_master_lot(self):
        return self.copy_settings.follow_master_lot

    @follow_master_lot.setter
    def follow_master_lot(self, widget) -> None:
        self.copy_settings.follow_master_lot = widget

    @property
    def normalize_sltp(self):
        return self.copy_settings.normalize_sltp

    @normalize_sltp.setter
    def normalize_sltp(self, widget) -> None:
        self.copy_settings.normalize_sltp = widget

    def _build_ui(self):
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("s1")
        self.terminal = QComboBox()
        self.terminal.setEditable(True)
        fit_combo_width(self.terminal, 30)
        form.addRow("Slave id", self.id_edit)
        form.addRow("Terminal", self.terminal)
        term_row = QHBoxLayout()
        self.launch_terminal_button = QPushButton("Open terminal for login")
        term_row.addWidget(self.launch_terminal_button)
        form.addRow("", term_row)

        self.profile_combo = QComboBox()
        fit_combo_width(self.profile_combo, 24)
        self.profile_hint = QLabel("")
        self.profile_hint.setWordWrap(True)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self.profile_combo)
        profile_row.addWidget(self.profile_hint)
        form.addRow("Copy profile", profile_row)
        root.addLayout(form)

        self.copy_settings = CopySettingsWidget()
        root.addWidget(self.copy_settings)

        buttons = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        root.addLayout(buttons)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self.profile_combo.activated.connect(self._on_profile_activated)

    def _on_launch_terminal(self):
        exe = self.terminal.currentText().strip()
        if not exe:
            return
        try:
            subprocess.Popen([exe])
        except OSError:
            pass

    def _populate_terminals(self):
        self.terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.terminal.addItem(inst.exe_path)
                self.terminal.setItemData(
                    self.terminal.count() - 1, inst.exe_path,
                    Qt.ItemDataRole.ToolTipRole)
        except Exception:
            pass

    # ---- copy profile picker ----
    def _profile_by_id(self, pid: str) -> CopyProfile | None:
        for profile in self.profiles:
            if profile.id == pid:
                return profile
        return None

    def _reload_profiles(self, select_id: str = "") -> None:
        """Refill the picker: own settings, one item per profile, a disabled
        separator and the "New profile…" action. Selecting ``select_id`` is
        done with signals blocked so no profile-change handler fires."""
        combo = self.profile_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("Own settings (no profile)", "")
            for profile in self.profiles:
                combo.addItem(profile.name, profile.id)
                combo.setItemData(combo.count() - 1, profile.name,
                                  Qt.ItemDataRole.ToolTipRole)
            combo.insertSeparator(combo.count())
            combo.addItem("New profile…", "__new__")
            combo.setItemData(combo.count() - 1, "Create a new copy profile",
                              Qt.ItemDataRole.ToolTipRole)
            idx = combo.findData(select_id)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            combo.blockSignals(False)

    def _update_profile_hint(self, profile: CopyProfile | None) -> None:
        if profile is None:
            self.profile_hint.setText("")
            self.profile_hint.setToolTip("")
            self.profile_hint.setVisible(False)
            self.profile_combo.setToolTip("Own settings (no profile)")
        else:
            # Keep a very long, unbroken name from forcing the dialog wider
            # than the screen: elide it in the label, full text in the tooltip.
            name = (profile.name if len(profile.name) <= 32
                    else profile.name[:31] + "…")
            self.profile_hint.setText(
                f"Settings come from profile '{name}' — "
                "edit it under Copy profiles.")
            self.profile_hint.setToolTip(
                f"Settings come from profile '{profile.name}' — "
                "edit it under Copy profiles.")
            self.profile_hint.setVisible(True)
            self.profile_combo.setToolTip(profile.name)

    def _on_profile_changed(self, _index: int = -1) -> None:
        data = self.profile_combo.currentData()
        if data == "__new__":
            # The creation action only fires on activation.
            return
        data = data or ""
        self._last_pid = data
        self._apply_profile_selection(data)

    def _on_profile_activated(self, _index: int) -> None:
        if self.profile_combo.currentData() != "__new__":
            return
        try:
            # Mint an id that avoids every profile already in this editor, so
            # a newly created profile can never collide with an existing one.
            seed = CopyProfile(
                id=new_profile_id({p.id for p in self.profiles}), name="",
                **self.copy_settings.values())
        except (TypeError, ValueError):
            # Keep the invalid text in place; stay on the previous selection.
            self._reload_profiles(self._last_pid)
            self._apply_profile_selection(self._last_pid)
            return
        dlg = ProfileEditor(seed,
                            existing_names=[p.name for p in self.profiles],
                            parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._reload_profiles(self._last_pid)
            self._apply_profile_selection(self._last_pid)
            return
        profile = dlg.profile()
        if profile is None:
            self._reload_profiles(self._last_pid)
            self._apply_profile_selection(self._last_pid)
            return
        self.profiles.append(profile)
        if self._create_profile is not None:
            self._create_profile(profile)
        self._reload_profiles(profile.id)
        self._apply_profile_selection(profile.id)

    def _apply_profile_selection(self, pid: str) -> None:
        """Switch the shared settings to the profile ``pid`` (or own settings
        for ``""``), snapshotting/restoring the user's own values as needed."""
        pid = pid or ""
        profile = self._profile_by_id(pid) if pid else None
        if pid == self._current_pid:
            # Just refresh the presentation; never touch the values.
            self.copy_settings.set_editable(profile is None)
            self._update_profile_hint(profile)
            return
        if pid:
            if profile is None:
                # Unknown id (e.g. the profile list was not passed in).
                self._reload_profiles(self._current_pid)
                return
            if self._current_pid == "":
                try:
                    self._own_snapshot = dict(self.copy_settings.values())
                except (TypeError, ValueError):
                    # Invalid numeric text: keep it and stay on own settings.
                    self._last_pid = ""
                    self._reload_profiles("")
                    self.copy_settings.set_editable(True)
                    self._update_profile_hint(None)
                    return
                # values() normalizes numbers to floats; remember the exact
                # text so restoring own settings shows what the user typed.
                self._own_texts = {
                    name: getattr(self.copy_settings, name).text()
                    for name in _NUMERIC_FIELDS
                }
            self.copy_settings.set_values(profile)
            self.copy_settings.set_editable(False)
        else:
            self.copy_settings.set_editable(True)
            if not self._started_attached and self._own_snapshot is not None:
                self.copy_settings.set_values(self._own_snapshot)
                for name, text in self._own_texts.items():
                    getattr(self.copy_settings, name).setText(text)
        self._current_pid = pid
        # Keep the previous selection in sync so a following "New profile…"
        # cancel restores this selection (including a just-created profile).
        self._last_pid = pid
        self._update_profile_hint(profile)

    def set_spec(self, spec: AccountSpec, *, lock_identity: bool = True) -> None:
        """Pre-populate the editor from an existing AccountSpec (edit mode).
        When lock_identity is True, the slave id and terminal path are shown
        read-only/disabled so the slave's identity cannot change mid-edit."""
        self._started_attached = bool(spec.profile_id)
        self._own_snapshot = None
        self._own_texts = {}
        self._current_pid = spec.profile_id or ""
        self._last_pid = self._current_pid
        self.setWindowTitle("Edit Slave")
        self.id_edit.setText(spec.id)
        if lock_identity:
            self.id_edit.setReadOnly(True)
        if spec.terminal_path:
            if self.terminal.findText(spec.terminal_path) < 0:
                self.terminal.addItem(spec.terminal_path)
            self.terminal.setCurrentText(spec.terminal_path)
        if lock_identity:
            self.terminal.setEnabled(False)
        profile = self._profile_by_id(self._current_pid)
        idx = self.profile_combo.findData(self._current_pid)
        if self._current_pid and (profile is None or idx < 0):
            # Unknown id (e.g. the profile was deleted or the list was not
            # passed in): fall back to own settings so an unchanged Accept
            # cannot persist a dangling profile reference.
            self._current_pid = ""
            self._last_pid = ""
            idx = -1
        self.copy_settings.set_values(profile if profile is not None else spec)
        self.profile_combo.blockSignals(True)
        try:
            self.profile_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self.profile_combo.blockSignals(False)
        self.copy_settings.set_editable(profile is None)
        self._update_profile_hint(profile)

    def spec(self) -> AccountSpec | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        base = AccountSpec(
            id=self.id_edit.text().strip() or "s1",
            terminal_path=self.terminal.currentText().strip() or None,
            profile_id=self._current_pid or "")
        return replace(base, **self.copy_settings.values())


def add_slave(parent_window, profiles: Iterable[CopyProfile] = (),
              create_profile: Callable[[CopyProfile], None] | None = None
              ) -> AccountSpec | None:
    """Open the SlaveEditor modally against the main window's controller.
    Returns the configured AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, profiles=profiles,
                      create_profile=create_profile, parent=parent_window)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None


def edit_slave(parent_window, spec: AccountSpec,
               profiles: Iterable[CopyProfile] = (),
               create_profile: Callable[[CopyProfile], None] | None = None
               ) -> AccountSpec | None:
    """Open the SlaveEditor modally, pre-populated with `spec` (identity
    locked). Returns the edited AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, profiles=profiles,
                      create_profile=create_profile, parent=parent_window)
    dlg.set_spec(spec, lock_identity=True)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None
