# Master Lot Tracking (Follow the Master's Largest Lot)

Date: 2026-11-17
Status: Approved (design)
Size: Project (engine + controller + settings + GUI + tests)

## Intent

The slave's lot sizing uses a manually configured **Master base lot**. That
number goes stale when the user raises their usual master lot over time: with
base 10 and fixed lot 0.1, a master trade of 10 opens 0.1. After the user's
usual master lot grows to 20, that same 10-lot trade should open 0.05 (half the
new base) — but the copier still sees 10 as the base and opens 0.1.

The copier should learn the master's current base from what it observes and
scale down relative to that. It must **never** scale the slave up: the fixed
lot (or the balance-step lot) is the ceiling; a base that has grown only makes
smaller trades smaller.

## Goals

- Learn, per master symbol, the largest master lot observed, and use it as the
  effective base — across restarts.
- Effective base = `max(configured master_base_lot, learned lot)`.
- Down-only scaling exactly as today: a 23-lot trade sets the base to 23 and
  opens 0.1 (fixed 0.1); a 10-lot trade then opens 0.05.
- Opt-in per profile/slave; existing configurations stay byte-for-byte
  behaviour-identical (default off).
- Applies to `balance_step` and `fixed_lot`; `copy_master` is unaffected.
- Visible and resettable in the GUI.

## Non-goals

- Symmetric up-scaling (a master lot above the base never raises the slave lot).
- A rolling window or automatic downward adaptation: the learned base only
  grows; lowering it is a manual reset.
- A configurable "master lot step" (the user chose the exact observed maximum;
  a step field was considered and dropped).
- Per-slave learned values; the learned base is a property of the master and is
  shared by every slave that enables tracking.
- Worker/IPC changes. Sizing stays manager-side.

## Anti-goals

- Changing sizing behaviour for configurations that do not opt in.
- Counting slave positions or non-master volumes as observations.
- Persisting non-finite or non-positive lots.
- Letting the learned base break the existing snap/clamp/cap/round tail; Max
  Lots remains the universal cap.

## Constraints

- `calculate_slave_lot` and the shared `_snap_clamp` tail stay unchanged; the
  effective base is computed at the `derive_command` call site.
- The engine is driven by the supervisor's daemon thread; the GUI reset can run
  concurrently, so the learned store must be thread-safe.
- Settings writes are atomic (`SettingsStore.save`) and rare (only when the
  learned maximum grows or on reset).
- Defaults keep old `settings.json` files working: new booleans default
  `False`, new settings keys are ignored by older versions.

## Approaches considered

1. **Track the largest observed master lot, per symbol, persisted, opt-in**
   (chosen). Matches the user's request exactly, keeps the existing sizer and
   its down-only formula, and is backward compatible.
2. **A configurable master lot step** that rounds the observed maximum down to
   clean multiples. Rejected: an extra field for a benefit the user does not
   need — their master lots are clean multiples already.
3. **Auto-edit the configured `master_base_lot`** when a bigger master trade is
   seen. Rejected: it mutates user configuration behind their back, has no
   per-symbol notion, and would affect slaves/profiles that never opted in.

## Design

### Observation and effective base

- The engine observes every master snapshot. For each `Position` in
  `snapshot.positions`, it updates the learned value for `Position.symbol`
  when `volume` is finite and greater than the current value.
- `derive_command` computes, for a NEW event:

  ```python
  base = cfg.master_base_lot
  if cfg.follow_master_lot:
      base = max(base, learned.get(pos.symbol, 0.0))
  ```

  and passes `base` as the `master_base_lot` argument of the existing
  `calculate_slave_lot`. No sizer change.
- Observations are applied **before** deriving commands in the same tick, so a
  23-lot NEW opens 0.1 for itself (base 23), not after one extra tick.
- Per master symbol: a large-lot instrument never poisons the base for a
  small-lot instrument.
- Tracking accumulates even when no slave has opted in (cheap); only opted-in
  slaves use it.

### Thread-safe learned store

A small `LearnedMasterLots` holder (new, in `manager/engine/`), guarded by a
`threading.Lock`:

- `update(positions)` → applies per-symbol maxima; returns True if anything grew.
- `get(symbol) -> float` → 0.0 when unknown.
- `as_dict() -> dict[str, float]` → a copy for persistence/callbacks.
- `reset()` → clears all values.

