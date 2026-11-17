# Symbol Map Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an example-based Symbol Assistant to the shared symbol-map widget: the user enters 1–2 master/slave example pairs, the assistant infers a regex rule and slave template, previews it, and inserts the pair into the map — with shadowing warnings and the existing one-trade-per-master-trade invariant pinned by tests.

**Architecture:** A pure inference module (`manager/engine/symbol_assist.py`) derives the rule and reports conflicts against existing rows; a Qt dialog (`manager/gui/symbol_assistant.py`) drives it with examples, editable rule fields, preview, and apply/insert-above choices; `CopySettingsWidget` gains an Assistant button and applies the returned `AssistantResult` at the requested row.

**Tech Stack:** Python 3.12, PySide6 (Qt), pytest, the existing `SymbolMapper`.

**Spec:** `docs/superpowers/specs/2026-11-17-symbol-map-assistant-design.md`

## Global Constraints

- Tests run with the app venv interpreter: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest ...` (pytest is not on PATH). GUI tests use the offscreen `qapp` fixture.
- Work directly on `main` (standing user instruction: no worktrees, no feature branches).
- **No production-engine changes.** `SymbolMapper`, `parse_symbol_map`, `_expand_template`, `derive_command`, and the worker are untouched; Task 4 only adds regression tests around them.
- Generated master patterns and slave templates must contain neither `,` nor `=` (the map CSV is `master=slave` joined by commas).
- Generated patterns always use a fixed-width year group `(\d{n})` with `n` ∈ {2, 4}; never `\d{2,4}` (its comma would break the CSV).
- Validation and preview construct `SymbolMapper(..., lambda s: False)` so the same-name fallback cannot make an invalid rule look valid.
- All new user-facing strings are English; the button label is exactly `Assistant…` (with the ellipsis).
- The Assistant button is part of `set_editable`, so it is disabled whenever the symbol table is locked.
- The assistant only changes the table after the user accepts; existing rows are never modified on open or on inference.
- Inference requires a 2–4 digit year in every master example; undated contracts are rejected with a clear error and remain manually editable.

## Review Focus

1. **A 5+-digit year run** must be rejected, not silently split into a 4-digit year plus a digit-leading suffix (pinned in Task 1).
2. **An exact-tier row always wins** over a new regex row regardless of order; the warning must say ordering cannot fix that (Task 1 `find_conflicts` + Task 2 dialog).
3. **Insert-above placement** must use the lowest conflicting row index so the new rule wins for every conflicting example (Task 2 + Task 3).
4. **Validation must disable the same-name fallback** so a non-matching rule is rejected instead of resolving to the master symbol (Task 1).
5. **One master NEW → exactly one OPEN per slave** even when several map rows match (Task 4).

---

## File Structure

- `manager/engine/symbol_assist.py` — pure inference, validation, conflict detection. (Task 1)
- `manager/tests/test_symbol_assist.py` — unit tests. (Task 1)
- `manager/gui/symbol_assistant.py` — `AssistantResult`, `SymbolAssistant` dialog. (Task 2)
- `manager/tests/test_symbol_assistant.py` — dialog tests. (Task 2)
- `manager/gui/profile_editor.py` — Assistant button, `symbol_map_pairs()`, result application, `set_editable`. (Task 3)
- `manager/tests/test_profile_editor.py`, `manager/tests/test_slave_editor.py` — widget tests. (Task 3)
- `manager/tests/test_transform.py`, `manager/tests/test_copy_loop.py` — invariant regression tests. (Task 4)
- `README.md` — assistant + invariant documentation. (Task 5)

---

### Task 1: Pure inference and conflict detection

**Files:**
- Create: `manager/engine/symbol_assist.py`
- Test: `manager/tests/test_symbol_assist.py`

**Interfaces:**
- Consumes: `manager.engine.transform.SymbolMapper`.
- Produces:
  - `MONTH_CODES: str = "FGHJKMNQUVXZ"`
  - `@dataclass(frozen=True) class SymbolRule: master_pattern: str; slave_template: str`
  - `class SymbolAssistError(ValueError)`
  - `@dataclass(frozen=True) class RuleConflict: master_example: str; expected_slave: str; actual_slave: str; row_index: int; exact_tier: bool`
  - `infer_symbol_rule(examples: list[tuple[str, str]]) -> SymbolRule`
  - `validate_rule(rule: SymbolRule, examples: list[tuple[str, str]]) -> None`
  - `find_conflicts(rule: SymbolRule, examples: list[tuple[str, str]], existing_rows: list[tuple[str, str]]) -> list[RuleConflict]`

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_symbol_assist.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_symbol_assist.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'manager.engine.symbol_assist'`.

