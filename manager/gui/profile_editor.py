# manager/gui/profile_editor.py
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass, replace

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QPushButton, QHBoxLayout, QCheckBox,
    QHeaderView, QDialog, QDialogButtonBox, QMessageBox, QSizePolicy,
)

from manager.app.controller import (
    AccountSpec, CopyProfile, SHARED_PROFILE_FIELDS, new_profile_id,
)
from manager.engine.transform import parse_symbol_map


def fit_combo_width(combo: QComboBox, contents_length: int) -> None:
    """A combo sizes itself to its longest item by default; a long terminal
    path or profile name would then force the whole dialog wider than the
    screen. Cap the width at a readable contents length — the popup still
    lists every item (elided) and item tooltips carry the full text."""
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(contents_length)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


# (attribute name, user-facing label) for the numeric shared fields. The label
# is the form-row caption, so an invalid value is named in the warning.
_NUMERIC_FIELD_LABELS = (
    ("step_amount", "Step amount"),
    ("step_size", "Step size"),
    ("max_lot", "Max lots"),
    ("max_trade_age_minutes", "Max trade age (min)"),
    ("master_base_lot", "Master base lot size"),
    ("fixed_lot", "Fixed lot size"),
)


class CopySettingsWidget(QWidget):
    """The copy-trading settings shared by slave accounts and copy profiles:
    the master->slave symbol map table with Add/Remove Row, the lot-sizing
    mode and its fields, and the normalize SL/TP checkbox.

    ``values()`` returns the shared-field dict (numbers as floats);
    ``set_values`` fills the widget from a mapping, AccountSpec or CopyProfile;
    ``set_editable(False)`` disables every input, as used for the read-only
    preview of a profile-attached slave."""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        # The widget is embedded into existing dialogs that already own their
        # layout margins; no extra padding keeps the extracted UI identical.
        root.setContentsMargins(0, 0, 0, 0)

        self.symbol_table = QTableWidget(0, 2)
        self.symbol_table.setHorizontalHeaderLabels(
            ["Master symbol (regex)", "Slave symbol"])
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.symbol_table)
        sym_row = QHBoxLayout()
        self.add_sym_button = QPushButton("Add Row")
        self.del_sym_button = QPushButton("Remove Row")
        sym_row.addWidget(self.add_sym_button)
        sym_row.addWidget(self.del_sym_button)
        root.addLayout(sym_row)
        self.add_sym_button.clicked.connect(self._add_sym_row)
        self.del_sym_button.clicked.connect(self._del_sym_row)

        self._sizing = QFormLayout()
        self.sizing_mode = QComboBox()
        self.sizing_mode.addItem("Balance step (lots step)", "balance_step")
        self.sizing_mode.addItem("Copy master lot", "copy_master")
        self.sizing_mode.addItem("Fixed lot", "fixed_lot")
        self.step_amount = QLineEdit("100")
        self.step_size = QLineEdit("0.01")
        self.max_lot = QLineEdit("10")
        self.max_trade_age_minutes = QLineEdit("10")
        self.master_base_lot = QLineEdit("0.0")
        self.fixed_lot = QLineEdit("0.01")
        self.normalize_sltp = QCheckBox("Normalize SL/TP to slave open price")
        self.normalize_sltp.setChecked(True)
        self._sizing.addRow("Lot sizing mode", self.sizing_mode)
        self._sizing.addRow("Master base lot size", self.master_base_lot)
        self._sizing.addRow("Fixed lot size", self.fixed_lot)
        self._sizing.addRow("Step amount", self.step_amount)
        self._sizing.addRow("Step size", self.step_size)
        self._sizing.addRow("Max lots", self.max_lot)
        self._sizing.addRow("Max trade age (min)", self.max_trade_age_minutes)
        root.addLayout(self._sizing)
        root.addWidget(self.normalize_sltp)
        self.sizing_mode.currentIndexChanged.connect(self._update_sizing_visibility)
        self._update_sizing_visibility()

    # ---- symbol map table ----
    def _add_sym_row(self) -> None:
        r = self.symbol_table.rowCount()
        self.symbol_table.insertRow(r)
        self.symbol_table.setItem(r, 0, QTableWidgetItem(""))
        self.symbol_table.setItem(r, 1, QTableWidgetItem(""))

    def _del_sym_row(self) -> None:
        r = self.symbol_table.currentRow()
        if r >= 0:
            self.symbol_table.removeRow(r)

    def _symbol_map_csv(self) -> str:
        pairs = []
        for r in range(self.symbol_table.rowCount()):
            m = self.symbol_table.item(r, 0)
            s = self.symbol_table.item(r, 1)
            if m is None or s is None:
                continue
            mt = m.text().strip()
            st = s.text().strip()
            if mt and st:
                pairs.append(f"{mt}={st}")
        return ",".join(pairs)

    # ---- mode-dependent visibility ----
    def _update_sizing_visibility(self) -> None:
        """Show only the lot-sizing fields relevant to the chosen mode.
        Widgets stay constructed in every mode (so callers can read them);
        only visibility toggles. max_lot + max_trade_age are always relevant
        (cap + age apply to all modes)."""
        mode = self.sizing_mode.currentData()

        def show_row(widget, visible: bool) -> None:
            widget.setVisible(visible)
            lbl = self._sizing.labelForField(widget)
            if lbl is not None:
                lbl.setVisible(visible)

        is_balance = mode == "balance_step"
        is_fixed = mode == "fixed_lot"
        show_row(self.step_amount, is_balance)
        show_row(self.step_size, is_balance)
        show_row(self.master_base_lot, is_balance)
        show_row(self.fixed_lot, is_fixed)

    # ---- data in/out ----
    def values(self) -> dict[str, object]:
        """Return the shared-profile fields; every numeric field is a float."""
        return {
            "symbol_map_csv": self._symbol_map_csv(),
            "step_amount": float(self.step_amount.text()),
            "step_size": float(self.step_size.text()),
            "max_lot": float(self.max_lot.text()),
            "max_trade_age_minutes": float(self.max_trade_age_minutes.text()),
            "normalize_sltp": self.normalize_sltp.isChecked(),
            "sizing_mode": self.sizing_mode.currentData(),
            "master_base_lot": float(self.master_base_lot.text()),
            "fixed_lot": float(self.fixed_lot.text()),
        }

    def set_values(self, source: Mapping[str, object] | AccountSpec
                   | CopyProfile) -> None:
        """Populate the widget from a mapping or dataclass (AccountSpec /
        CopyProfile). Fields absent from a mapping are left untouched."""
        data = _shared_fields(source)
        if "symbol_map_csv" in data:
            csv = data["symbol_map_csv"]
            if not isinstance(csv, str):
                csv = ""
            self.symbol_table.setRowCount(0)
            for master, slave in parse_symbol_map(csv).items():
                r = self.symbol_table.rowCount()
                self.symbol_table.insertRow(r)
                self.symbol_table.setItem(r, 0, QTableWidgetItem(master))
                self.symbol_table.setItem(r, 1, QTableWidgetItem(slave))
        if "sizing_mode" in data:
            idx = self.sizing_mode.findData(data["sizing_mode"])
            self.sizing_mode.setCurrentIndex(idx if idx >= 0 else 0)
        for name, widget in (
            ("step_amount", self.step_amount),
            ("step_size", self.step_size),
            ("max_lot", self.max_lot),
            ("max_trade_age_minutes", self.max_trade_age_minutes),
            ("master_base_lot", self.master_base_lot),
            ("fixed_lot", self.fixed_lot),
        ):
            if name in data:
                widget.setText(str(data[name]))
        if "normalize_sltp" in data:
            self.normalize_sltp.setChecked(bool(data["normalize_sltp"]))

    def set_editable(self, editable: bool) -> None:
        """Enable/disable every input and both symbol-row buttons."""
        for widget in (
            self.symbol_table, self.add_sym_button, self.del_sym_button,
            self.sizing_mode, self.step_amount, self.step_size, self.max_lot,
            self.max_trade_age_minutes, self.master_base_lot, self.fixed_lot,
            self.normalize_sltp,
        ):
            widget.setEnabled(editable)


