# Slave Lot-Sizing Modes + Master Base Lot Scaling

Date: 2026-08-04
Status: Approved (design)

## Problem

The copier's slave lot sizing today has a single strategy: a **balance-step**
sizer (`manager/engine/transform.py:49`, ported from `CLotSizer::CalculateLots`)
that computes the slave lot purely from the **slave's balance** —
`floor(balance / step_amount) * step_size`, snapped to the symbol's volume step
and clamped to min/max. The **master's lot is ignored** for sizing (it appears
only in the trade comment and in partial-close fractions).

The user wants multiple risk-management modes for each slave, and a way to make
the slave's size **track the master's lot** while still resting on the
lot-step grid:

1. **Keep** the existing balance-step mode (option 1).
2. **Add a copy-master mode** — the slave mirrors the master's lot per trade.
3. **Add a fixed-lot mode** — the slave opens one configured lot size for every
   trade.
4. **Enhance the balance-step mode** with an optional **Master base lot size**:
   the slave's normal balance-step size corresponds to the master's *usual* lot
   (e.g. 0.1). When a specific master trade is **smaller** than that base, the
   slave opens a **proportionally smaller** position, still snapped to lot
   steps. Larger master trades do **not** scale the slave up (down-only).

## Goal

Three selectable per-slave lot-sizing modes, engine-side (no worker/IPC
changes), backward-compatible with existing `settings.json` files, with the
balance-step mode gaining an optional master-base-lot down-scaling feature.

## Signal & inputs available

- Master trade lot: `pos.volume` — already in scope at the sizing call site
  (`manager/engine/copy_loop.py:55`, `derive_command` for `event.kind == "NEW"`).
