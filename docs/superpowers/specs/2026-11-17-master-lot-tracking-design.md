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
- Tracking accumulates and persists even when no slave has opted in (cheap;
  the values are inert until a slave enables the flag). This is a deliberate
  builder-check call: a user who turns tracking on later immediately has the
  history, and the cost is one inert settings key.

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

**Settings write serialization (builder-check call).** `SettingsStore` gains
one internal `threading.RLock` that covers every read-modify-write operation:
`save_config`, `save_master_lot_tracking`, and the new reset path each
re-read the file *inside* the lock before writing. The atomic `os.replace`
already prevents a torn file; the lock prevents lost updates between the
engine's persistence callback (supervisor thread) and the GUI's config save
(GUI thread). A growth callback in flight during a reset therefore cannot
resurrect the old values, and a GUI save cannot drop the newly learned max.

**Terminal identity (builder-check call).** The tracking key is the normalized
exe path actually used for the master this run — `assigned[master.id].exe_path`
— normalized the same way `TerminalManager._norm` normalizes paths (unified
separators, case-insensitive on Windows). The raw GUI text is never compared
against the stored value.

`CopyController.start`:
1. Normalizes the master path via the existing `prepare`/`assigned` step.
2. Loads the stored tracking.
3. If the stored terminal path differs from the normalized current one, the
   session runs with empty lots — but the stored drop + new path are **not
   persisted yet**. A failed Start (readiness, Algo Trading, spawn error) must
   not destroy the previous terminal's learned values. The drop is committed
   only after `sup.start()` succeeds; the first growth callback re-saves with
   the new path anyway.
4. Passes the lots and an `on_learned_lots_changed` callback into
   `build_supervisor` → `CopyEngine`.

The engine calls the callback whenever the maximum grows or tracking is reset.
The controller persists via `save_master_lot_tracking` (try/except → log) and
forwards the new dict to the GUI callback.

### Reset

- `CopyController.reset_learned_master_lots()`: resets the engine's holder (if
  running), saves an empty `lots` map, and emits the callback.
- **Reset must stick against still-open positions (builder-check call).** The
  engine observes every snapshot, so a naive reset would re-learn the 23-lot
  position on the very next tick. `LearnedMasterLots.reset()` therefore takes
  the set of master tickets open at reset time; `update()` skips those tickets
  until they close (they disappear from the snapshot). After they close, a new
  large trade can raise the base again.
- The Main window's Master pane shows a label with the learned values
  (e.g. `Learned master lots: EURUSD 23, XAUUSD 0.5`, or `none yet`) and a
  **Reset learned master lots** button, enabled only when values exist.
- **While stopped** (builder-check call): the label shows the persisted values
  loaded at construction, and Reset clears the persisted `lots` for the stored
  terminal path (the path key itself is kept). While running, Reset also
  clears the in-memory holder with ticket suppression above.
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
- Reset while the large position is still open: the ticket is suppressed, so
  the base stays lowered until that position closes.
- A failed Start leaves the stored tracking of the previous terminal intact.
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
  hidden); main-window label/reset button (running and stopped); old configs
  default `False`.

## Builder-check decisions (author's calls)

The fresh builder raised six points; all were settled here rather than reopened
with the user:

1. **Settings consistency** — one `RLock` in `SettingsStore` covers every
   read-modify-write; no lost updates between the engine callback and GUI
   saves.
2. **Reset durability** — tickets open at reset are suppressed until they
   close, so Reset cannot be undone by the next tick.
3. **Failed Start** — the terminal-change drop is held in memory and committed
   only after a successful start.
4. **Terminal identity** — the normalized `assigned[master.id].exe_path` is the
   key, compared with `TerminalManager._norm` semantics.
5. **Opt-in scope** — observation and persistence run regardless of whether a
   slave enabled the flag; the stored values are inert otherwise.
6. **Stopped state** — the Master pane shows persisted values and Reset works
   while stopped (clears the stored lots, keeps the path key).
