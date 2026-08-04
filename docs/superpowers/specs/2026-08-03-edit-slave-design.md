# Edit an Added Slave — Design

**Date:** 2026-08-03
**Status:** Approved (brainstormed with user)

## Goal

Let the user edit a slave's trading parameters after it has been added to the
slave list, without removing and re-adding it. Editing works whether copying is
running or stopped. Edits take effect live (for future trades only) — already-open
trades are never modified by an edit.

## Background / constraints

- A slave is an `AccountSpec` (`manager/app/controller.py`):
  `id, terminal_path, symbol_map_csv, step_amount, step_size, max_lot,
  max_trade_age_minutes, normalize_sltp`.
- The GUI (`manager/gui/main_window.py`) holds `_slaves: list[AccountSpec]`,
  shown in a `QListWidget` as `"{id}: {terminal-folder}"`. Today there are only
  **Add** and **Remove** buttons; no edit.
- `SlaveEditor` (`manager/gui/slave_editor.py`) is the add dialog: `id_edit`,
  `terminal` combo, symbol table (master/slave pairs → `symbol_map_csv`),
  `step_amount`/`step_size`/`max_lot`/`max_trade_age_minutes`, `normalize_sltp`.
  It can build an `AccountSpec` from fields (`spec()`, `_spec_from_fields()`,
  `_symbol_map_csv()`) but has **no path to pre-populate from an existing spec**.
- Config persistence: `_config_dict`/`_load_config` round-trip master + slaves
  through `SettingsStore`; `aboutToQuit` saves on exit; add/remove both call
  `_save_config()`.
- Existing gating pattern: Start / Update-&-restart are disabled while copying
  is running. **Edit intentionally breaks that pattern** — it is available while
  running, per the user's explicit request.

### Where each parameter lives at runtime

This determines what "live apply, don't touch open trades" requires:

- `step_amount`, `step_size`, `max_lot`, `max_trade_age_minutes` — engine-side
  (`CopyEngine.SlaveState.config`, `manager/engine/copy_loop.py`). Read only in
  `derive_command` for **NEW** opens (`calculate_lots`, `is_too_old`).
  `MODIFY`/`PARTIAL_CLOSE`/`CLOSE` route via the `RecordTable` and use
  `rec.master_open_volume` / `rec.slave_open_volume` (stored at open time), not
  the live params. → Editing these never resizes or re-routes an open position.
- `symbol_map_csv` — engine-side (`SymbolMapper` built at `add_slave`).
  `derive_command` resolves the slave symbol only for **NEW** opens; existing
  positions are tracked by ticket linkage. → Re-mapping only affects future
  opens. Caveat: a brand-new target symbol needs `SymbolInfo`, which the worker
  reports.
- `normalize_sltp` — **worker-side** (used in `execute_command` for OPEN *and*
  MODIFY/PARTIAL). The engine does not hold it. Changing it live requires IPC to
  the worker, and it affects how future master-driven MODIFYs on open positions
  translate SL/TP. This is the only param that touches open trades, and only via
  *future* commands (never retroactively). The user explicitly accepted this
  ("apply live") as the desired behavior.

## Design

### Scope of editing

- **Editable:** `symbol_map_csv`, `step_amount`, `step_size`, `max_lot`,
  `max_trade_age_minutes`, `normalize_sltp`.
- **Locked (read-only in the dialog):** `id`, `terminal_path` — the slave's
  identity. Locking them keeps the engine key (`slave_id`) and the worker's
  terminal stable, so linkage/identity cannot break.
- **Available while running or stopped.**

### Apply timing (the user's decision)

- While running: edits apply **live** — engine-side params update in-process
  immediately; `normalize_sltp` is pushed to the worker via a new IPC message.
- Open trades are never modified: volume/linkage/age are safe by construction
  (above); `normalize_sltp` applies only to future commands (including future
  master-driven MODIFYs on open positions), never retroactively.

### Components

1. **`SlaveEditor` (`manager/gui/slave_editor.py`)** — gains
   `set_spec(spec: AccountSpec, *, lock_identity: bool = True)`:
   - Pre-fills `id_edit` (set `setReadOnly(True)`), `terminal` (set current text
     to `spec.terminal_path`, `setEnabled(False)`), parses `spec.symbol_map_csv`
     (`"m=s,m=s"`) back into the symbol-table rows, fills the four sizing
     line-edits and checks `normalize_sltp`.
   - Window title → `"Edit Slave"` when editing.
   - `spec()` / `_spec_from_fields()` are reused unchanged to build the result.
   - New module function `edit_slave(parent_window, spec) -> AccountSpec | None`
     (sibling to `add_slave`): opens `SlaveEditor` pre-populated with `spec`,
     identity locked; returns the edited `AccountSpec` or `None` on cancel.

