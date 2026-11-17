import pytest

from manager.engine.symbol_assist import (
    RuleConflict, SymbolAssistError, SymbolRule,
    find_conflicts, infer_symbol_rule, validate_rule,
)


def test_infer_futures_family():
    rule = infer_symbol_rule([("YMZ26", "YM_Z"), ("YMH27", "YM_H")])
    assert rule.master_pattern == r"^(YM)([FGHJKMNQUVXZ])(\d{2})$"
    assert rule.slave_template == "YM_$2"


def test_inferred_rule_covers_other_months_and_years():
    rule = infer_symbol_rule([("YMZ26", "YM_Z"), ("YMH27", "YM_H")])
    validate_rule(rule, [("YMZ26", "YM_Z"), ("YMM27", "YM_M"),
                         ("YMQ28", "YM_Q")])


def test_infer_other_family():
    rule = infer_symbol_rule([("ESZ26", "ES_Z")])
    assert rule.master_pattern == r"^(ES)([FGHJKMNQUVXZ])(\d{2})$"
    assert rule.slave_template == "ES_$2"


def test_infer_different_slave_root():
    rule = infer_symbol_rule([("NQH27", "NAS_H")])
    assert rule.master_pattern == r"^(NQ)([FGHJKMNQUVXZ])(\d{2})$"
    assert rule.slave_template == "NAS_$2"


def test_infer_broker_suffix():
    rule = infer_symbol_rule([("YMH27.cfd", "YM_H.cfd")])
    assert rule.master_pattern == r"^(YM)([FGHJKMNQUVXZ])(\d{2})\.cfd$"
    assert rule.slave_template == "YM_$2.cfd"


def test_infer_slave_with_year():
    rule = infer_symbol_rule([("YMH27", "YM_H27")])
    assert rule.slave_template == "YM_$2$3"
    validate_rule(rule, [("YMZ26", "YM_Z26")])


def test_infer_four_digit_year():
    rule = infer_symbol_rule([("YMZ2026", "YM_Z")])
    assert rule.master_pattern == r"^(YM)([FGHJKMNQUVXZ])(\d{4})$"


def test_infer_root_contains_month_letter():
    rule = infer_symbol_rule([("YMM27", "YM_M")])
    assert rule.slave_template == "YM_$2"


def test_infer_escapes_literal_dollar_in_template():
    rule = infer_symbol_rule([("YMZ26", "YM_$Z")])
    assert rule.slave_template == "YM_$$$2"
    validate_rule(rule, [("YMZ26", "YM_$Z")])


@pytest.mark.parametrize("examples", [
    [],
    [("", "")],
    [("YMZ", "YM_Z")],                 # no year
    [("YMZ2", "YM_Z")],                # 1-digit year
    [("YMZ20261", "YM_Z")],            # 5-digit year
    [("Z26", "YM_Z")],                 # empty master prefix
    [("26", "YM_Z")],                  # no month code
    [("YM", "YM_Z")],                  # no month+year
    [("YMZ26", "YM_Z"), ("ESH27", "ES_H")],       # different families
    [("YMH27", "YM_H27"), ("YMZ26", "YM_Z5")],    # slave year width
])
def test_infer_rejects_invalid_input(examples):
    with pytest.raises(SymbolAssistError):
        infer_symbol_rule(examples)


def test_validate_rule_disables_the_same_name_fallback():
    # ^NOPE$ does not match YMZ26; with the fallback enabled the mapper would
    # return the master symbol itself and this test would wrongly pass.
    with pytest.raises(SymbolAssistError):
        validate_rule(SymbolRule(r"^NOPE$", "YMZ26"), [("YMZ26", "YMZ26")])


def test_conflicts_detect_an_exact_tier_winner():
    rule = infer_symbol_rule([("YMZ26", "YM_Z")])
    conflicts = find_conflicts(rule, [("YMZ26", "YM_Z")],
                               [("YMZ26", "OLD")])
    assert conflicts == [RuleConflict("YMZ26", "YM_Z", "OLD", 0, True)]


def test_conflicts_detect_a_regex_winner_and_its_index():
    rule = infer_symbol_rule([("YMZ26", "YM_Z")])
    rows = [("EURUSD", "EURUSD"),
            (r"^(YM)([FGHJKMNQUVXZ])(\d{2})$", "OLD_$2")]
    conflicts = find_conflicts(rule, [("YMZ26", "YM_Z")], rows)
    assert [(c.row_index, c.exact_tier, c.actual_slave) for c in conflicts] == [
        (1, False, "OLD_Z")]


def test_conflicts_are_empty_when_no_existing_row_wins():
    rule = infer_symbol_rule([("YMZ26", "YM_Z")])
    assert find_conflicts(rule, [("YMZ26", "YM_Z")],
                          [("EURUSD", "EURUSD")]) == []


def test_conflicts_skip_incomplete_rows_but_keep_table_indices():
    rule = infer_symbol_rule([("YMZ26", "YM_Z")])
    rows = [("", ""), ("YMZ26", "OLD"), ("", "")]
    conflicts = find_conflicts(rule, [("YMZ26", "YM_Z")], rows)
    assert conflicts[0].row_index == 1