- [ ] **Step 3: Implement `manager/engine/symbol_assist.py`**

Implement the algorithm from the spec:

- Constants/dataclasses as in the Interfaces block.
- `infer_symbol_rule`:
  - Strip each pair; require at least one pair with both sides non-empty.
  - Master: find the rightmost index `i` where `master[i] in MONTH_CODES` and `master[i+1].isdigit()`; the entire digit run after `i` must be 2–4 digits. master prefix = `master[:i]` (non-empty), month = `master[i]`, year = the run, suffix = the text after the run.
  - Require all masters to agree on prefix, year width and suffix.
  - Slave: month letter = the last index with `slave[i] in MONTH_CODES` (raise if none). Slave prefix = before it, tail = after it. If the tail starts with a digit run: its width must equal the master year width → slave year + rest suffix; otherwise raise. With no leading digits: no slave year, entire tail is suffix. Require all slaves to agree on prefix, suffix and year presence/width.
  - Build `master_pattern = "^(" + re.escape(prefix) + ")([" + MONTH_CODES + "])(\\d{" + str(year_width) + "})" + re.escape(master_suffix) + "$"`.
  - Build `slave_template = _escape_template(slave_prefix) + "$2" + ("$3" if slave_year else "") + _escape_template(slave_suffix)`, where `_escape_template` replaces every literal `$` with `$$`.
  - Raise `SymbolAssistError` if either field contains `,` or `=`.
  - Call `validate_rule(rule, examples)` before returning.
- `validate_rule`: build `SymbolMapper(f"{rule.master_pattern}={rule.slave_template}", lambda s: False)` and require each example master to resolve to its slave; otherwise raise `SymbolAssistError` naming the example.
- `find_conflicts`:
  - `complete = [(i, m, s) for i, (m, s) in enumerate(existing_rows) if m and s]`.
  - Build the new rule's mapper (fallback disabled) for the expected slave of each example.
  - For each example, walk `complete` in order; for each row build a one-row `SymbolMapper(f"{m}={s}", lambda s: False)` and resolve the example. The first non-empty result is the winner; record `(row_index, actual, exact_tier=(example == m))`.
  - Emit a `RuleConflict` when the winner's result differs from the expected slave.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_symbol_assist.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/engine/symbol_assist.py manager/tests/test_symbol_assist.py
git commit -m "feat(engine): add example-based symbol rule inference"
```

---

### Task 2: Symbol Assistant dialog

**Files:**
- Create: `manager/gui/symbol_assistant.py`
- Test: `manager/tests/test_symbol_assistant.py`

**Interfaces:**
- Consumes: `SymbolRule`, `SymbolAssistError`, `infer_symbol_rule`, `validate_rule`, `find_conflicts`, `RuleConflict` (Task 1); `SymbolMapper` for preview.
- Produces:
  - `@dataclass(frozen=True) class AssistantResult: master_pattern: str; slave_template: str; insert_before: int | None = None`
  - `class SymbolAssistant(QDialog)`
  - `SymbolAssistant(existing_rows: list[tuple[str, str]] | None = None, parent=None)`
  - `SymbolAssistant.rule() -> AssistantResult | None` (None unless accepted)
  - Widgets used by tests: `examples_table` (QTableWidget, 2 columns), `add_example_button`, `remove_example_button`, `master_pattern_edit`, `slave_template_edit`, `rederive_button`, `preview_input`, `preview_label`, `conflict_label`, `insert_above_button`, `apply_anyway_button`, `apply_button`, `cancel_button`.
  - Helper `_add_example_row(master: str = "", slave: str = "") -> None`.

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_symbol_assistant.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_symbol_assistant.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'manager.gui.symbol_assistant'`.