2. **`MainWindow` (`manager/gui/main_window.py`)** — add an **"Edit Slave…"**
   button next to Add/Remove and wire **double-click** on a list row to the same
   handler. `_on_edit_slave`:
   - `row = slave_list.currentRow()`; if `row < 0` → return.
   - `new = edit_slave(self, self._slaves[row])`; if `new is None` → return.
   - `self._slaves[row] = new`; refresh that row's list label; `_save_config()`.
   - If `self._controller.is_running()`:
     `self._controller.apply_slave_edit(new.id, new)`.
   - Wrap in `try/except` mirroring `_on_start`, so a bad value logs instead of
     crashing.
   - Edit button enabled whenever a row is selected (running or not).

3. **`CopyController` (`manager/app/controller.py`)** — new
   `apply_slave_edit(self, slave_id, spec) -> None`:
   - If `self._supervisor is None` (not running): return. The edit is already in
     `_slaves` + saved config and applies on the next Start via
     `build_worker_configs`.
   - If running: call
     `self._engine.update_slave_config(slave_id, step_amount=spec.step_amount,
     step_size=spec.step_size, max_lot=spec.max_lot,
     max_trade_age_minutes=spec.max_trade_age_minutes,
     symbol_map_csv=spec.symbol_map_csv, normalize_sltp=spec.normalize_sltp)`
     (returns `map_changed: bool`), then
     `self._supervisor.reconfigure_slave(slave_id, spec.symbol_map_csv,
     spec.normalize_sltp)`.

4. **`CopyEngine` (`manager/engine/copy_loop.py`)** — new
   `update_slave_config(self, slave_id, *, step_amount, step_size, max_lot,
   max_trade_age_minutes, symbol_map_csv, normalize_sltp) -> bool`:
   - Updates `state.config` fields in place.
   - If `symbol_map_csv` changed, rebuilds
     `state.mapper = SymbolMapper(symbol_map_csv, lambda s: s in state.symbol_infos)`.
   - Returns whether the map changed.
   - Safety: `derive_command` routes `MODIFY`/`PARTIAL_CLOSE`/`CLOSE` via the
     `RecordTable` (slave_ticket + stored volumes) and only `NEW` reads these
     fields / the mapper — so the update affects future opens only.

5. **IPC `ReconfigureMsg` (`manager/ipc/messages.py`)** — new message:
   `source_id: str, symbol_map_csv: str, normalize_sltp: bool, KIND = "reconfigure"`.

6. **`Supervisor` (`manager/supervisor.py`)** — new
   `reconfigure_slave(self, slave_id, symbol_map_csv, normalize_sltp) -> None`:
   - `h = self._handles.get(slave_id)`; if `h is None` or `h.pipe is None` →
     return (no-op for a dead/fatal slave).
   - `h.config["symbol_map_csv"] = symbol_map_csv;
     h.config["normalize_sltp"] = normalize_sltp` — so a subsequent `_restart`
     spawns with the **new** config (durable across worker restarts).
   - `_send(slave_id, ReconfigureMsg(source_id=slave_id,
     symbol_map_csv=symbol_map_csv, normalize_sltp=normalize_sltp))`.

7. **Worker `_slave_loop` (`manager/worker/mt5_worker.py`)** — handle
   `ReconfigureMsg` arriving on the pipe (the loop already polls for commands):
   on reconfigure, update the local `normalize` and `symbol_map_csv`, re-run
   `build_symbol_info_msg` with the new map and `send_msg` it back (so the
   engine ingests `SymbolInfo` for any newly-mapped symbols), then continue the
   loop. Open positions / in-flight commands are unaffected — the worker never
   uses these params for `MODIFY`/`CLOSE` routing (those use `slave_ticket` from
   the command).

### Data flow while running

1. User selects a slave row and clicks **Edit Slave…** (or double-clicks).
2. `SlaveEditor.set_spec(current_spec, lock_identity=True)` pre-populates; user
   edits trading params; OK.
3. GUI: `self._slaves[row] = new`; refresh list label; `_save_config()`.
4. GUI: `self._controller.apply_slave_edit(new.id, new)` (running only).
5. Controller: `engine.update_slave_config(...)` updates `state.config` (+
   rebuilds mapper if the map changed) — future opens use new step/size/max_lot/
   max_age + new symbol map.
