# Fixed-Lot Master-Base-Lot Down-Scaling

Date: 2026-11-17
Status: Approved (design)
Size: Small change

## Intent

In `fixed_lot` mode the slave opens one configured lot for every trade and
ignores the master's volume entirely. The user wants the fixed lot to keep
tracking the master's lot the same way `balance_step` + Master base lot
already does — down-only: a master trade smaller than the base lot scales the
slave's fixed lot proportionally down; a master trade at or above the base lot
opens the full fixed lot.

The only intended difference between `balance_step` and `fixed_lot` stays
this: the slave lot is not derived from the slave's balance, but is constant
(until the master-base down-scaling applies).

Example: fixed lot 0.1, master base lot 10, master opens 5.0 → slave opens
0.05 (snapped to the symbol's lot step). Master opens 10 or more → 0.1.

## Goals

- `fixed_lot` honours `master_base_lot` with the same down-only formula as
  `balance_step`.
- No new configuration field and no new sizing mode.
- The Master base lot field is editable in Fixed Lot mode in the GUI.
- Existing configurations (`master_base_lot = 0.0`) behave exactly as today.

## Non-goals

- Symmetric scaling (master lot > base does not scale the slave up).
- Changing `copy_master`.
- A separate base-lot field for fixed lot.
- Any worker/IPC change (sizing stays manager-side; the worker sees only the
  computed `CommandMsg.volume`).

## Anti-goals

- Changing the shared snap/clamp/cap/round tail or the below-minimum
  behaviour (a scaled lot under `volume_min` opens the symbol minimum).
- Changing partial-close semantics (already proportional to the slave's open
  volume in every mode).

## Approaches considered

1. **Reuse `master_base_lot` in the `fixed_lot` branch** (chosen). No new
   field, no store migration, GUI only needs a visibility change, and the
   semantics match `balance_step` exactly.
2. **A separate `fixed_base_lot` field.** Duplicates an existing knob and adds
   store/GUI/editor surface for no behavioural gain.
3. **A fourth mode `fixed_lot_scaled`.** More modes to explain and maintain;
   `master_base_lot = 0.0` already means "off", so a mode is unnecessary.

## Design

### `manager/engine/transform.py` — `calculate_slave_lot`

Replace the `fixed_lot` branch with:

```python
if mode == SIZING_FIXED_LOT:
    if fixed_lot <= 0.0:
        return 0.0
    raw = fixed_lot
    if master_base_lot > 0.0 and master_volume < master_base_lot:
        raw *= master_volume / master_base_lot
    return _snap_clamp(raw, lot_step, min_lot, max_lot)
```

- Down-only by the same guard as `balance_step` (`master_volume <
  master_base_lot`).
- `master_base_lot <= 0.0` disables scaling → `raw = fixed_lot` (today's
  behaviour, backward compatible).
- The shared tail is unchanged: snap down to the lot step, clamp up to
  `min_lot`, cap at `max_lot`, round to the lot-step digit count.

### `manager/gui/profile_editor.py` — `_update_sizing_visibility`

Show the Master base lot field in both balance-step and fixed-lot modes:

```python
show_row(self.master_base_lot, is_balance or is_fixed)
```

`copy_master` continues to hide it. Widgets stay constructed in every mode.

### Tests

- `manager/tests/test_transform.py`
  - Existing `test_calculate_slave_lot_fixed_lot_uses_fixed_lot` (base 0)
    stays green.
  - Fixed 0.1 / base 10 / master 5 → 0.05.
  - Fixed 0.1 / base 10 / master 10 (or larger) → 0.1 (no scaling up).
  - Fixed 0.1 / base 10 / master 0.5 → raw 0.005 → clamped up to `min_lot`.
  - Fixed 0.1 / base 10 / invalid `fixed_lot = 0` → 0.0.
- `manager/tests/test_slave_editor.py`
  - Fixed Lot mode now shows `master_base_lot` (update the existing
    visibility assertion).
  - Balance Step and Copy Master visibility unchanged.
- `README.md` — extend the lot-sizing bullet: fixed lot is constant and
  follows the master-base-lot down-scaling like balance step.

## Risks / notes

- Same asymmetry as `balance_step`: deliberately down-only.
- A scaled fixed lot below the symbol minimum opens `volume_min`, consistent
  with every existing sizing path.
- Live edits to `fixed_lot` / `master_base_lot` already flow through
  `update_slave_config`; they apply to the next NEW trade.
