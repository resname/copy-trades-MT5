# Fixed-Lot Master-Base-Lot Down-Scaling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fixed_lot` sizing honour `master_base_lot` with the same down-only scaling as `balance_step`, so a master trade smaller than the base opens a proportionally smaller fixed lot.

**Architecture:** One branch change in `calculate_slave_lot` (`manager/engine/transform.py`) — the fixed-lot raw value is multiplied by `master_volume / master_base_lot` when the master trade is below the base, then passed through the existing shared `_snap_clamp` tail. The GUI shows the Master base lot field in Fixed Lot mode; no new config field, no store migration, no worker/IPC change.

**Tech Stack:** Python 3.11+, PySide6 (GUI), pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-fixed-lot-base-scaling-design.md`

## Global Constraints

- Tests run with the app venv interpreter: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest ...` (pytest is not on PATH; the system Python lacks pytest/PySide6). GUI tests use the `qapp` fixture.
- Work directly on `main` (standing user instruction: no worktrees, no feature branches).
- Scaling is **down-only**: a master volume at or above `master_base_lot` opens the full `fixed_lot`; it is never scaled up.
- `master_base_lot <= 0.0` disables scaling entirely (backward compatible: existing configs keep today's fixed-lot behaviour).
- `fixed_lot <= 0.0` still returns `0.0` (invalid config; the engine skips the trade).
- The shared tail `_snap_clamp` is unchanged: snap down to the lot step, clamp up to `min_lot`, cap at `max_lot`, round to the lot-step digit count.
- No new configuration field, no new sizing mode, no changes to `copy_master`, no worker/IPC changes.

---

## Review Focus

1. **Master volume exactly equal to the base lot** — must NOT scale (the guard is strict `<`), so a trade at the base opens the full fixed lot.
2. **Scaled fixed lot below the symbol minimum** — must open `volume_min`, not skip the trade.
3. **`master_base_lot` zero or negative with fixed lot** — scaling disabled; the full fixed lot opens.
4. **`lot_step <= 0`** — must return `0.0` (engine skips) rather than an unsnapped lot.
5. **GUI field visibility** — Master base lot must be visible in both balance-step and fixed-lot modes, and hidden in copy-master mode.

Each of these is pinned by a test in the task that owns the code (Task 1 for 1–4, Task 2 for 5).

---

## File Structure

- `manager/engine/transform.py` — fixed-lot branch gains the down-only base scaling. (Task 1)
- `manager/tests/test_transform.py` — unit tests for fixed-lot scaling. (Task 1)
- `manager/gui/profile_editor.py` — Master base lot visible in fixed-lot mode. (Task 2)
- `manager/tests/test_slave_editor.py` — visibility test update. (Task 2)
- `README.md` — describe fixed-lot base scaling. (Task 3)

---

### Task 1: Fixed-lot down-scaling in the sizer

**Files:**
- Modify: `manager/engine/transform.py:152-155` (the `SIZING_FIXED_LOT` branch)
- Test: `manager/tests/test_transform.py:215-222` (extend the fixed-lot tests)

**Interfaces:**
- Consumes: `calculate_slave_lot(mode: str, master_volume: float, balance: float, step_amount: float, step_size: float, master_base_lot: float, fixed_lot: float, max_lot: float, lot_step: float, min_lot: float) -> float` (existing signature, unchanged) and `_snap_clamp(lots, lot_step, min_lot, max_lot) -> float`.
- Produces: same signature; `fixed_lot` now scales down when `master_base_lot > 0.0 and master_volume < master_base_lot`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_transform.py` after `test_calculate_slave_lot_fixed_lot_invalid_returns_zero` (line 222):

```python
def test_calculate_slave_lot_fixed_lot_scales_down_below_base():
    # fixed 0.1, base 10, master 5 -> 0.1 * (5/10) = 0.05
    assert calculate_slave_lot(SIZING_FIXED_LOT, 5.0, 1000, 100, 0.01,
                               10.0, 0.1, 10, 0.01, 0.01) == 0.05


def test_calculate_slave_lot_fixed_lot_no_scaling_at_base():
    # master 10 == base 10 -> strict '<' guard -> full fixed lot
    assert calculate_slave_lot(SIZING_FIXED_LOT, 10.0, 1000, 100, 0.01,
                               10.0, 0.1, 10, 0.01, 0.01) == 0.1


def test_calculate_slave_lot_fixed_lot_no_scaling_above_base():
    # master 20 > base 10 -> down-only -> full fixed lot
    assert calculate_slave_lot(SIZING_FIXED_LOT, 20.0, 1000, 100, 0.01,
                               10.0, 0.1, 10, 0.01, 0.01) == 0.1


def test_calculate_slave_lot_fixed_lot_scaled_below_min_opens_min():
    # fixed 0.1, base 10, master 0.5 -> raw 0.005 -> snaps to 0.0 -> min 0.01
    assert calculate_slave_lot(SIZING_FIXED_LOT, 0.5, 1000, 100, 0.01,
                               10.0, 0.1, 10, 0.01, 0.01) == 0.01


def test_calculate_slave_lot_fixed_lot_negative_base_disables_scaling():
    # master_base_lot <= 0 -> disabled -> full fixed lot (master 0.5 ignored)
    assert calculate_slave_lot(SIZING_FIXED_LOT, 0.5, 1000, 100, 0.01,
                               -10.0, 0.1, 10, 0.01, 0.01) == 0.1


def test_calculate_slave_lot_fixed_lot_with_base_capped_at_max_lot():
    # fixed 10, base 10, master 5 -> raw 5 -> capped at max_lot 2
    assert calculate_slave_lot(SIZING_FIXED_LOT, 5.0, 1000, 100, 0.01,
                               10.0, 10.0, 2.0, 0.01, 0.01) == 2.0


def test_calculate_slave_lot_fixed_lot_with_base_invalid_lot_step_returns_zero():
    assert calculate_slave_lot(SIZING_FIXED_LOT, 5.0, 1000, 100, 0.01,
                               10.0, 0.1, 10, 0.0, 0.01) == 0.0


def test_calculate_slave_lot_fixed_lot_zero_with_base_returns_zero():
    # invalid fixed lot is skipped before any scaling is considered
    assert calculate_slave_lot(SIZING_FIXED_LOT, 5.0, 1000, 100, 0.01,
                               10.0, 0.0, 10, 0.01, 0.01) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py -q`

Expected: the new `fixed_lot_scales_down_below_base` and `fixed_lot_scaled_below_min_opens_min` tests FAIL (they currently return 0.1); the others may already pass because they pin unchanged behaviour.

- [ ] **Step 3: Implement the branch change**

In `manager/engine/transform.py`, replace the `SIZING_FIXED_LOT` branch (lines 152-155) with:

```python
    if mode == SIZING_FIXED_LOT:
        if fixed_lot <= 0.0:
            return 0.0
        raw = fixed_lot
        if master_base_lot > 0.0 and master_volume < master_base_lot:
            raw *= master_volume / master_base_lot
        return _snap_clamp(raw, lot_step, min_lot, max_lot)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py -q`

Expected: all tests PASS, including the pre-existing fixed-lot tests (base `0.0` keeps the old behaviour).

- [ ] **Step 5: Run the engine-related suite**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py manager/tests/test_copy_loop.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/engine/transform.py manager/tests/test_transform.py
git commit -m "feat(engine): scale fixed lot down below the master base lot"
```

---

### Task 2: Show Master base lot in Fixed Lot mode

**Files:**
- Modify: `manager/gui/profile_editor.py:139-144` (`_update_sizing_visibility`)
- Test: `manager/tests/test_slave_editor.py:204-213` (fixed-lot visibility test)

**Interfaces:**
- Consumes: `CopySettingsWidget.master_base_lot` (QLineEdit), `self._sizing.labelForField(widget)`, `sizing_mode.currentData()` returning `"balance_step" | "copy_master" | "fixed_lot"`.
- Produces: no API change; visibility only.

- [ ] **Step 1: Update the failing GUI test**

In `manager/tests/test_slave_editor.py`, in `test_slave_editor_fixed_lot_shows_fixed_field_hides_step` (line 204), change the last assertion from:

```python
    assert dlg.master_base_lot.isHidden()
```

to:

```python
    assert not dlg.master_base_lot.isHidden()  # base-lot scaling applies to fixed lot
```

Also confirm the copy-master test (`test_slave_editor_copy_master_hides_step_and_base_fields`, line 192) still asserts `dlg.master_base_lot.isHidden()` — leave it unchanged.

- [ ] **Step 2: Run the test to verify it fails**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_slave_editor.py::test_slave_editor_fixed_lot_shows_fixed_field_hides_step -q`

Expected: FAIL — `master_base_lot` is currently hidden in fixed-lot mode.

- [ ] **Step 3: Implement the visibility change**

In `manager/gui/profile_editor.py`, change the line in `_update_sizing_visibility`:

```python
        show_row(self.master_base_lot, is_balance)
```

to:

```python
        show_row(self.master_base_lot, is_balance or is_fixed)
```

- [ ] **Step 4: Run the GUI tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_slave_editor.py -q`

Expected: PASS, including the balance-step and copy-master visibility tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/profile_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): show master base lot in fixed-lot mode"
```

---

### Task 3: Document the fixed-lot behaviour in the README

**Files:**
- Modify: `README.md:85-93` (the lot-sizing feature bullet)

**Interfaces:**
- Consumes: nothing (documentation only).

- [ ] **Step 1: Update the README bullet**

Replace the Fixed lot line in the lot-sizing list with:

```markdown
  - **Fixed lot** — the slave opens one configured lot size for every trade.
    Optionally set a **Master base lot size** (like balance step): when a master
    trade is *smaller* than the base, the fixed lot is scaled down
    proportionally (fixed 0.1 with base 10, master opens 5 → 0.05); trades at or
    above the base are not scaled up.
```

- [ ] **Step 2: Verify the README renders the intended text**

Run: `git diff -- README.md`

Expected: exactly the Fixed lot bullet changed, no other lines touched.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): describe fixed-lot master base scaling"
```
