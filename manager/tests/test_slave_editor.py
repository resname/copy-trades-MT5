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
                                 "10", "10", True, "balance_step", "0.1", "0.01")
    assert "EURUSD=EURUSD" in spec.symbol_map_csv


def test_slave_editor_master_column_header_advertises_regex(qapp):
    # the master column accepts regex patterns; the header must say so
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    assert dlg.symbol_table.horizontalHeaderItem(0).text() == "Master symbol (regex)"
    assert dlg.symbol_table.horizontalHeaderItem(1).text() == "Slave symbol"


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


def test_set_spec_pre_populates_and_locks_identity(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController([_inst("C:/s1/terminal64.exe")]))
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD",
                       step_amount=500.0, step_size=0.02, max_lot=20.0,
                       max_trade_age_minutes=5.0, normalize_sltp=False)
    dlg.set_spec(spec, lock_identity=True)
    assert dlg.windowTitle() == "Edit Slave"
    assert dlg.id_edit.text() == "s1"
    assert dlg.id_edit.isReadOnly()
    assert dlg.terminal.currentText() == "C:/s1/terminal64.exe"
    assert not dlg.terminal.isEnabled()
    # symbol table has one row per mapped pair
    assert dlg.symbol_table.rowCount() == 2
    assert dlg.symbol_table.item(0, 0).text() == "EURUSD"
    assert dlg.symbol_table.item(0, 1).text() == "EURUSD"
    assert dlg.symbol_table.item(1, 0).text() == "GBPUSD"
    assert dlg.symbol_table.item(1, 1).text() == "GBPUSD"
    assert dlg.step_amount.text() == "500.0"
    assert dlg.step_size.text() == "0.02"
    assert dlg.max_lot.text() == "20.0"
    assert dlg.max_trade_age_minutes.text() == "5.0"
    assert dlg.normalize_sltp.isChecked() is False


def test_set_spec_round_trips_through_spec(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    spec = AccountSpec(id="s2", terminal_path="C:/s2/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD",
                       step_amount=100.0, step_size=0.01, max_lot=10.0,
                       max_trade_age_minutes=10.0, normalize_sltp=True)
    dlg = SlaveEditor(FakeController([_inst("C:/s2/terminal64.exe")]))
    dlg.set_spec(spec, lock_identity=True)
    dlg.accept()
    out = dlg.spec()
    assert out.id == "s2"
    assert out.terminal_path == "C:/s2/terminal64.exe"
    assert out.symbol_map_csv == "EURUSD=EURUSD,GBPUSD=GBPUSD"
    assert out.step_amount == 100.0 and out.step_size == 0.01
    assert out.max_lot == 10.0 and out.max_trade_age_minutes == 10.0
    assert out.normalize_sltp is True


def test_edit_slave_pre_populates_with_locked_identity(qapp, monkeypatch):
    """edit_slave opens a SlaveEditor pre-populated with `spec` and returns the
    edited spec. We avoid the real modal loop by patching exec to accept
    immediately, so spec() reflects the pre-populated (unchanged) values and
    identity stays locked."""
    from manager.gui.slave_editor import edit_slave, SlaveEditor
    from manager.app.controller import AccountSpec
    from PySide6.QtWidgets import QDialog, QWidget

    def stub_exec(self):
        self.setResult(QDialog.DialogCode.Accepted)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SlaveEditor, "exec", stub_exec)

    class _Win(QWidget):
        def __init__(self):
            super().__init__()
            self._controller = FakeController([_inst("C:/s1/terminal64.exe")])

    win = _Win()
    orig = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD", step_amount=100.0,
                       step_size=0.01, max_lot=10.0, max_trade_age_minutes=10.0,
                       normalize_sltp=True)
    out = edit_slave(win, orig)
    assert out is not None
    # identity locked (unchanged), trading params round-trip from set_spec
    assert out.id == "s1"
    assert out.terminal_path == "C:/s1/terminal64.exe"
    assert out.symbol_map_csv == "EURUSD=EURUSD"
    assert out.step_amount == 100.0 and out.normalize_sltp is True