class ProfileEditor(QDialog):
    """Modal dialog that edits a CopyProfile's name and shared settings.

    ``accept()`` refuses an empty or case-insensitively duplicate name and
    non-numeric sizing fields, showing a warning parented to the dialog and
    staying open. ``profile()`` returns the resulting CopyProfile only when
    the dialog was accepted, otherwise None."""

    def __init__(self, profile: CopyProfile | None = None, *,
                 existing_names: Iterable[str] = (), parent=None):
        super().__init__(parent)
        self._profile = profile
        self._existing_names = tuple(existing_names)
        self._result_name = ""
        self._result_values: dict[str, object] | None = None

        self.setWindowTitle("Copy Profile")
        self.setModal(True)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(profile.name if profile is not None else "")
        form.addRow("Profile name", self.name_edit)

        self.copy_settings = CopySettingsWidget()
        if profile is not None:
            self.copy_settings.set_values(profile)
        form.addRow(self.copy_settings)
        root.addLayout(form)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        root.addWidget(self.button_box)

    def set_profile(self, profile: CopyProfile) -> None:
        """Replace the profile shown by the dialog."""
        self._profile = profile
        self.name_edit.setText(profile.name)
        self.copy_settings.set_values(profile)

    def accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Invalid profile",
                                "Profile name cannot be empty.")
            return
        taken = {n.casefold() for n in self._existing_names}
        if name.casefold() in taken:
            QMessageBox.warning(
                self, "Invalid profile",
                f"A profile named '{name}' already exists.")
            return
        # Validate each numeric field individually so the warning can name the
        # offending one, and reject non-finite values (float() accepts nan/inf
        # and 1e400 is already inf — they would raise later when sizing lots).
        for field, label in _NUMERIC_FIELD_LABELS:
            text = getattr(self.copy_settings, field).text()
            try:
                value = float(text)
            except (TypeError, ValueError):
                QMessageBox.warning(
                    self, "Invalid profile",
                    f"{label} must be a number.")
                return
            if not math.isfinite(value):
                QMessageBox.warning(
                    self, "Invalid profile",
                    f"{label} must be a finite number.")
                return
        try:
            values = self.copy_settings.values()
        except (TypeError, ValueError):
            QMessageBox.warning(
                self, "Invalid profile",
                "Every numeric field must contain a number.")
            return
        self._result_name = name
        self._result_values = values
        super().accept()

    def profile(self) -> CopyProfile | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        profile_id = self._profile.id if self._profile is not None else ""
        if not profile_id:
            profile_id = new_profile_id()
        values = self._result_values
        if values is None:
            values = self.copy_settings.values()
        name = self._result_name or self.name_edit.text().strip()
        return CopyProfile(id=profile_id, name=name, **values)


