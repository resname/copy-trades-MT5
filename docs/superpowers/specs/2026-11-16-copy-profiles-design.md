# Copy Profiles — shared copy settings across slaves

Date: 2026-11-16
Status: Proposed (design)

## Problem

Today every slave owns its full copy configuration: `AccountSpec` in
`manager/app/controller.py` carries the symbol map, lot-sizing mode and its
parameters, max lot, max trade age, and the SL/TP normalization flag alongside
the slave's identity (`id`, `terminal_path`). When several accounts need the
same copy behavior, the user re-enters the same values for every slave and has
to edit each slave when one setting changes.

The user wants a first-class **copy profile**: define the copy settings once,
attach any number of slaves to it, and have a profile edit apply to every
attached slave — including live while copying.

Decisions already made with the user:

- A profile holds **all** copy settings, **including the symbol map**.
- UI variant **A**: a dedicated "Copy profiles" group in the main window, with
  New / Edit / Duplicate / Delete; the slave editor becomes identity + profile
  picker.
- On the first start of a profile-aware manager, legacy configs are migrated
  **once**: slaves with identical copy settings are grouped into one shared
  profile; lone slaves stay "own settings". Migration never loses settings.
- A slave may remain without a profile ("Own settings") — the normal case is a
  profile, but standalone stays supported.
- Deleting a profile is blocked while slaves are attached.
- A profile edit applies live to attached slaves through the existing
  `apply_slave_edit` path; open trades are never modified.

## Goals

1. `CopyProfile` is a stored entity: name + symbol map + sizing mode +
   master base lot + fixed lot + step amount + step size + max lot + max trade
   age + normalize SL/TP.
2. Any number of slaves may reference one profile; a slave references at most
   one profile.
3. Editing a profile updates every attached slave's in-memory spec and, when
   copying, the running engine + workers via the existing live-apply path.
4. Profile management UI in the main window (Variant A).
5. The slave editor keeps working for standalone slaves and gains a profile
   picker plus an inline "New profile…" action.
6. Old `settings.json` files load losslessly (with the one-time grouping
   migration below); saving stays materialized so a downgrade to an older
   manager remains functional.
7. Tests cover the data model, persistence/migration, GUI handlers, and live
   apply.
8. One-time, lossless migration of pre-profile configs: after the update,
   slaves that already shared identical settings share one profile; everything
   else (master, autostart, accounts, provisioned instances, global) is
   untouched.

## Non-goals

- No drag-and-drop/tree slave grouping (Variant C).
- No per-slave overrides on top of a profile: a profile-attached slave's copy
  settings are exactly the profile's.
- No profiles for the master (the master has no copy settings).
- No worker/IPC protocol change: the worker already receives concrete values;
  only the manager-side source of those values changes.
- No worker restart on profile edit (the existing live reconfigure is used).
- No migration prompt or review step: migration is automatic and logged.
- No profile import/export.

## Constraints

- `settings.json` keeps its shape: `config.master`, `config.slaves`,
  `config.autostart`; `config.profiles` is added. `accounts`,
  `provisioned_instances`, and `global` are untouched.
- Identity (`id`, `terminal_path`) is per slave; a profile never changes it.
- Live apply goes through `CopyController.apply_slave_edit`, which calls
  `CopyEngine.update_slave_config` and `Supervisor.reconfigure_slave`.
- The profile's shared fields are exactly the non-identity fields of
  `AccountSpec`.
- GUI tests must stay hermetic (FakeController, no real terminals/tasks).

## Approaches considered

- **A — Profiles group in the main window + shared settings editor (chosen).**
  Profiles are visible, directly editable, and can exist without slaves.
  Reuses the live-apply path 1:1. Cost: one more group in an already tall
  window, and the slave editor must be split into identity + shared settings.
- **B — Everything inside the slave editor.** A profile dropdown + "Edit
  profile…" only there. Compact, but profiles are hidden and a profile cannot
  exist before its first slave. Rejected because managing a shared entity only
  through a slave is awkward.
- **C — Slave tree grouped by profile with drag & drop.** Best for very many
  accounts, but the largest change and the highest regression risk for no
  requested benefit. Rejected.

## Design

### Data model

In `manager/app/controller.py`:

```python
@dataclass
class CopyProfile:
    id: str
    name: str
    symbol_map_csv: str = ""
    step_amount: float = 100.0
    step_size: float = 0.01
    max_lot: float = 10.0
    max_trade_age_minutes: float = 10.0
    normalize_sltp: bool = True
    sizing_mode: str = "balance_step"
    master_base_lot: float = 0.0
    fixed_lot: float = 0.01


SHARED_PROFILE_FIELDS = (
    "symbol_map_csv", "step_amount", "step_size", "max_lot",
    "max_trade_age_minutes", "normalize_sltp", "sizing_mode",
    "master_base_lot", "fixed_lot",
)
```

`AccountSpec` gains `profile_id: str = ""`. Its shared fields remain present
and are always materialized from the profile when `profile_id` points at one;
that keeps the store self-contained and downgrade-safe.

Helpers in the same module:

- `profile_from_spec(profile_id, name, spec) -> CopyProfile` — copies the
  shared fields out of an `AccountSpec`.
- `apply_profile(spec, profile) -> AccountSpec` — returns a new `AccountSpec`
  with `id`/`terminal_path` preserved, shared fields copied from `profile`,
  and `profile_id = profile.id`.
- `new_profile_id(existing_ids) -> str` — `uuid4().hex[:8]`, re-drawn on the
  (vanishingly unlikely) collision.

Profile names are user-visible, trimmed, non-empty, and unique
case-insensitively within the profile list. Selection everywhere uses the
profile `id`, never the name.

### Persistence

`MainWindow._config_dict()` writes:

```json
{
  "master": {"terminal_path": "..."},
  "profiles": [
    {"id": "a1b2c3d4", "name": "Propfirm A", "symbol_map_csv": "...",
     "step_amount": 100.0, "...": "..."}
  ],
  "slaves": [
    {"id": "s1", "terminal_path": "...", "profile_id": "a1b2c3d4",
     "symbol_map_csv": "...", "...": "materialized shared fields"}
  ],
  "autostart": {"...": "..."}
}
```

- Attached slaves are materialized from their profile immediately before
  writing, so the file is internally consistent.
- Profile dicts are written with `dataclasses.asdict`.
- A slave with no profile keeps `profile_id: ""` and its own values.

`_load_config()`:

1. Load `config.profiles` (if present) through a validating coercion pass:
   - an entry must be a dict with a non-empty string `id` and `name`;
   - every numeric field must convert with `float(...)` (a real number, bool
     excluded), `normalize_sltp` must be a real `bool`, `sizing_mode` must be
     one of the known modes, and `symbol_map_csv` must be a string;
   - any entry that fails is **dropped as a whole** (log
     `profile <id> dropped: <reason>`) — never trust and never partially
     coerce, because a bad value copied into a slave can raise at Start or
     during live apply;
   - duplicate ids: the first entry wins; later ones are dropped with a log;
   - empty names become `"Profile <id[:4]>"`; duplicate names
     (case-insensitive) get `" 2"`, `" 3"`, … appended. Both are logged, and
     attachments are preserved.
2. Load slaves exactly as today (dataclass-field filtering), which tolerates
   old files without `profile_id`.
3. If a slave has a non-empty `profile_id` that resolves, replace its shared
   fields with `apply_profile(...)`.
4. If the `profile_id` does not resolve, keep the slave's stored values, clear
   `profile_id`, and append a log line
   (`profile <id> for slave <sid> not found — using stored settings`). A
   profile dropped in step 1 is indistinguishable from a missing one here:
   its slaves keep their materialized values, clear `profile_id`, and log.
5. Legacy slaves (no `profile_id`) load unchanged; the one-time migration
   described next may then group them into profiles.
6. The existing guard that drops saved `platform: "ib"` slaves stays.

### One-time migration of legacy configs

Trigger: `_load_config` sees a `config` dict **without** a `profiles` key.
Profile-aware versions always write the key (even as `[]`), so migration runs
once. A store-less window (GUI tests, embedded use) never migrates or saves.
Slaves in such a config are loaded as today; any stale `profile_id` is ignored
because no profiles exist to resolve.

"Identical" means every shared setting is equal:

- `step_amount`, `step_size`, `max_lot`, `max_trade_age_minutes`,
  `master_base_lot`, `fixed_lot` compared as floats;
- `normalize_sltp` and `sizing_mode` compared as-is;
- `symbol_map_csv` compared as a mapping via `parse_symbol_map` (row order does
  not matter); if the CSV cannot be parsed, compare the raw string.