def test_slave_editor_has_sizing_mode_combo(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    assert dlg.sizing_mode is not None
    assert dlg.sizing_mode.count() == 3
    datas = [dlg.sizing_mode.itemData(i) for i in range(dlg.sizing_mode.count())]
    assert set(datas) == {"balance_step", "copy_master", "fixed_lot"}


def test_slave_editor_balance_step_shows_step_fields_hides_fixed(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.sizing_mode.setCurrentIndex(0)  # balance_step
    assert not dlg.step_amount.isHidden()
    assert not dlg.step_size.isHidden()
    assert not dlg.master_base_lot.isHidden()
    assert dlg.fixed_lot.isHidden()


def test_slave_editor_copy_master_hides_step_and_base_fields(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    idx = dlg.sizing_mode.findData("copy_master")
    dlg.sizing_mode.setCurrentIndex(idx)
    assert dlg.step_amount.isHidden()
    assert dlg.step_size.isHidden()
    assert dlg.master_base_lot.isHidden()
    assert dlg.fixed_lot.isHidden()
    assert not dlg.max_lot.isHidden()  # cap always visible


def test_slave_editor_fixed_lot_shows_fixed_field_hides_step(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    idx = dlg.sizing_mode.findData("fixed_lot")
    dlg.sizing_mode.setCurrentIndex(idx)
    assert not dlg.fixed_lot.isHidden()
    assert not dlg.max_lot.isHidden()
    assert dlg.step_amount.isHidden()
    assert dlg.step_size.isHidden()
    assert dlg.master_base_lot.isHidden()


def test_slave_editor_spec_carries_sizing_mode(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.id_edit.setText("s1")
    dlg.terminal.setCurrentIndex(0)
    idx = dlg.sizing_mode.findData("copy_master")
    dlg.sizing_mode.setCurrentIndex(idx)
    dlg.master_base_lot.setText("0.2")
    dlg.fixed_lot.setText("0.3")
    dlg.accept()
    spec = dlg.spec()
    assert spec.sizing_mode == "copy_master"
    assert spec.master_base_lot == 0.2
    assert spec.fixed_lot == 0.3


def test_set_spec_pre_populates_sizing_fields(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController([_inst("C:/s1/terminal64.exe")]))
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD", step_amount=100.0,
                       step_size=0.01, max_lot=10.0, max_trade_age_minutes=10.0,
                       normalize_sltp=True, sizing_mode="fixed_lot",
                       master_base_lot=0.1, fixed_lot=0.07)
    dlg.set_spec(spec, lock_identity=True)
    assert dlg.sizing_mode.currentData() == "fixed_lot"
    assert dlg.fixed_lot.text() == "0.07"
    assert dlg.master_base_lot.text() == "0.1"
    # fixed_lot mode -> fixed field visible, step fields hidden
    assert not dlg.fixed_lot.isHidden()
    assert dlg.step_amount.isHidden()


# --- Task 9: platform picker + IB contract table ---------------------------

def test_editor_platform_mt5_default(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    assert dlg.platform.currentData() == "mt5"
    assert not dlg.terminal.isHidden()
    # mt5 mode keeps the original 2-column symbol table
    assert dlg.symbol_table.columnCount() == 2
    assert dlg.symbol_table.horizontalHeaderItem(1).text() == "Slave symbol"


def test_editor_ib_mode_hides_terminal_shows_ib_fields(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    dlg.platform.setCurrentIndex(1)             # IB
    assert dlg.terminal.isHidden()
    assert dlg.launch_terminal_button.isHidden()
    assert not dlg.ib_host.isHidden() and not dlg.ib_port.isHidden()
    assert not dlg.ib_client_id.isHidden()
    # header labels switch to the contract columns
    assert dlg.symbol_table.columnCount() == 5
    assert dlg.symbol_table.horizontalHeaderItem(4).text() == "Master $/pt"


def test_editor_ib_spec_roundtrip(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    dlg.platform.setCurrentIndex(1)
    dlg.id_edit.setText("ib1")
    dlg.symbol_table.setRowCount(1)
    for col, text in enumerate(["US30", "YM", "CME", "FUT", "1.0"]):
        dlg.symbol_table.setItem(0, col, _qitem(text))
    dlg.ib_port.setText("4002")
    dlg.sizing_mode.setCurrentIndex(1)          # copy_master
    dlg._update_sizing_visibility()
    dlg.accept()                                 # sets result() == Accepted; spec() reads it
    spec = dlg.spec()
    assert spec.platform == "ib" and spec.terminal_path is None
    assert spec.symbol_map_csv == "US30=YM"
    assert spec.contract_map == {"YM": {"exchange": "CME", "sec_type": "FUT",
                                        "master_point_value": 1.0,
                                        "currency": "USD"}}
    assert spec.sizing_mode == "copy_master"


def test_editor_edit_existing_ib_spec(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController())
    spec = AccountSpec(id="ib1", platform="ib", terminal_path=None,
                       symbol_map_csv="US30=YM",
                       contract_map={"YM": {"exchange": "CME", "sec_type": "FUT",
                                            "master_point_value": 1.0}})
    dlg.set_spec(spec, lock_identity=True)
    assert dlg.platform.currentData() == "ib"
    assert dlg.symbol_table.columnCount() == 5
    assert dlg.symbol_table.item(0, 1).text() == "YM"
    assert dlg.symbol_table.item(0, 2).text() == "CME"
    assert dlg.symbol_table.item(0, 3).text() == "FUT"
    assert dlg.symbol_table.item(0, 4).text() == "1.0"
    assert dlg.ib_port.text() == str(spec.ib_port)
    # terminal row hidden in IB edit mode
    assert dlg.terminal.isHidden()


def test_set_spec_locks_platform_combo_and_carries_ib_connection(qapp):
    """F5: a locked existing-slave edit must not be able to switch platforms
    (the respawn would come back as the wrong worker), and an IB slave's
    connection fields must round-trip into spec() — an edited port survives
    the respawn path via the supervisor's config propagation."""
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    spec = AccountSpec(id="ib1", platform="ib", terminal_path=None,
                       symbol_map_csv="US30=YM", ib_host="127.0.0.1",
                       ib_port=4001, ib_client_id=3,
                       contract_map={"YM": {"exchange": "CME",
                                            "sec_type": "FUT",
                                            "master_point_value": 1.0}})
    dlg = SlaveEditor(FakeController())
    dlg.set_spec(spec, lock_identity=True)
    assert not dlg.platform.isEnabled()          # platform locked
    assert dlg.ib_host.text() == "127.0.0.1"
    assert dlg.ib_port.text() == "4001"          # populated from the spec
    assert dlg.ib_client_id.text() == "3"
    # an unlocked (identity-free) edit keeps the platform switchable...
    dlg2 = SlaveEditor(FakeController())
    dlg2.set_spec(spec, lock_identity=False)
    assert dlg2.platform.isEnabled()
    # ...and an edited connection round-trips out of spec()
    dlg2.ib_port.setText("4002")
    dlg2.ib_client_id.setText("9")
    dlg2.accept()
    out = dlg2.spec()
    assert out.platform == "ib" and out.ib_port == 4002
    assert out.ib_client_id == 9 and out.ib_host == "127.0.0.1"