- [ ] **Step 3: Implement `manager/gui/symbol_assistant.py`**

Build the dialog with the layout from the spec (English strings):
- Examples table (2 columns: `Master example`, `Slave example`), add/remove buttons, one empty row at start.
- Rule row: `master_pattern_edit`, `slave_template_edit`, `rederive_button`.
- Preview row: `preview_input` + `preview_label`.
- `error_label` and `conflict_label` (hidden until needed), `insert_above_button` and `apply_anyway_button` (hidden until a conflict), `apply_button`, `cancel_button`.
- On example cell change: if `self._manual` is False, call `_rederive()`; update the preview.
- On a rule field edit: set `self._manual = True`; update the preview.
- `_rederive()`: `self._manual = False`; collect complete example pairs; call `infer_symbol_rule`; on `SymbolAssistError` show it in `error_label` and leave the fields; on success fill the fields and clear the error.
- `_update_preview()`: read the test symbol (default: first example's master), build a no-fallback `SymbolMapper`; show `f"{master} → {result}"` or the error.
- `_on_apply()`: validate: complete examples ≥ 1; the pattern compiles; no `,`/`=` in either field; `validate_rule` passes. On failure show `error_label`, keep the dialog open. On success compute `find_conflicts`; if empty, `_finish(None)`; otherwise fill `conflict_label` (naming the winning row and the slave it produces; for an exact-tier conflict the text must contain the word `exact` and say ordering cannot fix it), show the two choice buttons, and enable `insert_above_button` only when a non-exact conflict exists.
- `insert_above_button` → `_finish(min(c.row_index for c in conflicts if not c.exact_tier))`; `apply_anyway_button` → `_finish(None)`.
- `_finish(insert_before)`: store `AssistantResult(...)` in `self._result`, call `self.accept()`.
- `cancel_button` → `self.reject()`.
- `rule()` returns `self._result`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_symbol_assistant.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/symbol_assistant.py manager/tests/test_symbol_assistant.py
git commit -m "feat(gui): add the symbol assistant dialog"
```

---

### Task 3: Symbol-map widget integration

**Files:**
- Modify: `manager/gui/profile_editor.py` (`CopySettingsWidget`)
- Test: `manager/tests/test_profile_editor.py`, `manager/tests/test_slave_editor.py`

**Interfaces:**
- Consumes: `SymbolAssistant`, `AssistantResult` (Task 2).
- Produces:
  - `CopySettingsWidget.assistant_button` (QPushButton, text `Assistant…`)
  - `CopySettingsWidget.symbol_map_pairs() -> list[tuple[str, str]]` — every table row in order, raw cell text (`""` for empty cells).
  - `CopySettingsWidget._apply_assistant_result(result: AssistantResult) -> None`
  - `set_editable` includes `assistant_button`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_profile_editor.py`:

```python
def test_copy_settings_widget_has_assistant_button(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    assert w.assistant_button.text() == "Assistant…"


def test_copy_settings_widget_assistant_button_follows_set_editable(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    w.set_editable(False)
    assert not w.assistant_button.isEnabled()
    w.set_editable(True)
    assert w.assistant_button.isEnabled()


def test_symbol_map_pairs_preserves_order(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    w._add_sym_row()
    w._add_sym_row()
    w.symbol_table.item(0, 0).setText("EURUSD")
    w.symbol_table.item(0, 1).setText("EURUSD")
    w.symbol_table.item(1, 0).setText("YMZ26")
    w.symbol_table.item(1, 1).setText("YM_Z")
    assert w.symbol_map_pairs() == [("EURUSD", "EURUSD"), ("YMZ26", "YM_Z")]


def test_apply_assistant_result_inserts_above(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    from manager.gui.symbol_assistant import AssistantResult
    w = CopySettingsWidget()
    w._add_sym_row()
    w.symbol_table.item(0, 0).setText("EURUSD")
    w.symbol_table.item(0, 1).setText("EURUSD")
    w._apply_assistant_result(AssistantResult("PATTERN", "TEMPLATE", 0))
    assert w.symbol_map_pairs() == [("PATTERN", "TEMPLATE"),
                                    ("EURUSD", "EURUSD")]
    assert w.values()["symbol_map_csv"].startswith("PATTERN=TEMPLATE,")


def test_apply_assistant_result_replaces_first_and_removes_duplicates(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    from manager.gui.symbol_assistant import AssistantResult
    w = CopySettingsWidget()
    for master in ("PATTERN", "EURUSD", "PATTERN"):
        w._add_sym_row()
        r = w.symbol_table.rowCount() - 1
        w.symbol_table.item(r, 0).setText(master)
        w.symbol_table.item(r, 1).setText("OLD")
    w._apply_assistant_result(AssistantResult("PATTERN", "NEW", None))
    assert w.symbol_map_pairs() == [("PATTERN", "NEW"), ("EURUSD", "OLD")]


def test_apply_assistant_result_appends_when_new(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    from manager.gui.symbol_assistant import AssistantResult
    w = CopySettingsWidget()
    w._apply_assistant_result(AssistantResult("PATTERN", "TEMPLATE", None))
    assert w.symbol_map_pairs() == [("PATTERN", "TEMPLATE")]


def test_assistant_button_applies_the_dialog_result(qapp, monkeypatch):
    import manager.gui.profile_editor as profile_editor
    from manager.gui.symbol_assistant import AssistantResult
    from PySide6.QtWidgets import QDialog

    class FakeAssistant:
        def __init__(self, existing_rows=None, parent=None):
            self.existing_rows = existing_rows
        def exec(self):
            return QDialog.DialogCode.Accepted
        def rule(self):
            return AssistantResult("PATTERN", "TEMPLATE", None)

    monkeypatch.setattr(profile_editor, "SymbolAssistant", FakeAssistant)
    w = profile_editor.CopySettingsWidget()
    w.assistant_button.click()
    assert w.symbol_map_pairs() == [("PATTERN", "TEMPLATE")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_profile_editor.py -q`

Expected: FAIL — `CopySettingsWidget` has no `assistant_button`.

- [ ] **Step 3: Implement the widget changes**

In `manager/gui/profile_editor.py`:
- Import `SymbolAssistant`, `AssistantResult` from `manager.gui.symbol_assistant` **at module level** (the integration test monkeypatches `profile_editor.SymbolAssistant`).
- In `CopySettingsWidget.__init__`, create `self.assistant_button = QPushButton("Assistant…")`; add it to the symbol add/remove button row; connect `clicked` to `_open_assistant`.
- `symbol_map_pairs()`: return `[(table.item(r,0).text() if item else "", table.item(r,1).text() if item else "") for r in range(rowCount())]`.
- `_open_assistant()`: `dlg = SymbolAssistant(existing_rows=self.symbol_map_pairs(), parent=self)`; if `dlg.exec() != QDialog.DialogCode.Accepted`: return; result = `dlg.rule()`; if result: `_apply_assistant_result(result)`.
- `_apply_assistant_result(result)`:
  - If `result.insert_before is not None`: `insertRow(insert_before)` and set the two cells.
  - Else if a complete row with master == `result.master_pattern` exists: set its slave cell; collect the indices of all other complete rows with the same master and remove them from the highest index down.
  - Else append a row.
  - Select the applied row.
- `set_editable`: add `self.assistant_button` to the widget tuple.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_profile_editor.py manager/tests/test_slave_editor.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/profile_editor.py manager/tests/test_profile_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): wire the symbol assistant into the map table"
```

---

### Task 4: Single-trade invariant regression tests

**Files:**
- Test: `manager/tests/test_transform.py`, `manager/tests/test_copy_loop.py`

**Interfaces:**
- Consumes: the existing `SymbolMapper`, `SlaveConfig`, `CopyEngine`; no production changes.
- Produces: regression tests only.

- [ ] **Step 1: Write the failing-or-passing invariant tests**

Append to `manager/tests/test_transform.py`:

```python
def test_overlapping_rows_resolve_to_exactly_one_slave():
    first_broad = SymbolMapper(
        "YM.*=YM,^(YM)([FGHJKMNQUVXZ])(\\d{2})$=YM_$2", lambda s: False)
    assert first_broad.resolve("YMZ26") == "YM"
    first_specific = SymbolMapper(
        "^(YM)([FGHJKMNQUVXZ])(\\d{2})$=YM_$2,YM.*=YM", lambda s: False)
    assert first_specific.resolve("YMZ26") == "YM_Z"