For each group of **two or more** slaves, create one `CopyProfile` whose shared
fields are the group's values. Name it `"<first slave id> + <second slave id>"`
for two slaves, or `"<first slave id> + <n-1> more"` for three or more; run the
name through the same uniqueness helper. Assign that `profile_id` to every
slave in the group, keeping each `terminal_path` and materialized fields.
Groups of exactly one slave keep `profile_id = ""` ("own settings") and create
no profile.

Each created profile logs one line:
`migrated slaves s1, s2, s3 into profile 's1 + 2 more'`.

Immediately after a migration run, `_save_config()` writes the new shape
(including `profiles: []` when no group qualified), which persists the result
and marks migration as done.

The grouping/naming logic is a pure function in
`manager/app/controller.py`:
`migrate_legacy_profiles(slaves) -> tuple[list[CopyProfile], dict[str, str]]`
(returns the new profiles and a `slave_id -> profile_id` map), unit-testable
without Qt.

Nothing else in the store is rewritten: `master`, `autostart`, `accounts`,
`provisioned_instances`, and `global` pass through `SettingsStore`'s merge
untouched, as they do today.

### Main window (Variant A)

A new `QGroupBox("Copy profiles")` sits between the Master pane and the Slave
list:

```
Copy profiles
  Propfirm A
  Propfirm B
  [New…] [Edit…] [Duplicate…] [Delete]
```

- `profile_list: QListWidget` — items show the profile name; the item tooltip
  shows a one-line summary (lot-sizing mode + step/base/max, max trade age,
  normalize on/off, symbol-map row count).
- `add_profile_button` "New…", `edit_profile_button` "Edit…",
  `duplicate_profile_button` "Duplicate…", `delete_profile_button` "Delete".
- Edit/Delete/Duplicate are enabled only with a selection; double-click =
  Edit.
- State lives in `MainWindow._profiles: list[CopyProfile]` plus
  `_profile_by_id(id) -> CopyProfile | None`.

Slave rows become
`"{id}: {terminal basename} ({profile name})"` or
`"{id}: {basename} (own settings)"`, via one `_slave_label(spec)` helper used
on add, edit, load, and profile changes. The helper looks the profile up by
`spec.profile_id`; an unknown id renders "(own settings)".

### Profile editor

New `manager/gui/profile_editor.py`:

- `CopySettingsWidget(QWidget)` — extracted from the slave editor's current
  widgets: the symbol map table (`Master symbol (regex)` / `Slave symbol`
  headers, Add/Remove Row), the lot-sizing form (mode combo, master base lot,
  fixed lot, step amount, step size, max lots, max trade age) with the same
  mode-dependent visibility, and the "Normalize SL/TP" checkbox.
  - `values() -> dict` returns the shared-field dict.
  - `set_values(dict | AccountSpec | CopyProfile)` populates it.
  - `set_editable(bool)` enables/disables all inputs (used for a
    profile-attached slave preview).
- `ProfileEditor(QDialog)` — a name field plus a `CopySettingsWidget`.
  - Constructor: `ProfileEditor(profile: CopyProfile | None = None, *,
    existing_names: Iterable[str] = (), exclude_profile_id: str = "",
    parent=None)`. The caller passes the names it must not collide with (all
    other profiles) and, when editing, the profile's own id so its current
    name does not count as a duplicate.
  - `set_profile(profile)` pre-populates; `profile() -> CopyProfile | None`
    returns the result on accept.
  - `accept()` is overridden: it trims the name, validates non-emptiness and
    case-insensitive uniqueness against `existing_names` (minus the excluded
    id), and validates every numeric field with `float(...)`. On any failure
    it shows a `QMessageBox.warning` parented to the dialog and returns
    without calling `super().accept()`, so the dialog stays open and the
    entered values are kept (unlike the slave editor, which keeps its current
    propagate behavior).
  - The dialog owns name validation; callers never re-open or post-validate.
