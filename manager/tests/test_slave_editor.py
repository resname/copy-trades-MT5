# manager/tests/test_slave_editor.py
import pytest

pytest.importorskip("PySide6")


class FakeController:
    def __init__(self, instances=None):
        self._instances = instances or []
    def discover_instances(self):
        return self._instances


def test_slave_editor_constructs(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.terminal.discovery import TerminalInstance
    c = FakeController([TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")])
    dlg = SlaveEditor(c)
    assert dlg.terminal is not None
    assert dlg.launch_terminal_button is not None
    assert dlg.symbol_table is not None
    assert dlg.step_amount is not None
    assert dlg.max_lot is not None
    assert dlg.normalize_sltp is not None
    items = [dlg.terminal.itemText(i) for i in range(dlg.terminal.count())]
    assert "C:/i0/terminal64.exe" in items


def test_slave_editor_spec_returns_accountspec(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.id_edit.setText("s1")
    dlg.terminal.setCurrentIndex(0)
    dlg.step_amount.setText("100")
    dlg.step_size.setText("0.01")
    dlg.max_lot.setText("10")
    dlg.max_trade_age_minutes.setText("10")
    dlg.accept()                      # simulate the user clicking OK
    spec = dlg.spec()
    assert isinstance(spec, AccountSpec)
    assert spec.id == "s1"
    assert spec.terminal_path == "C:/i0/terminal64.exe"
    assert spec.max_lot == 10.0
    assert spec.normalize_sltp is True


def test_slave_editor_symbol_table_round_trips_into_csv(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    dlg.symbol_table.setRowCount(1)
    dlg.symbol_table.setItem(0, 0, _qitem("EURUSD"))
    dlg.symbol_table.setItem(0, 1, _qitem("EURUSD"))
    spec = dlg._spec_from_fields("s2", "C:/i0/terminal64.exe", "100", "0.01",
                                 "10", "10", True)
    assert "EURUSD=EURUSD" in spec.symbol_map_csv


def test_slave_editor_launch_button_runs_terminal64_exe(qapp, monkeypatch):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.terminal.setCurrentIndex(0)
    popped = []
    monkeypatch.setattr("manager.gui.slave_editor.subprocess.Popen",
                        lambda cmd, **k: popped.append(cmd) or object())
    dlg.launch_terminal_button.click()
    assert popped == [["C:/i0/terminal64.exe"]]


def test_slave_editor_launch_button_labeled_for_login(qapp):
    from manager.gui.slave_editor import SlaveEditor
    class _C:
        def discover_instances(self): return []
    dlg = SlaveEditor(_C())
    assert dlg.launch_terminal_button.text() == "Open terminal for login"


def _inst(exe):
    from manager.terminal.discovery import TerminalInstance
    return TerminalInstance(exe.rsplit("/terminal64.exe", 1)[0], exe, "appdata")


def _qitem(text):
    from PySide6.QtWidgets import QTableWidgetItem
    return QTableWidgetItem(text)
