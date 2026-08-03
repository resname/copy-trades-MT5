# manager/gui/server_picker.py
from __future__ import annotations

import re

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QComboBox, QPushButton,
)

from manager.brokers.catalog import PREVIOUSLY_USED

# trailing " (demo|real|unknown|manual)" label added to dropdown display text
_LABEL_RE = re.compile(r"\s*\((demo|real|unknown|manual)\)\s*$")
_MANUAL = "(manual)"


def _strip_label(text: str) -> str:
    return _LABEL_RE.sub("", text or "").strip()


class _RefreshWorker(QThread):
    """Runs controller.refresh_brokers off the GUI thread; emits the new
    BrokerCatalog on done (Qt marshals the signal back to the GUI thread)."""
    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        self.done.emit(self._fn())


class BrokerServerPicker(QWidget):
    """A reusable Broker -> Server picker: an editable Broker combo, an
    editable Server combo (demo-first, display "<server> (demo|real)" with the
    raw name held as item user-data), and a Refresh button (best-effort live
    refresh off-thread). Used by the master form and the slave editor.

    The widget is purely a name selector — it holds no credentials and never
    logs in. ``server()`` returns the raw server name (selected item's
    user-data, or typed free-text with any label suffix stripped)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._refresh_worker = None

        form = QFormLayout(self)
        self.broker_combo = QComboBox()
        self.broker_combo.setEditable(True)
        self.server_combo = QComboBox()
        self.server_combo.setEditable(True)
        self.refresh_button = QPushButton("Refresh")

        broker_row = type(self)._row(self.broker_combo, self.refresh_button)
        form.addRow("Broker", broker_row)
        form.addRow("Server", self.server_combo)

        self.broker_combo.currentIndexChanged.connect(self._on_broker_changed)
        self.refresh_button.clicked.connect(self._on_refresh)

        self._populate_brokers()

    @staticmethod
    def _row(*widgets):
        from PySide6.QtWidgets import QHBoxLayout
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            row.addWidget(w)
        return row

    # ---- population ----
    def _catalog(self):
        return self._controller.get_catalog()

    def _populate_brokers(self, preserve_server: str = ""):
        prev_broker = self.broker_combo.currentText()
        prev_server = preserve_server or self.server_combo.currentText()
        self.broker_combo.blockSignals(True)
        self.broker_combo.clear()
        names = self._catalog().broker_names()
        if not names:
            names = [_MANUAL]  # no brokers at all -> free-text only
        for n in names:
            self.broker_combo.addItem(n)
        idx = self.broker_combo.findText(prev_broker)
        if idx >= 0:
            self.broker_combo.setCurrentIndex(idx)
        elif prev_broker:
            self.broker_combo.setEditText(prev_broker)
        self.broker_combo.blockSignals(False)
        self._populate_servers(prev_server)

    def _on_broker_changed(self, _idx):
        # broker changed -> show that broker's servers (demo-first); do not
        # carry the previous server (it belongs to another broker)
        self._populate_servers("")

    def _populate_servers(self, preserve: str):
        broker = self.broker_combo.currentText()
        if broker and broker != _MANUAL:
            servers = self._catalog().servers_for(broker)
        else:
            servers = []
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        for s in servers:
            self.server_combo.addItem(f"{s.name} ({s.type})", s.name)
        idx = self.server_combo.findData(preserve) if preserve else -1
        if idx >= 0:
            self.server_combo.setCurrentIndex(idx)
        elif servers:
            self.server_combo.setCurrentIndex(0)  # demo-first default
        elif preserve:
            self.server_combo.setEditText(preserve)
        self.server_combo.blockSignals(False)

    # ---- public API ----
    def server(self) -> str:
        """Raw server name: the selected item's user-data if a dropdown item is
        selected, else the typed free-text with any trailing label stripped."""
        idx = self.server_combo.currentIndex()
        data = self.server_combo.itemData(idx)
        if isinstance(data, str) and data:
            return data
        return _strip_label(self.server_combo.currentText())

    def set_server(self, name: str) -> None:
        """Pre-fill the server for an existing account: select the matching
        dropdown item if present, else set it as free text."""
        idx = self.server_combo.findData(name)
        if idx >= 0:
            self.server_combo.setCurrentIndex(idx)
        else:
            self.server_combo.setEditText(name)

    def set_broker(self, name: str) -> None:
        idx = self.broker_combo.findText(name)
        if idx >= 0:
            self.broker_combo.setCurrentIndex(idx)
        else:
            self.broker_combo.setEditText(name)

    # ---- refresh (best-effort, off-thread) ----
    def _on_refresh(self):
        self._refresh_worker = _RefreshWorker(self._controller.refresh_brokers,
                                               self)
        self._refresh_worker.done.connect(self._on_refresh_done)
        self.refresh_button.setEnabled(False)
        self._refresh_worker.start()

    def _on_refresh_done(self, _catalog):
        self._refresh_worker = None
        self.refresh_button.setEnabled(True)
        # repopulate, preserving the current broker + server if still present
        self._populate_brokers()