def test_exact_row_beats_regex_rows_regardless_of_order():
    m = SymbolMapper(
        "^(YM)([FGHJKMNQUVXZ])(\\d{2})$=YM_$2,YMZ26=EXACT", lambda s: False)
    assert m.resolve("YMZ26") == "EXACT"
```

Append to `manager/tests/test_copy_loop.py`:

```python
def test_one_master_new_emits_one_open_with_overlapping_rows():
    cfg = _cfg(symbol_map="YM.*=YM,^(YM)([FGHJKMNQUVXZ])(\\d{2})$=YM_$2")
    eng = _engine(slaves=(cfg,), infos={"s1": {"YM": SI}})
    cmds = eng.ingest_snapshot(
        _snap([_pos(1, volume=0.5, symbol="YMZ26")]), now=NOW)["s1"]
    assert len(cmds) == 1
    assert cmds[0].symbol == "YM"


def test_exact_row_wins_for_a_master_new():
    cfg = _cfg(
        symbol_map="^(YM)([FGHJKMNQUVXZ])(\\d{2})$=YM_$2,YMZ26=EXACT")
    eng = _engine(slaves=(cfg,), infos={"s1": {"EXACT": SI}})
    cmds = eng.ingest_snapshot(
        _snap([_pos(1, volume=0.5, symbol="YMZ26")]), now=NOW)["s1"]
    assert len(cmds) == 1
    assert cmds[0].symbol == "EXACT"
