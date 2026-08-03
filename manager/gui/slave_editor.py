# manager/gui/slave_editor.py
from __future__ import annotations

import subprocess
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QPushButton, QHBoxLayout, QCheckBox,
    QHeaderView,
)

from manager.app.controller import AccountSpec


class SlaveEditor(QDialog):
    """A modal dialog to add/edit one slave account: terminal-path dropdown
    (auto-populated, required — the user manually logs in to the terminal),
    an Open-terminal-for-login button, a master->slave symbol map table, lot-sizing
    fields, maxLot, maxTradeAge, and the normalize-SL/TP toggle. ``spec()``
    returns the configured AccountSpec (None if cancelled)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Slave")
        self._controller = controller
        self._build_ui()
        self._populate_terminals()

    def _build_ui(self):
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("s1")
        self.terminal = QComboBox()
        self.terminal.setEditable(True)
        form.addRow("Slave id", self.id_edit)
        form.addRow("Terminal", self.terminal)
        term_row = QHBoxLayout()
        self.launch_terminal_button = QPushButton("Open terminal for login")
        term_row.addWidget(self.launch_terminal_button)
        form.addRow("", term_row)
        root.addLayout(form)

        self.symbol_table = QTableWidget(0, 2)
        self.symbol_table.setHorizontalHeaderLabels(["Master symbol", "Slave symbol"])
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

        sizing = QFormLayout()
        self.step_amount = QLineEdit("100")
        self.step_size = QLineEdit("0.01")
        self.max_lot = QLineEdit("10")
        self.max_trade_age_minutes = QLineEdit("10")
        self.normalize_sltp = QCheckBox("Normalize SL/TP to slave open price")
        self.normalize_sltp.setChecked(True)
        sizing.addRow("Step amount", self.step_amount)
        sizing.addRow("Step size", self.step_size)
        sizing.addRow("Max lots", self.max_lot)
        sizing.addRow("Max trade age (min)", self.max_trade_age_minutes)
        root.addLayout(sizing)
        root.addWidget(self.normalize_sltp)

        buttons = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        root.addLayout(buttons)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)

    def _on_launch_terminal(self):
        exe = self.terminal.currentText().strip()
        if not exe:
            return
        try:
            subprocess.Popen([exe])
        except OSError:
            pass

    def _add_sym_row(self):
        self.symbol_table.insertRow(self.symbol_table.rowCount())
        self.symbol_table.setItem(self.symbol_table.rowCount() - 1, 0, QTableWidgetItem(""))
        self.symbol_table.setItem(self.symbol_table.rowCount() - 1, 1, QTableWidgetItem(""))

    def _del_sym_row(self):
        r = self.symbol_table.currentRow()
        if r >= 0:
            self.symbol_table.removeRow(r)

    def _populate_terminals(self):
        self.terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.terminal.addItem(inst.exe_path)
        except Exception:
            pass

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

    def _spec_from_fields(self, sid, terminal_path, step_amount, step_size,
                          max_lot, max_age, normalize) -> AccountSpec:
        return AccountSpec(
            id=sid, terminal_path=terminal_path or None,
            symbol_map_csv=self._symbol_map_csv(),
            step_amount=float(step_amount), step_size=float(step_size),
            max_lot=float(max_lot), max_trade_age_minutes=float(max_age),
            normalize_sltp=bool(normalize))

    def spec(self) -> AccountSpec | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        return self._spec_from_fields(
            self.id_edit.text().strip() or "s1",
            self.terminal.currentText().strip(),
            self.step_amount.text(), self.step_size.text(),
            self.max_lot.text(), self.max_trade_age_minutes.text(),
            self.normalize_sltp.isChecked())


def add_slave(parent_window) -> AccountSpec | None:
    """Open the SlaveEditor modally against the main window's controller.
    Returns the configured AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, parent=parent_window)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None