def new_profile(*, existing_names: Iterable[str] = (),
                existing_ids: Iterable[str] = (),
                seed: CopyProfile | None = None,
                parent=None) -> CopyProfile | None:
    """Open a ProfileEditor seeded from ``seed`` and return the new profile,
    or None if cancelled. A fresh id avoids ``existing_ids``."""
    if seed is None:
        seed = CopyProfile(id=new_profile_id(existing_ids), name="")
    else:
        seed = replace(seed, id=new_profile_id(existing_ids))
    dlg = ProfileEditor(seed, existing_names=existing_names, parent=parent)
    dlg.exec()
    return dlg.profile()


def edit_profile(profile: CopyProfile, *, existing_names: Iterable[str] = (),
                 parent=None) -> CopyProfile | None:
    """Open a ProfileEditor for ``profile`` and return the edited copy
    (same id), or None if cancelled."""
    dlg = ProfileEditor(profile, existing_names=existing_names, parent=parent)
    dlg.exec()
    return dlg.profile()


def _shared_fields(source: Mapping[str, object] | AccountSpec | CopyProfile
                   ) -> dict[str, object]:
    """Extract the SHARED_PROFILE_FIELDS present on a mapping or dataclass."""
    if isinstance(source, Mapping):
        return {name: source[name] for name in SHARED_PROFILE_FIELDS
                if name in source}
    if is_dataclass(source) and not isinstance(source, type):
        return {f.name: getattr(source, f.name) for f in fields(source)
                if f.name in SHARED_PROFILE_FIELDS}
    raise TypeError(
        "set_values expects a Mapping, AccountSpec or CopyProfile; got "
        f"{type(source).__name__}")
