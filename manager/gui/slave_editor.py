# manager/gui/slave_editor.py
from __future__ import annotations

import subprocess
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QPushButton, QHBoxLayout, QCheckBox,
    QHeaderView,
)

from manager.app.controller import AccountSpec
from manager.engine.transform import parse_symbol_map


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
        self._on_platform_changed()

    def _build_ui(self):
        root = QVBoxLayout(self)
        self._top_form = form = QFormLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("s1")
        self.platform = QComboBox()
        self.platform.addItem("MetaTrader terminal", "mt5")
        self.platform.addItem("IB Gateway (TWS API)", "ib")
        self.terminal = QComboBox()
        self.terminal.setEditable(True)
        form.addRow("Slave id", self.id_edit)
        form.addRow("Slave platform", self.platform)
        form.addRow("Terminal", self.terminal)
        term_row = QHBoxLayout()
        self.launch_terminal_button = QPushButton("Open terminal for login")
        term_row.addWidget(self.launch_terminal_button)
        form.addRow("", term_row)
        self.ib_host = QLineEdit("127.0.0.1")
        self.ib_port = QLineEdit("4002")
        self.ib_client_id = QLineEdit("7")
        form.addRow("IB host", self.ib_host)
        form.addRow("IB port", self.ib_port)
        form.addRow("IB client id", self.ib_client_id)
        root.addLayout(form)

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

        buttons = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        root.addLayout(buttons)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.platform.currentIndexChanged.connect(self._on_platform_changed)

    def _on_platform_changed(self, *_args) -> None:
        """MT5 mode: terminal row + 2-column symbol map. IB mode: hide the
        MetaTrader terminal row, show the IB connection fields, and widen the
        symbol table to the 5 contract columns (Master, Symbol, Exchange,
        SecType, Master $/pt)."""
        ib = self.platform.currentData() == "ib"
        def show(widget, visible: bool) -> None:
            widget.setVisible(visible)
            lbl = self._top_form.labelForField(widget)
            if lbl is not None:
                lbl.setVisible(visible)
        show(self.terminal, not ib)
        self.launch_terminal_button.setVisible(not ib)
        show(self.ib_host, ib)
        show(self.ib_port, ib)
        show(self.ib_client_id, ib)
        headers = self._IB_HEADERS if ib else self._MT5_HEADERS
        self.symbol_table.setColumnCount(5 if ib else 2)
        self.symbol_table.setHorizontalHeaderLabels(headers)
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

    _MT5_HEADERS = ["Master symbol (regex)", "Slave symbol"]
    _IB_HEADERS = ["Master symbol (regex)", "Symbol", "Exchange", "SecType",
                   "Master $/pt"]

    def _update_sizing_visibility(self) -> None:
        """Show only the lot-sizing fields relevant to the chosen mode.
        Widgets stay constructed in every mode (so tests/old code can read
        them); only visibility toggles. max_lot + max_trade_age are always
        relevant (cap + age apply to all modes)."""
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

    def _contract_map(self) -> dict:
        """IB mode only: read the 5-column contract table into the
        contract_map shape ({symbol: {"exchange", "sec_type",
        "master_point_value", "currency"}}). Rows missing any required
        cell are skipped."""
        out: dict = {}
        for r in range(self.symbol_table.rowCount()):
            def cell(r, c):
                item = self.symbol_table.item(r, c)
                return item.text().strip() if item is not None else ""
            m, sym, exch, sec, mpv = (cell(r, 0), cell(r, 1), cell(r, 2),
                                      cell(r, 3), cell(r, 4))
            if not (m and sym and exch and sec and mpv):
                continue
            out[sym] = {"exchange": exch.upper(), "sec_type": sec.upper(),
                        "master_point_value": float(mpv),
                        "currency": "USD"}
        return out

    def _spec_from_fields(self, sid, terminal_path, step_amount, step_size,
                          max_lot, max_age, normalize, sizing_mode,
                          master_base_lot, fixed_lot) -> AccountSpec:
        return AccountSpec(
            id=sid, terminal_path=terminal_path or None,
            symbol_map_csv=self._symbol_map_csv(),
            step_amount=float(step_amount), step_size=float(step_size),
            max_lot=float(max_lot), max_trade_age_minutes=float(max_age),
            normalize_sltp=bool(normalize),
            sizing_mode=sizing_mode, master_base_lot=float(master_base_lot),
            fixed_lot=float(fixed_lot))

    def set_spec(self, spec: AccountSpec, *, lock_identity: bool = True) -> None:
        """Pre-populate the editor from an existing AccountSpec (edit mode).
        When lock_identity is True, the slave id and terminal path are shown
        read-only/disabled so the slave's identity cannot change mid-edit."""
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
        idx = self.platform.findData(spec.platform or "mt5")
        self.platform.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_platform_changed()
        self.symbol_table.setRowCount(0)
        for master, slave in parse_symbol_map(spec.symbol_map_csv).items():
            r = self.symbol_table.rowCount()
            self.symbol_table.insertRow(r)
            self.symbol_table.setItem(r, 0, QTableWidgetItem(master))
            self.symbol_table.setItem(r, 1, QTableWidgetItem(slave))
            if spec.platform == "ib":
                d = (spec.contract_map or {}).get(slave, {})
                for col, text in enumerate(
                        [str(d.get("exchange", "")),
                         str(d.get("sec_type", "FUT")),
                         str(d.get("master_point_value", "1.0"))], start=2):
                    self.symbol_table.setItem(r, col, QTableWidgetItem(text))
        idx = self.sizing_mode.findData(spec.sizing_mode)
        self.sizing_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.master_base_lot.setText(str(spec.master_base_lot))
        self.fixed_lot.setText(str(spec.fixed_lot))
        # F5: an IB edit keeps its connection params; when identity is locked
        # the platform combo may not switch platforms (a locked existing-slave
        # edit would silently respawn as the wrong worker otherwise)
        self.platform.setEnabled(not lock_identity)
        self.ib_host.setText(spec.ib_host)
        self.ib_port.setText(str(spec.ib_port))
        self.ib_client_id.setText(str(spec.ib_client_id))
        self.step_amount.setText(str(spec.step_amount))
        self.step_size.setText(str(spec.step_size))
        self.max_lot.setText(str(spec.max_lot))
        self.max_trade_age_minutes.setText(str(spec.max_trade_age_minutes))
        self.normalize_sltp.setChecked(spec.normalize_sltp)

    def spec(self) -> AccountSpec | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        platform = self.platform.currentData()
        if platform == "ib":
            # Same float-parse behaviour as the MT5 path: a malformed numeric
            # field raises ValueErrors straight out of the dialog.
            return AccountSpec(
                id=self.id_edit.text().strip() or "s1",
                terminal_path=None,
                symbol_map_csv=self._symbol_map_csv(),
                step_amount=float(self.step_amount.text()),
                step_size=float(self.step_size.text()),
                max_lot=float(self.max_lot.text()),
                max_trade_age_minutes=float(self.max_trade_age_minutes.text()),
                normalize_sltp=self.normalize_sltp.isChecked(),
                sizing_mode=self.sizing_mode.currentData(),
                master_base_lot=float(self.master_base_lot.text()),
                fixed_lot=float(self.fixed_lot.text()),
                platform="ib",
                ib_host=self.ib_host.text().strip() or "127.0.0.1",
                ib_port=int(self.ib_port.text() or "4002"),
                ib_client_id=int(self.ib_client_id.text() or "7"),
                contract_map=self._contract_map())
        return self._spec_from_fields(
            self.id_edit.text().strip() or "s1",
            self.terminal.currentText().strip(),
            self.step_amount.text(), self.step_size.text(),
            self.max_lot.text(), self.max_trade_age_minutes.text(),
            self.normalize_sltp.isChecked(),
            self.sizing_mode.currentData(),
            self.master_base_lot.text(), self.fixed_lot.text())


def add_slave(parent_window) -> AccountSpec | None:
    """Open the SlaveEditor modally against the main window's controller.
    Returns the configured AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, parent=parent_window)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None


def edit_slave(parent_window, spec: AccountSpec) -> AccountSpec | None:
    """Open the SlaveEditor modally, pre-populated with `spec` (identity
    locked). Returns the edited AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, parent=parent_window)
    dlg.set_spec(spec, lock_identity=True)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None