```

Note: `test_transform.py` must import `SymbolMapper`; add it to the existing import block if absent.

- [ ] **Step 2: Run tests**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py manager/tests/test_copy_loop.py -q`

Expected: PASS. These tests pin current behaviour; they should pass immediately. If any fails, stop and investigate — it means the invariant is already broken.

- [ ] **Step 3: Run the split-order tests**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_mt5_worker.py -q`

Expected: PASS (oversized volume still opens several orders on the one resolved symbol).

- [ ] **Step 4: Commit**

```bash
git add manager/tests/test_transform.py manager/tests/test_copy_loop.py
git commit -m "test(engine): pin one OPEN per master NEW with overlapping rows"
```

---

### Task 5: README

**Files:**
- Modify: `README.md` (symbol-map section and the copy-profile usage note)

**Interfaces:**
- Consumes: nothing (documentation only).

- [ ] **Step 1: Document the assistant and the invariant**

In the symbol-mapping section, add:

```markdown
- **Symbol Assistant** — next to the map rows, **Assistant…** takes one or more
  example pairs (master → slave) and generates the regex rule automatically.
  Example: `YMZ26 → YM_Z` and `YMH27 → YM_H` produces
  `^(YM)([FGHJKMNQUVXZ])(\d{2})$ → YM_$2`, which covers every YM contract
  across rollovers. The rule is shown in editable fields with a live preview;
  the assistant warns when an existing exact row (which always wins) or an
  earlier regex row would keep the new rule from taking effect.
- **One trade per master trade per slave** — a master trade resolves to exactly
  one map row, so it opens at most one position per slave. The only exception
  is the existing oversized-volume split, which opens several orders on that
  same slave symbol.
```

- [ ] **Step 2: Verify the docs**

Run: `git diff -- README.md`

Expected: only the intended additions.

- [ ] **Step 3: Run the full suite**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document the symbol assistant and the one-trade rule"
```
