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
    assert w.step_amount.isHidden()
    assert not w.master_base_lot.isHidden()  # base-lot scaling applies to fixed lot


def _accept_with_name(name):
    def stub(self):
        self.name_edit.setText(name)
        self.accept()
        return self.result()
    return stub


def test_profile_editor_prepopulates_and_returns_profile(qapp):
    from manager.gui.profile_editor import ProfileEditor
    from manager.app.controller import CopyProfile
    p = CopyProfile(id="p1", name="A", symbol_map_csv="EURUSD=EURUSD",
                    step_amount=500.0, fixed_lot=0.07)
    dlg = ProfileEditor(p)
    assert dlg.name_edit.text() == "A"
    assert dlg.copy_settings.values()["step_amount"] == 500.0
    dlg.accept()
    out = dlg.profile()
    assert out.id == "p1" and out.name == "A"
    assert out.symbol_map_csv == "EURUSD=EURUSD"
    assert out.step_amount == 500.0 and out.fixed_lot == 0.07


def test_profile_editor_rejects_empty_and_duplicate_names(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor
    from PySide6.QtWidgets import QDialog, QMessageBox
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""))
    dlg = ProfileEditor(existing_names={"Propfirm A"})
    dlg.name_edit.setText("   ")
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted
    dlg.name_edit.setText("propfirm a")
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert len(warnings) == 2


def test_profile_editor_rejects_non_numeric_field(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor
    from PySide6.QtWidgets import QDialog, QMessageBox
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    dlg = ProfileEditor()
    dlg.name_edit.setText("A")
    dlg.copy_settings.step_amount.setText("abc")
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert warnings
    # the warning names the offending field, not a generic message
    assert "Step amount" in warnings[0][2]


def test_profile_editor_rejects_non_finite_numeric_fields(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor
    from PySide6.QtWidgets import QDialog, QMessageBox
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""))
    for text in ("nan", "1e400"):
        warnings.clear()
        dlg = ProfileEditor()
        dlg.name_edit.setText("A")
        dlg.copy_settings.step_amount.setText(text)
        dlg.accept()
        assert dlg.result() != QDialog.DialogCode.Accepted, text
        assert warnings and "Step amount" in warnings[0], text


def test_new_profile_generates_id_and_uses_seed(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor, new_profile
    from manager.app.controller import CopyProfile
    monkeypatch.setattr(ProfileEditor, "exec", _accept_with_name("Neu"))
    seed = CopyProfile(id="", name="", step_amount=500.0)
    out = new_profile(seed=seed, existing_ids={"aaaaaaaa"})
    assert out is not None
    assert out.id and out.id != "aaaaaaaa"
    assert out.name == "Neu" and out.step_amount == 500.0


def test_edit_profile_returns_edited_profile(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor, edit_profile
    from manager.app.controller import CopyProfile
    monkeypatch.setattr(ProfileEditor, "exec", _accept_with_name("B"))
    out = edit_profile(CopyProfile(id="p1", name="A"))
    assert out is not None and out.id == "p1" and out.name == "B"


def test_copy_settings_widget_follow_master_lot_round_trip(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    assert w.follow_master_lot.isChecked() is False
    w.follow_master_lot.setChecked(True)
    assert w.values()["follow_master_lot"] is True
    w.set_values({"follow_master_lot": False})
    assert w.follow_master_lot.isChecked() is False
    w.set_editable(False)
    assert not w.follow_master_lot.isEnabled()


def test_copy_settings_widget_follow_master_lot_visibility(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    for mode in ("balance_step", "fixed_lot"):
        w.sizing_mode.setCurrentIndex(w.sizing_mode.findData(mode))
        assert not w.follow_master_lot.isHidden()
    w.sizing_mode.setCurrentIndex(w.sizing_mode.findData("copy_master"))
    assert w.follow_master_lot.isHidden()
