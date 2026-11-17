from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QVBoxLayout,
)

from manager.engine.symbol_assist import (
    SymbolAssistError, SymbolRule, find_conflicts, infer_symbol_rule,
    validate_rule,
)
from manager.engine.transform import SymbolMapper


@dataclass(frozen=True)
class AssistantResult:
    master_pattern: str
    slave_template: str
    insert_before: int | None = None


class SymbolAssistant(QDialog):
    """Example-based symbol-rule assistant, opened from the symbol map."""

    def __init__(self, existing_rows: list[tuple[str, str]] | None = None,
                 parent=None):
        super().__init__(parent)
        self._existing_rows = list(existing_rows or [])
        self._manual = False
        self._filling = False
        self._result: AssistantResult | None = None
        self._conflicts = []

        self.setWindowTitle("Symbol Assistant")
        self.setModal(True)
        root = QVBoxLayout(self)

        root.addWidget(QLabel("Examples (master → slave)"))
        self.examples_table = QTableWidget(0, 2)
        self.examples_table.setHorizontalHeaderLabels(
            ["Master example", "Slave example"])
        self.examples_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.examples_table)
        example_buttons = QHBoxLayout()
        self.add_example_button = QPushButton("+ Example")
        self.remove_example_button = QPushButton("−")
        example_buttons.addWidget(self.add_example_button)
        example_buttons.addWidget(self.remove_example_button)
        root.addLayout(example_buttons)

        rule_form = QFormLayout()
        self.master_pattern_edit = QLineEdit()
        self.slave_template_edit = QLineEdit()
        self.rederive_button = QPushButton("Re-derive")
        rule_form.addRow("Master pattern", self.master_pattern_edit)
        rule_form.addRow("Slave template", self.slave_template_edit)
        rule_form.addRow("", self.rederive_button)
        root.addLayout(rule_form)

        preview_form = QFormLayout()
        self.preview_input = QLineEdit()
        self.preview_label = QLabel("")
        preview_form.addRow("Test master symbol", self.preview_input)
        preview_form.addRow("Result", self.preview_label)
        root.addLayout(preview_form)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        self.conflict_label = QLabel("")
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setVisible(False)
        root.addWidget(self.conflict_label)

        conflict_buttons = QHBoxLayout()
        self.insert_above_button = QPushButton("Insert above")
        self.apply_anyway_button = QPushButton("Apply anyway")
        self.insert_above_button.setVisible(False)
        self.apply_anyway_button.setVisible(False)
        conflict_buttons.addWidget(self.insert_above_button)
        conflict_buttons.addWidget(self.apply_anyway_button)
        root.addLayout(conflict_buttons)

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel")
        self.apply_button = QPushButton("Apply")
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.apply_button)
        root.addLayout(buttons)

        self.add_example_button.clicked.connect(
            lambda: self._add_example_row())
        self.remove_example_button.clicked.connect(self._on_remove_example)
        self.rederive_button.clicked.connect(self._rederive)
        self.apply_button.clicked.connect(self._on_apply)
        self.cancel_button.clicked.connect(self.reject)
        self.insert_above_button.clicked.connect(self._on_insert_above)
        self.apply_anyway_button.clicked.connect(
            lambda: self._finish(None))
        self.examples_table.itemChanged.connect(self._on_example_changed)
        self.master_pattern_edit.textChanged.connect(self._on_rule_changed)
        self.slave_template_edit.textChanged.connect(self._on_rule_changed)
        self.preview_input.textChanged.connect(self._update_preview)

        self._add_example_row()

    # ---- examples ----
    def _add_example_row(self, master: str = "", slave: str = "") -> None:
        row = self.examples_table.rowCount()
        self.examples_table.insertRow(row)
        self.examples_table.setItem(row, 0, QTableWidgetItem(master))
        self.examples_table.setItem(row, 1, QTableWidgetItem(slave))

    def _on_remove_example(self) -> None:
        row = self.examples_table.currentRow()
        if row >= 0:
            self.examples_table.removeRow(row)
        self._on_example_changed(None)

    def _examples(self) -> list[tuple[str, str]]:
        pairs = []
        for row in range(self.examples_table.rowCount()):
            master = self.examples_table.item(row, 0)
            slave = self.examples_table.item(row, 1)
            master_text = (master.text() if master else "").strip()
            slave_text = (slave.text() if slave else "").strip()
            if master_text and slave_text:
                pairs.append((master_text, slave_text))
        return pairs

    def _on_example_changed(self, _item) -> None:
        if self._filling:
            return
        if not self._manual and self._examples():
            self._rederive()
        self._update_preview()

    # ---- rule / preview ----
    def _set_rule(self, pattern: str, template: str) -> None:
        self._filling = True
        self.master_pattern_edit.setText(pattern)
        self.slave_template_edit.setText(template)
        self._filling = False

    def _on_rule_changed(self, _text) -> None:
        if self._filling:
            return
        self._manual = True
        self._clear_conflict()
        self._update_preview()

    def _rederive(self) -> None:
        self._manual = False
        pairs = self._examples()
        if not pairs:
            self._show_error("At least one complete example is required.")
            return
        try:
            rule = infer_symbol_rule(pairs)
        except SymbolAssistError as exc:
            self._show_error(str(exc))
            return
        self._clear_error()
        self._set_rule(rule.master_pattern, rule.slave_template)
        self._update_preview()

    def _update_preview(self) -> None:
        pairs = self._examples()
        symbol = (self.preview_input.text().strip()
                  or (pairs[0][0] if pairs else ""))
        pattern = self.master_pattern_edit.text().strip()
        template = self.slave_template_edit.text().strip()
        if not symbol or not pattern or not template:
            self.preview_label.setText("")
            return
        try:
            mapper = SymbolMapper(f"{pattern}={template}", lambda s: False)
            result = mapper.resolve(symbol)
        except Exception as exc:  # invalid regex / malformed map string
            self.preview_label.setText(f"invalid rule: {exc}")
            return
        self.preview_label.setText(
            f"{symbol} → {result}" if result else f"{symbol} → no match")

    # ---- apply ----
    def _on_apply(self) -> None:
        self._clear_error()
        self._clear_conflict()
        pairs = self._examples()
        if not pairs:
            self._show_error("At least one complete example is required.")
            return
        pattern = self.master_pattern_edit.text().strip()
        template = self.slave_template_edit.text().strip()
        if not pattern or not template:
            self._show_error(
                "The master pattern and slave template are required.")
            return
        if any(ch in pattern or ch in template for ch in ",="):
            self._show_error("The rule cannot contain ',' or '='.")
            return
        try:
            re.compile(pattern)
        except re.error as exc:
            self._show_error(f"Invalid master pattern: {exc}")
            return
        rule = SymbolRule(pattern, template)
        try:
            validate_rule(rule, pairs)
        except SymbolAssistError as exc:
            self._show_error(str(exc))
            return
        conflicts = find_conflicts(rule, pairs, self._existing_rows)
        if not conflicts:
            self._finish(None)
            return
        self._conflicts = conflicts
        lines = []
        for conflict in conflicts:
            existing_template = self._existing_rows[conflict.row_index][1]
            lines.append(
                f"Row {conflict.row_index + 1} "
                f"({conflict.master_example} → {existing_template}) would "
                f"win and opens {conflict.actual_slave} instead of "
                f"{conflict.expected_slave}.")
        if any(conflict.exact_tier for conflict in conflicts):
            lines.append(
                "An exact-tier row always wins regardless of order; edit or "
                "remove it — inserting above cannot fix it.")
        self.conflict_label.setText(" ".join(lines))
        self.conflict_label.setVisible(True)
        self.insert_above_button.setVisible(True)
        self.apply_anyway_button.setVisible(True)
        self.insert_above_button.setEnabled(
            any(not conflict.exact_tier for conflict in conflicts))

    def _on_insert_above(self) -> None:
        self._finish(min(conflict.row_index for conflict in self._conflicts
                         if not conflict.exact_tier))

    def _finish(self, insert_before: int | None) -> None:
        self._result = AssistantResult(
            self.master_pattern_edit.text().strip(),
            self.slave_template_edit.text().strip(),
            insert_before)
        self.accept()

    # ---- labels ----
    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def _clear_error(self) -> None:
        self.error_label.setText("")
        self.error_label.setVisible(False)

    def _clear_conflict(self) -> None:
        self._conflicts = []
        self.conflict_label.setText("")
        self.conflict_label.setVisible(False)
        self.insert_above_button.setVisible(False)
        self.apply_anyway_button.setVisible(False)
        self.insert_above_button.setEnabled(False)

    def rule(self) -> AssistantResult | None:
        return self._result