6. Controller: `supervisor.reconfigure_slave(...)` → `ReconfigureMsg` to worker.
7. Worker: updates `normalize_sltp` + `symbol_map_csv`, re-sends `SymbolInfoMsg`.
8. Engine ingests the new `SymbolInfo`. Open trades are never revisited.

## Edge cases / error handling

- **Not running:** `apply_slave_edit` returns early (supervisor is None). The
  edit is already in `_slaves` + saved config and applies on next Start — no
  special path needed.
- **No row selected:** Edit button / double-click is a no-op (guarded by
  `currentRow() < 0`). The Edit button is enabled only when a row is selected.
- **Worker dead or fatal at edit time:** `reconfigure_slave` finds
  `h.pipe is None` (or `h.fatal`) → no-op send. Engine-side params are still
  updated live, so future opens via the engine use new values immediately
  regardless of worker state. When the worker is later restarted, it spawns
  from `h.config`, which `reconfigure_slave` updated — so the restart picks up
  the edit. Fatal workers don't restart (per the Task B fix); a fatal slave
  stays surfaced as an error until the user fixes the terminal and Starts
  again, at which point the saved config applies.
- **Symbol map gains a brand-new target symbol:** the worker's re-reported
  `SymbolInfoMsg` supplies its `SymbolInfo` to the engine before any new open
  targets it. If a new open arrives in the tiny window before `SymbolInfo`
  lands, `derive_command` returns `None` (`info is None`) and the open is
  skipped this tick — it is copied on the next snapshot once info arrives. No
  crash, no bad order. (Consistent with the existing startup readiness behavior.)
- **Invalid numeric input** (e.g. `step_amount = "abc"`): `_spec_from_fields`
  does `float(...)` and would raise. This is a **pre-existing** latent issue in
  the Add path too, not introduced by this feature. It is deliberately **not**
  bundled (single-focus; fixing it would also change Add). Flagged as a
  follow-up. The GUI edit handler wraps the call in `try/except` mirroring
  `_on_start`, so a bad value logs instead of crashing.
- **`id` / `terminal_path` locked:** the dialog shows them read-only, so the
  engine key (`slave_id`) never changes and `h.config["terminal_path"]` stays
  consistent. No linkage/identity breakage.
- **Concurrent edits / double-edit:** the dialog is modal (`exec()`), so only
  one edit at a time; no reentrancy.

## Testing

All tests live in the existing PySide6-GUI + headless pytest suites. Run the GUI
suite with the app venv that has PySide6
(`C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q`),
per the `gui-tests-need-pyside6-venv` memory.

- `test_slave_editor.py` — `set_spec` pre-populates every field;
  `id_edit.isReadOnly()` and `terminal.isEnabled() is False` when
  `lock_identity=True`; the symbol table round-trips from `symbol_map_csv` and
  back; `edit_slave` returns an `AccountSpec` whose locked fields equal the
  original and whose trading fields reflect the edits.
- `test_main_window.py` — Edit button + double-click open the editor pre-filled
  with the selected slave; on OK, `_slaves[row]` and the list label update and
  config is saved; no-selection is a no-op.
- `test_controller.py` — `apply_slave_edit` while running updates engine config
  and calls `reconfigure_slave`; while not running is a no-op (engine/supervisor
  untouched).
- `test_copy_loop.py` — `update_slave_config` updates fields and rebuilds the
  mapper only when `symbol_map_csv` changes; **open-trade safety**: with an open
  position in the `RecordTable`, changing `step_amount` / `symbol_map_csv` does
  not alter the `MODIFY`/`PARTIAL_CLOSE`/`CLOSE` commands for that ticket (still
  routed via the record + stored volumes), while a subsequent `NEW` uses the new
  params.
- `test_supervisor.py` — `reconfigure_slave` sends a `ReconfigureMsg` and
  updates `h.config` (so a later restart spawns with new config); no-op when the
  handle/pipe is gone.
- `test_mt5_worker.py` — the worker receives `ReconfigureMsg`, updates its
  `normalize_sltp` + `symbol_map_csv`, and emits a fresh `SymbolInfoMsg`;
  in-flight `MODIFY`/`CLOSE` commands are unaffected.

## Out of scope (noted, not built)

- Numeric-input validation in the editor (pre-existing; affects Add too).
- Live edit of `id` / `terminal_path` (intentionally locked).