- Slave balance: `state.balance` (from the slave's `StatusMsg`).
- Symbol constraints: `SymbolInfo.volume_step`, `volume_min`, `volume_max`
  (`manager/engine/models.py:56-67`).
- Sizing runs in the manager process; the worker only receives the
  already-computed `CommandMsg.volume`. The worker's partial-close logic is
  already master-volume-proportional (`fraction = new_master_volume /
  master_open_volume` applied to `slave_open_volume`), so it is correct
  regardless of how the slave's open volume was determined.

## Modes & sizing math

A `SizingMode` is a string enum: `"balance_step"` | `"copy_master"` |
`"fixed_lot"`.

For each NEW trade the engine computes a **raw lot** depending on the mode,
then runs it through a **single shared tail** (snap / clamp / cap / round).

### Raw lot per mode

Let `raw_balance = floor(slave_balance / step_amount) * step_size`
(the unclamped balance-step value; `0.0` when `step_amount <= 0` or
`step_size <= 0`).

| Mode | Condition | Raw lot |
|---|---|---|
| `balance_step` | `master_base_lot <= 0` (disabled) | `raw_balance` (current behavior) |
| `balance_step` | `master_base_lot > 0` and `master_lot < master_base_lot` | `raw_balance * (master_lot / master_base_lot)` (scale **down** only) |
| `balance_step` | `master_base_lot > 0` and `master_lot >= master_base_lot` | `raw_balance` (no scaling up) |
| `copy_master` | — | `master_lot` |
| `fixed_lot` | — | `fixed_lot` |

### Shared tail (identical for every mode)

1. Snap **down** to the symbol's volume step:
   `lots = floor(raw / volume_step) * volume_step`.
2. If `volume_step <= 0` → return `0.0` (cannot snap; engine skips the trade).
3. Clamp **up** to `volume_min`: `lots = max(lots, volume_min)`.
   (A lot that undershoots the minimum — e.g. a heavily down-scaled trade, or a
   tiny master lot in copy-master mode — becomes the symbol's minimum rather
   than being skipped. This is the user's chosen behavior and matches the
   existing sizer's `max(lots, min_lot)` clamp.)
4. Cap at the per-slave `max_lot` and the symbol's `volume_max`:
   `lots = min(lots, volume_max, max_lot)`.
5. Normalize floating-point noise: `lot_digits = max(0, round(-log10(volume_step)))`;
   `lots = round(lots, lot_digits)`.

### Invalid config → skip (early `return 0.0`, before the tail)

These cases return `0.0` *before* `_snap_clamp`, so the engine skips the trade
(matches today's `calculate_lots` early `return 0.0`):

- `balance_step` with `step_amount <= 0` or `step_size <= 0` (invalid config).
- `fixed_lot` with `fixed_lot <= 0` (invalid config).
- Any unknown `sizing_mode` (defensive).

### Valid config, small raw lot → opens the minimum (tail clamps up)

When the config is valid but the computed raw lot is below `volume_min`, the
tail's `max(lots, volume_min)` step opens the symbol's minimum instead of
skipping. This covers:

- `balance_step` with valid steps but low balance (`raw_balance = 0.0`) —
  **today's behavior**: `floor(balance/step_amount) = 0` → `0.0` →
  `max(0.0, min_lot)` = `min_lot`. Preserved exactly.
- `balance_step` + base scaling where `raw_balance * (master_lot / base)`
  undershoots `volume_min`.
- `copy_master` where `master_lot` is below the slave symbol's `volume_min`.
- `fixed_lot` where `fixed_lot` is below `volume_min`.

`copy_master` has no invalid-config guard (a real position has positive
volume); `lot_step <= 0` is handled inside `_snap_clamp` (`return 0.0`).

### Backward compatibility

Existing `settings.json` entries have no `sizing_mode` / `master_base_lot` /
`fixed_lot` keys. With defaults `sizing_mode = "balance_step"`,
`master_base_lot = 0.0` (disabled), the dispatcher takes the
`master_base_lot <= 0` branch → `raw_balance` → identical to today. Existing
sizing tests therefore stay green unchanged.

## Architecture

Approach 1 (chosen): refactor `calculate_lots` into a shared tail plus a
mode dispatcher.

```
calculate_slave_lot(mode, master_volume, balance, step_amount, step_size,
                    master_base_lot, fixed_lot, max_lot,
                    lot_step, min_lot, max_lot_symbol) -> float
   ├─ compute raw lot per mode (+ down-only base scaling for balance_step)
   └─ _snap_clamp(raw, lot_step, min_lot, max_lot, max_lot_symbol) -> float

calculate_lots(balance, step_amount, step_size, max_lot,
               lot_step, min_lot, max_lot_symbol) -> float
   └─ wrapper: early-return 0.0 on invalid step params;
      raw = floor(balance/step_amount)*step_size; return _snap_clamp(raw, ...)
```

`_snap_clamp` holds the snap-down / clamp-up / cap / round logic currently
inlined in `calculate_lots` (lines 60-82). `calculate_lots` becomes a thin
wrapper so its existing unit tests (`test_transform.py`) pass unchanged.

## Components

### `manager/engine/transform.py`
- Extract `_snap_clamp(lots, lot_step, min_lot, max_lot, max_lot_symbol)
  -> float` from the current `calculate_lots` tail (snap-down, clamp-up to
  min, cap at the two maxima, round to lot digits). Returns `0.0` if
  `lot_step <= 0`.
- Add `calculate_slave_lot(mode, master_volume, balance, step_amount,
  step_size, master_base_lot, fixed_lot, max_lot, lot_step, min_lot,
  max_lot_symbol) -> float`:
  - `balance_step`: if `step_amount <= 0` or `step_size <= 0` → `0.0`;
    `raw = floor(balance/step_amount)*step_size`; if `master_base_lot > 0` and
    `master_volume < master_base_lot`: `raw *= master_volume / master_base_lot`;
    return `_snap_clamp(raw, ...)`.
  - `copy_master`: return `_snap_clamp(master_volume, ...)`.
  - `fixed_lot`: if `fixed_lot <= 0` → `0.0`; return
    `_snap_clamp(fixed_lot, ...)`.
  - Unknown mode → `0.0` (defensive; engine skips).
- Rewrite `calculate_lots` as the wrapper above (preserves the existing
  signature + the early `return 0.0` on `step_amount/step_size <= 0`).
- Define `SizingMode` constants (module-level strings, e.g.
  `SIZING_BALANCE_STEP = "balance_step"`, `SIZING_COPY_MASTER = "copy_master"`,
  `SIZING_FIXED_LOT = "fixed_lot"`) — a plain string enum keeps serialization
  trivial (JSON-friendly) with no custom encode/decode.

### `manager/app/controller.py` — `AccountSpec`
- Add fields with defaults:
  - `sizing_mode: str = "balance_step"`
  - `master_base_lot: float = 0.0`
  - `fixed_lot: float = 0.01`
- `CopyController.start`'s `SlaveConfig(...)` construction (lines 187-193)
  passes the three new fields through.

### `manager/engine/copy_loop.py` — `SlaveConfig` & call site
- Mirror the three new fields in `SlaveConfig` (lines 14-22).
- `derive_command` (line 55-57): replace the `calculate_lots(...)` call with
  `calculate_slave_lot(cfg.sizing_mode, pos.volume, state.balance,
  cfg.step_amount, cfg.step_size, cfg.master_base_lot, cfg.fixed_lot,
  cfg.max_lot, info.volume_step, info.volume_min, info.volume_max)`.
- `update_slave_config` (lines 130-152): patch `sizing_mode`,
  `master_base_lot`, `fixed_lot` in place alongside the existing fields.

### `manager/gui/slave_editor.py`
- Add a `QComboBox` "Lot sizing mode" (three items) at the top of the sizing
  `QFormLayout`; add `QLineEdit`s "Master base lot size" and "Fixed lot size".
- Visibility on mode change (`currentIndexChanged`):
  - `balance_step` → show step amount, step size, max lots, master base lot
    size; hide fixed lot size.
  - `copy_master` → show max lots; hide step amount, step size, master base
    lot size, fixed lot size.
  - `fixed_lot` → show fixed lot size, max lots; hide step amount, step size,
    master base lot size.
  - Widgets stay constructed in all modes (so existing editor tests that
    assert their existence keep passing); only visibility toggles.
- `_spec_from_fields` (lines 122-129): read `sizing_mode` from the combo and
  the relevant numeric fields; build `AccountSpec(..., sizing_mode=...,
  master_base_lot=..., fixed_lot=...)`.
- `set_spec` (lines 131-155): set the combo + the new line edits from the
  incoming `AccountSpec`; trigger the visibility update.

### `manager/settings/store.py`
- No code change. Persistence is auto-forward/backward-compatible: the GUI
  serializes via `dataclasses.asdict` and reconstructs via
  `AccountSpec.__dataclass_fields__` filtering (`main_window.py:190`,
  `main_window.py:217-220`), so new fields with defaults load from old files
  and save to new files without migration.

### `README.md`
- Update the "Lot sizing" feature bullet (around line 70) to describe the
  three modes and the master-base-lot down-only scaling.
- Update the slave-settings usage note (around line 184) to mention choosing
  a lot-sizing mode.

## Testing

- `manager/tests/test_transform.py`:
  - Existing `calculate_lots` tests stay green (wrapper preserves behavior).
  - `_snap_clamp`: snap-down, clamp-up, cap at both maxima, lot-digit rounding,
    `lot_step <= 0 → 0.0`.
  - `calculate_slave_lot` per mode:
    - `balance_step` no base → equals `calculate_lots`.
    - `balance_step` + base, `master_lot < base` → `raw_balance * (master_lot
      / base)`, snapped/clamped; **down-only** (a `master_lot > base` case
      yields the same as no-base).
    - `copy_master` → `master_lot` snapped/clamped/capped.
    - `fixed_lot` → `fixed_lot` snapped/clamped/capped; `fixed_lot <= 0 → 0.0`.
    - Below-min raw → clamped up to `volume_min` (the chosen "open minimum"
      behavior).
    - Unknown mode → `0.0`.
- `manager/tests/test_copy_loop.py`:
  - `derive_command` produces the right `CommandMsg.volume` for each mode
    (e.g. a copy-master slave mirrors the master lot; a fixed-lot slave opens
    the fixed lot).
  - `update_slave_config` patches `sizing_mode`/`master_base_lot`/`fixed_lot`,
    and a subsequent NEW uses the new mode.
- `manager/tests/test_slave_editor.py`:
  - Mode combo + new fields round-trip through `spec()`/`set_spec`.
  - Visibility toggles on mode change (relevant fields shown, others hidden).
- `manager/tests/test_settings_store.py`:
  - A config with the new fields persists and reloads with the same values;
  - an old-style config (no new fields) loads with the defaults.
- GUI tests use the PySide6 venv at
  `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe`.

## Risks / notes

- **Down-only scaling is asymmetric by design.** A master trade larger than
  the base lot does not increase the slave's size beyond its normal
  balance-step lot. This matches the user's explicit choice; if symmetric
  scaling is later wanted, it is a one-line change (drop the
  `master_lot < master_base_lot` guard).
- **Below-min → open minimum** can over-size a very small master trade
  relative to intent (a tiny master lot in copy-master mode, or a heavily
  down-scaled balance-step trade, opens `volume_min`). This is the user's
  chosen behavior and matches the existing sizer's clamp-up. The alternative
  (skip) was considered and rejected.
- **`max_lot` is a universal cap** across all three modes (including
  copy-master). A master trade larger than `max_lot` is capped. Intentional
  and consistent.
- No worker/IPC changes: sizing remains in the manager process; the worker
  sees only `CommandMsg.volume`. Live edits to mode/base/fixed-lot take effect
  on the next NEW trade via `update_slave_config` (no `ReconfigureMsg` needed,
  matching how `step_amount`/`step_size`/`max_lot` edits work today).
- No config migration: defaults + `__dataclass_fields__` filtering handle old
  files. No version field is introduced (YAGNI; the project has no migration
  mechanism today and doesn't need one for additive defaulted fields).