- `new_profile(*, existing_names=(), existing_ids=(), seed: CopyProfile |
  None = None) -> CopyProfile | None` and
  `edit_profile(profile, *, existing_names=()) -> CopyProfile | None` module
  helpers, mirroring `add_slave`/`edit_slave`. `new_profile` generates the id
  via `new_profile_id`; `seed` pre-fills the settings (used by "Duplicate…"
  and by the slave editor's "New profile…").

### Slave editor changes

`SlaveEditor` keeps identity and the live settings preview:

- Identity: `Slave id`, `Terminal`, `Open terminal for login` (unchanged).
- New `profile_combo: QComboBox` labelled "Copy profile":
  - item 0: `"Own settings (no profile)"`, data `""`;
  - one item per profile, data = profile id;
  - a disabled separator;
  - `"New profile…"`, data `"__new__"`.
- An `"Edit profile…"` button is **not** in the slave editor; profile editing
  lives in the main window (the user's approved Variant A sketch).
- `CopySettingsWidget` sits below the profile picker:
  - with "Own settings": enabled and shows the slave's own values;
  - with a profile selected: `set_editable(False)` and shows the profile's
    values, plus a hint label
    (`Settings come from profile 'X' — edit it under Copy profiles.`).
- Choosing `"New profile…"` opens `ProfileEditor` seeded from the widget's
  current values, with `existing_names` taken from the `profiles` list the
  editor was constructed with. On accept the editor calls the injected
  `create_profile(profile) -> None` callback (wired by `MainWindow`, which
  appends and saves) and selects the new profile; on cancel the combo reverts
  to the previous item. Duplicate/empty names are rejected inside the dialog,
  so the callback never has to reject.
- Constructor: `SlaveEditor(controller, profiles: list[CopyProfile] = (),
  create_profile: Callable[[CopyProfile], None] | None = None, parent=None)`;
  `add_slave(...)` / `edit_slave(...)` take the same two extra arguments.
- `spec()` returns an `AccountSpec` with identity fields from the form and
  shared fields from the selected profile (or from the widget for own
  settings), with `profile_id` set accordingly.
- `set_spec(spec, lock_identity=True)` pre-selects the profile and refreshes
  the widget; identity locking is unchanged.
- Profile preview never overwrites a standalone slave's own settings:
  - the editor snapshots the editable widget's values when it leaves "Own
    settings" for a profile view;
  - returning to "Own settings" restores that snapshot when the slave started
    as own settings (including a new slave with defaults), so a mis-click does
    not silently replace its values;
  - detaching a slave that started profile-attached (selecting "Own settings")
    keeps the widget's displayed profile values as the slave's new own
    settings; there is no snapshot to restore in that case.
  The rule in one line: on OK, an own-settings slave keeps its original own
  values unless it was attached; a detached slave inherits the profile values
  it was showing.

`add_slave()` / `edit_slave()` gain the `create_profile` callback argument
(passed through from `MainWindow`).

### Main-window handlers

- `_on_add_slave`: unchanged flow; the callback stores the new profile, saves,
  and refreshes both lists.
- `_on_edit_slave`: replaces the spec (now possibly with a new `profile_id`),
  relabels the row, saves, and live-applies when running (existing
  `apply_slave_edit`).
- `_on_add_profile`: `new_profile` (dialog validates the name) → append →
  save → select.
- `_on_edit_profile`: `edit_profile` (dialog validates the name) → replace in
  `_profiles` → `_apply_profile_change(profile)`.
- `_on_duplicate_profile`: prefill from the selected profile with
  `"<name> (copy)"` (made unique by appending ` 2`, ` 3`, … if needed), new id
  → append → save.
- `_on_delete_profile`: if any slave references it, show a `QMessageBox`
  listing the slave ids and do not delete; otherwise remove and save.
- `_apply_profile_change(profile)`:
  1. replace the stored profile;
  2. for every slave with `profile_id == profile.id`: build
     `effective = apply_profile(spec, profile)`, replace the spec, update the
     slave-list row label; if `controller.is_running()`, call
     `controller.apply_slave_edit(slave_id, effective)` (catch per slave, log,
     continue);
  3. refresh the profile row label/tooltip and save config.
- Profile rows get `_profile_label(profile)` (name) and
  `_profile_tooltip(profile)` (summary); both are pure helpers for testing.

### Live behavior

- Editing a profile while copying updates every attached slave's engine config
  and pushes the symbol map + normalize flag to its worker — the same code
  path as editing one slave today. Open trades are untouched; the new settings
  affect only future decisions, exactly as documented for `apply_slave_edit`.
- Attaching/detaching a slave while copying goes through the same live path
  (its shared fields and symbol map change; the worker process is not
  restarted).
- Adding a brand-new slave while copying still requires the next Start, as
  today — the manager does not spawn a worker for a slave it has not started.

### Backward and forward compatibility

- Old configs (no `profiles`): run the one-time migration described above.
  Slaves that shared identical settings end up on one profile; lone slaves
  appear as "(own settings)".
- New configs opened by an older manager: `profiles` is ignored, and each
  attached slave still carries its materialized shared fields, so copying
  behavior is preserved.
- A corrupted/missing profile reference never crashes the app; it degrades to
  the slave's stored settings with a log line.

### Edge cases

- Empty profile name or duplicate name (case-insensitive) → rejected with a
  warning.
- Deleting a profile with attached slaves → blocked.
- Deleting the last profile → fine.
- A profile with zero slaves → allowed.
- The combo's "New profile…" is transient: it never becomes the selected
  value in `spec()`; selection falls back to the previous item on cancel.
- Unknown `sizing_mode` in a profile → the combo falls back to the first item,
  matching today's slave-editor behavior.
- Profiles are identified by id, so renaming a profile updates labels but not
  attachments.

## Files touched

- `manager/app/controller.py` — `CopyProfile`, `AccountSpec.profile_id`,
  shared-field tuple, helpers, and `migrate_legacy_profiles`.
- `manager/gui/main_window.py` — profiles group, handlers, labels, config
  load/save (including the one-time legacy migration), live apply.
- `manager/gui/slave_editor.py` — extract `CopySettingsWidget`, add profile
  picker + callback, own-settings behavior.
- `manager/gui/profile_editor.py` — new: `CopySettingsWidget`,
  `ProfileEditor`, helpers.
- `manager/tests/test_controller.py` — model helpers.
- `manager/tests/test_slave_editor.py` — updated for the split editor +
  profile modes.
- `manager/tests/test_profile_editor.py` — new.
- `manager/tests/test_main_window.py` — profiles group, handlers, persistence,
  migration, live apply.
- `README.md` — usage: create a profile, attach slaves, edit once.
- `docs/superpowers/specs/2026-11-16-copy-profiles-design.md` — this document.

## Testing

- **Model** (`test_controller.py`): `profile_from_spec` copies exactly the
  shared fields; `apply_profile` preserves `id`/`terminal_path`, overrides
  only shared fields, sets `profile_id`; `migrate_legacy_profiles` groups
  identical configs (including symbol-map row-order differences), leaves lone
  slaves unassigned, and names groups `"s1 + s2"` / `"s1 + 2 more"`.
- **Profile editor** (`test_profile_editor.py`): constructs; round-trips
  values; `accept()` rejects empty/duplicate names and non-numeric fields
  without closing the dialog; `CopySettingsWidget.set_editable(False)`
  disables inputs.
- **Slave editor** (`test_slave_editor.py`): dropdown has "Own settings",
  profiles, and `New profile…`; profile selection disables the settings
  widget and `spec()` carries `profile_id` + profile values; own-settings mode
  stays editable; `New profile…` calls the injected callback and selects the
  new profile; cancel reverts; own → profile → own restores the snapshot;
  profile-attached → own keeps the displayed profile values.
- **Main window** (`test_main_window.py`):
  - group + buttons construct;
  - config round-trip with profiles and `profile_id`;
  - legacy config without `profiles` loads with "(own settings)" labels
    (lone slave case);
  - a legacy config with two or more identical slaves migrates to one shared
    profile, attaches all of them, saves the new shape, and logs; a second
    load does not migrate again;
  - legacy slaves with different settings stay "(own settings)";
  - malformed profile entries are dropped whole and duplicate ids keep the
    first; their slaves fall back to stored settings and log;
  - empty/duplicate profile names are made unique at load, attachments kept;
  - unresolved `profile_id` falls back and logs;
  - edit profile updates all attached specs, labels, persists, and calls
    `apply_slave_edit` for each attached slave exactly when running;
  - detaching preserves the last settings;
  - delete is blocked while attached and allowed otherwise;
  - duplicate gets a unique name and a new id;
  - slave label shows the profile name.
- All existing tests keep passing (slave-editor tests updated for the moved
  widgets).

## What's left to the builder

- Exact widget layout/spacing of the extracted `CopySettingsWidget` (preserve
  current labels and headers).
- Exact summary/tooltip wording.
- Whether profile rows also show the slave count (a nice-to-have; not
  required).
- Whether to add a "Save current slave settings as a profile…" shortcut (not
  required by this design).
