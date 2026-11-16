# manager/gui/slave_list_dialog.py
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton,
)


class SlaveListDialog(QDialog):
    """Large modal view of the manager's slave list with the same
    add/remove/edit actions. ``MainWindow._slaves`` stays the single source
    of truth: every action delegates to the manager, then the dialog
    refreshes its view."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("Slaves")
        self.setModal(True)
        self.resize(640, 600)

        root = QVBoxLayout(self)
        self.list_widget = QListWidget()
        root.addWidget(self.list_widget, 1)

        row = QHBoxLayout()
        self.add_button = QPushButton("Add Slave…")
        self.remove_button = QPushButton("Remove Slave")
        self.edit_button = QPushButton("Edit Slave…")
        self.close_button = QPushButton("Close")
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addWidget(self.edit_button)
        row.addStretch(1)
        row.addWidget(self.close_button)
        root.addLayout(row)

        self.add_button.clicked.connect(self._on_add)
        self.remove_button.clicked.connect(self._on_remove)
        self.edit_button.clicked.connect(self._on_edit)
        self.close_button.clicked.connect(self.reject)
        self.list_widget.itemDoubleClicked.connect(
            self._on_item_double_clicked)
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)
        self._refresh()

    def _selected_id(self) -> str:
        item = self.list_widget.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def _update_buttons(self) -> None:
        selected = bool(self._selected_id())
        self.remove_button.setEnabled(selected)
        self.edit_button.setEnabled(selected)

    def _refresh(self, select_id: str = "") -> None:
        selected = select_id or self._selected_id()
        self.list_widget.blockSignals(True)
        try:
            self.list_widget.clear()
            for spec in self._manager._slaves:
                label = self._manager._slave_label(spec)
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, spec.id)
                item.setToolTip(label)
                self.list_widget.addItem(item)
                if spec.id == selected:
                    self.list_widget.setCurrentItem(item)
        finally:
            self.list_widget.blockSignals(False)
        self._update_buttons()

    def _on_add(self) -> None:
        spec = self._manager._on_add_slave()
        self._refresh(spec.id if spec is not None else "")

    def _on_remove(self) -> None:
        slave_id = self._selected_id()
        if slave_id:
            self._manager._remove_slave_by_id(slave_id)
        self._refresh()

    def _on_edit(self) -> None:
        slave_id = self._selected_id()
        if slave_id:
            self._manager._edit_slave_by_id(slave_id)
        self._refresh()

    def _on_item_double_clicked(self, item) -> None:
        self.list_widget.setCurrentItem(item)
        self._on_edit()


def open_slave_list(parent_window) -> None:
    """Open the modal slave list against the main window's manager."""
    dlg = SlaveListDialog(parent_window, parent=parent_window)
    dlg.exec()
