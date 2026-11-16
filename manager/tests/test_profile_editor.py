import pytest

pytest.importorskip("PySide6")


def test_copy_settings_widget_constructs_with_expected_fields(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    assert w.symbol_table.columnCount() == 2
    assert w.symbol_table.horizontalHeaderItem(0).text() == "Master symbol (regex)"
    assert [w.sizing_mode.itemData(i) for i in range(w.sizing_mode.count())] == [
        "balance_step", "copy_master", "fixed_lot"]
    for name in ("step_amount", "step_size", "max_lot",
                 "max_trade_age_minutes", "master_base_lot", "fixed_lot",
                 "normalize_sltp"):
        assert getattr(w, name) is not None


def test_copy_settings_widget_values_and_set_values_round_trip(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    from manager.app.controller import AccountSpec
    w = CopySettingsWidget()
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD",
                       step_amount=500.0, step_size=0.02, max_lot=20.0,
                       max_trade_age_minutes=5.0, normalize_sltp=False,
                       sizing_mode="fixed_lot", master_base_lot=0.2,
                       fixed_lot=0.07)
    w.set_values(spec)
    assert w.symbol_table.rowCount() == 2
    out = w.values()
    assert out["symbol_map_csv"] == "EURUSD=EURUSD,GBPUSD=GBPUSD"
    assert out["step_amount"] == 500.0 and out["step_size"] == 0.02
    assert out["max_lot"] == 20.0 and out["max_trade_age_minutes"] == 5.0
    assert out["normalize_sltp"] is False
    assert out["sizing_mode"] == "fixed_lot"
    assert out["master_base_lot"] == 0.2 and out["fixed_lot"] == 0.07


def test_copy_settings_widget_set_editable_disables_everything(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    w.set_editable(False)
    assert not w.symbol_table.isEnabled()
    assert not w.sizing_mode.isEnabled()
    assert not w.step_amount.isEnabled()
    assert not w.normalize_sltp.isEnabled()
    assert not w.add_sym_button.isEnabled()
    w.set_editable(True)
    assert w.step_amount.isEnabled() and w.add_sym_button.isEnabled()


def test_copy_settings_widget_visibility_follows_mode(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    w.sizing_mode.setCurrentIndex(w.sizing_mode.findData("fixed_lot"))
    assert not w.fixed_lot.isHidden()
    assert w.step_amount.isHidden() and w.master_base_lot.isHidden()