`CopyEngine` owns one instance. `SlaveState` gets a reference to it;
`derive_command` reads through `get()`.

### Persistence

`SettingsStore` gains:

- `load_master_lot_tracking() -> tuple[str, dict[str, float]]` — reads
  `data["global"]["master_lot_tracking"]["terminal_path"]` and `["lots"]`.
  Missing/corrupt values degrade to `("", {})`; invalid lot entries (non-str
  key, non-finite, `<= 0`) are dropped individually.
- `save_master_lot_tracking(terminal_path, lots)` — merges into
  `data["global"]` and saves atomically.

Shape:

```json
"global": {
  "master_lot_tracking": {
    "terminal_path": "C:/MT5-master/terminal64.exe",
    "lots": {"EURUSD": 23.0, "XAUUSD": 0.5}
  }
}
```

`CopyController.start`:
1. Normalizes the master path via the existing `prepare`/`assigned` step.
2. Loads the stored tracking.
3. If the stored terminal path differs from the normalized current one, drops
   the lots (a different terminal must not inherit a stale base) and saves the
   new path.
4. Passes the lots and an `on_learned_lots_changed` callback into
   `build_supervisor` → `CopyEngine`.

The engine calls the callback whenever the maximum grows or tracking is reset.
The controller persists via `save_master_lot_tracking` (try/except → log) and
forwards the new dict to the GUI callback.

### Reset

- `CopyController.reset_learned_master_lots()`: resets the engine's holder (if
  running), saves an empty `lots` map, and emits the callback.
- The Main window's Master pane shows a label with the learned values
  (e.g. `Learned master lots: EURUSD 23, XAUUSD 0.5`, or `none yet`) and a
  **Reset learned master lots** button, enabled only when values exist.
- The label also refreshes on Start/Stop and via a new marshaled signal
  (`_StatusBridge.learned` → `MainWindow.on_learned_lots`), following the
  existing status/log bridge pattern.

### Configuration plumbing

- `AccountSpec.follow_master_lot: bool = False`
- `CopyProfile.follow_master_lot: bool = False`
- Add `"follow_master_lot"` to `SHARED_PROFILE_FIELDS` (so profiles apply it
  and the GUI round-trips it via `dataclasses.asdict` /
  `__dataclass_fields__` filtering).
- `coerce_profiles`: validate `follow_master_lot` as a bool (like
  `normalize_sltp`); an invalid value drops the profile whole.
- `SlaveConfig.follow_master_lot: bool = False`; passed in `start` and patched
  by `update_slave_config` / `apply_slave_edit`.

### GUI

`CopySettingsWidget` (shared by both editors):
- New `QCheckBox` **"Follow the master's largest lot"**, placed directly after
  the Master base lot row.
- Visible when the mode is `balance_step` or `fixed_lot`; hidden in
  `copy_master` (the base lot is irrelevant there).
- Included in `values()`, `set_values()`, and `set_editable()`.

Main window Master pane:
- `learned_lots_label` and `reset_learned_lots_button` as above.

## Backward compatibility

- Existing configs have no `follow_master_lot` → `False`; sizing is identical.
- Existing `settings.json` files have no `master_lot_tracking` key → empty.
- Older app versions ignore the new keys.

## Edge cases

- The largest trade in a tick is applied before all derivations in that tick.
- Learned value below the configured base → configured base wins (`max`).
- Master opens only a half position after a restart that never observed the
  full one → the base is still the configured value until a full-size trade is
  seen (inherent to observation-based learning; documented).
- Non-finite position volumes are ignored.
- `max_lot` still caps the slave order; the learned base is a reference, not an
  order size, and is not capped.
- `copy_master` slaves ignore both bases.

## Testing

- `manager/tests/test_transform.py` — unchanged sizer; no new tests needed.
- New/updated engine tests: per-symbol learning, same-tick application,
  callback on growth, reset, `get()` for unknown symbols, ignore non-finite.
- `derive_command` tests: follow on/off, effective base `max`, down-only,
  per-symbol isolation, `copy_master` untouched.
- Controller tests: load at start, auto-reset on master-path change, persist on
  learn, `reset_learned_master_lots`.
- Settings store tests: round-trip, corrupt/missing tracking degrades, invalid
  entries dropped, old files load defaults.
- GUI tests: checkbox round-trip + visibility (balance/fixed shown, copy-master
  hidden); main-window label/reset button; old configs default `False`.
