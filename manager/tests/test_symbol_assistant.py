import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog

from manager.gui.symbol_assistant import AssistantResult, SymbolAssistant


def _fill(dlg, pairs):
    dlg.examples_table.setRowCount(0)
    for master, slave in pairs:
        dlg._add_example_row(master, slave)


def test_examples_auto_fill_the_rule_and_preview(qapp):
    dlg = SymbolAssistant()
    _fill(dlg, [("YMZ26", "YM_Z"), ("YMH27", "YM_H")])
    assert dlg.master_pattern_edit.text() == r"^(YM)([FGHJKMNQUVXZ])(\d{2})$"
    assert dlg.slave_template_edit.text() == "YM_$2"
    assert "YM_Z" in dlg.preview_label.text()


def test_manual_rule_edit_stops_auto_inference_and_rederive_restores(qapp):
    dlg = SymbolAssistant()
    _fill(dlg, [("YMZ26", "YM_Z")])
    dlg.master_pattern_edit.setText("MANUAL")
    dlg.examples_table.item(0, 0).setText("YMQ28")
    dlg.examples_table.item(0, 1).setText("YM_Q")
    assert dlg.master_pattern_edit.text() == "MANUAL"
    dlg.rederive_button.click()
    assert dlg.master_pattern_edit.text().startswith("^(YM)")


def test_apply_without_conflicts_returns_the_rule(qapp):
    dlg = SymbolAssistant()
    _fill(dlg, [("YMZ26", "YM_Z"), ("YMH27", "YM_H")])
    dlg.apply_button.click()
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert dlg.rule() == AssistantResult(
        r"^(YM)([FGHJKMNQUVXZ])(\d{2})$", "YM_$2", None)


def test_invalid_examples_keep_the_dialog_open(qapp):
    dlg = SymbolAssistant()
    _fill(dlg, [("YMZ", "YM_Z")])  # no year
    dlg.apply_button.click()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert dlg.rule() is None
    assert dlg.error_label.isVisible() or dlg.error_label.text()


def test_regex_conflict_offers_insert_above(qapp):
    existing = [(r"^(YM)([FGHJKMNQUVXZ])(\d{2})$", "OLD_$2")]
    dlg = SymbolAssistant(existing_rows=existing)
    _fill(dlg, [("YMZ26", "YM_Z")])
    dlg.apply_button.click()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert "OLD_$2" in dlg.conflict_label.text()
    assert dlg.insert_above_button.isEnabled()
    dlg.insert_above_button.click()
    assert dlg.rule() == AssistantResult(
        r"^(YM)([FGHJKMNQUVXZ])(\d{2})$", "YM_$2", 0)


def test_exact_tier_conflict_cannot_be_fixed_by_ordering(qapp):
    dlg = SymbolAssistant(existing_rows=[("YMZ26", "OLD")])
    _fill(dlg, [("YMZ26", "YM_Z")])
    dlg.apply_button.click()
    assert "exact" in dlg.conflict_label.text().lower()
    assert not dlg.insert_above_button.isEnabled()
    dlg.apply_anyway_button.click()
    assert dlg.rule().insert_before is None


def test_cancel_returns_no_rule(qapp):
    dlg = SymbolAssistant()
    _fill(dlg, [("YMZ26", "YM_Z")])
    dlg.cancel_button.click()
    assert dlg.rule() is None


def test_conflict_state_clears_when_an_example_changes(qapp):
    existing = [(r"^(YM)([FGHJKMNQUVXZ])(\d{2})$", "OLD_$2")]
    dlg = SymbolAssistant(existing_rows=existing)
    _fill(dlg, [("YMZ26", "YM_Z")])
    dlg.apply_button.click()
    assert dlg.conflict_label.text()
    dlg.examples_table.item(0, 1).setText("YM_H")  # example edit
    assert dlg.conflict_label.text() == ""
    assert dlg.insert_above_button.isHidden()
    assert dlg.apply_anyway_button.isHidden()


def test_dialog_surfaces_the_month_ambiguity_tip(qapp):
    dlg = SymbolAssistant()
    assert "month-code" in dlg.hint_label.text()


def test_apply_rejects_csv_unsafe_fields(qapp):
    dlg = SymbolAssistant()
    _fill(dlg, [("YMZ26", "YM_Z")])
    dlg.master_pattern_edit.setText("A,B")
    dlg.apply_button.click()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert "cannot contain" in dlg.error_label.text()
