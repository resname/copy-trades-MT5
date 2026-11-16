# Copy Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add shared copy profiles so one profile's copy settings (including the symbol map) drive many slaves, edits apply live to all attached slaves, and legacy configs are migrated once without losing settings.

**Architecture:** A `CopyProfile` dataclass in `manager/app/controller.py` holds the shared subset of `AccountSpec`; slaves reference it by `profile_id`. The GUI gets a "Copy profiles" group and a shared `CopySettingsWidget` used by both a new `ProfileEditor` and the slimmed `SlaveEditor`. `MainWindow` owns the profile list, persistence, one-time migration, and live reconfiguration through the existing `CopyController.apply_slave_edit` path. No worker/IPC protocol changes.

**Tech Stack:** Python ≥3.11, PySide6 ≥6.6, pytest (offscreen Qt), JSON settings in `%APPDATA%\CopyTradesMT5\settings.json`.

**Spec:** `docs/superpowers/specs/2026-11-16-copy-profiles-design.md`

## Global Constraints

- Python ≥3.11; PySide6 ≥6.6; run tests from the repo root with `python -m pytest`.
- UI copy is English, matching the existing app ("Own settings (no profile)", "Copy profiles").
- No changes to worker/IPC message types or the copy engine's decision logic.
- A profile holds exactly `SHARED_PROFILE_FIELDS`; slave identity (`id`, `terminal_path`) is never part of a profile.
- Persist materialized shared fields on every slave, so an older manager that ignores `profiles` still copies correctly.
- `settings.json` keys other than `config` (`accounts`, `provisioned_instances`, `global`) must never be rewritten or dropped.
- Keep `AccountSpec` defaults unchanged; existing configs must load.
- TDD: every task writes the failing test first, runs it, implements, reruns, commits.

## Review Focus

- **Legacy config, two or more slaves with identical settings:** they must end up on one profile after first start, with a log line, and a second start must not migrate again.
- **Legacy config without a `profiles` key but with `profile_id` on a slave** (downgrade/corruption): treat every slave as legacy, ignore stale `profile_id`, never crash.
- **Malformed profile values in `settings.json`** (`"abc"` for a number, `null` for a bool, unknown `sizing_mode`): drop the whole profile, keep attached slaves on their materialized values, log; never let a bad value reach the engine.
- **Symbol-map row order differs between two legacy slaves** (`A=X,B=Y` vs `B=Y,A=X`): they count as identical and share one profile.
- **Slave editor own → profile → own round trip:** returning to "Own settings" restores the slave's own values; a mis-click must not silently overwrite them.

---

### Task 1: CopyProfile model and shared-field helpers

**Files:**
- Modify: `manager/app/controller.py` (after `AccountSpec`, before `StatusUpdate`)
- Test: `manager/tests/test_controller.py`

**Interfaces:**
- Consumes: existing `AccountSpec` dataclass.
- Produces:
  - `SHARED_PROFILE_FIELDS: tuple[str, ...]`
  - `CopyProfile` dataclass with fields `id`, `name`, `symbol_map_csv`, `step_amount`, `step_size`, `max_lot`, `max_trade_age_minutes`, `normalize_sltp`, `sizing_mode`, `master_base_lot`, `fixed_lot` (defaults identical to `AccountSpec`).
  - `AccountSpec.profile_id: str = ""`.
  - `profile_from_spec(profile_id: str, name: str, spec: AccountSpec) -> CopyProfile`
  - `apply_profile(spec: AccountSpec, profile: CopyProfile) -> AccountSpec`
  - `new_profile_id(existing_ids: Iterable[str] = ()) -> str`
  - `unique_profile_name(name: str, existing_names: Iterable[str], *, fallback: str = "Profile") -> str`

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_controller.py`:

```python
def test_profile_from_spec_copies_only_shared_fields():
    from manager.app.controller import AccountSpec, profile_from_spec
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD", step_amount=500.0,
                       step_size=0.02, max_lot=20.0,
                       max_trade_age_minutes=5.0, normalize_sltp=False,
                       sizing_mode="fixed_lot", master_base_lot=0.2,
                       fixed_lot=0.07)
    p = profile_from_spec("p1", "Propfirm A", spec)
    assert (p.id, p.name) == ("p1", "Propfirm A")
    assert p.symbol_map_csv == "EURUSD=EURUSD"
    assert p.step_amount == 500.0 and p.step_size == 0.02
    assert p.max_lot == 20.0 and p.max_trade_age_minutes == 5.0
    assert p.normalize_sltp is False
    assert p.sizing_mode == "fixed_lot"
    assert p.master_base_lot == 0.2 and p.fixed_lot == 0.07


def test_apply_profile_preserves_identity_and_overrides_shared_fields():
    from manager.app.controller import AccountSpec, CopyProfile, apply_profile
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       step_amount=100.0, max_lot=10.0, profile_id="")
    p = CopyProfile(id="p1", name="A", symbol_map_csv="A=B",
                    step_amount=500.0, max_lot=20.0,
                    normalize_sltp=False, sizing_mode="fixed_lot",
                    fixed_lot=0.07)
    out = apply_profile(spec, p)
    assert out is not spec
    assert (out.id, out.terminal_path) == ("s1", "C:/s1/terminal64.exe")
    assert out.profile_id == "p1"
    assert out.step_amount == 500.0 and out.max_lot == 20.0
    assert out.symbol_map_csv == "A=B"
    assert out.normalize_sltp is False and out.fixed_lot == 0.07


def test_new_profile_id_is_unique_and_short():
    from manager.app.controller import new_profile_id
    a = new_profile_id()
    b = new_profile_id(existing_ids={a})
    assert a != b
    assert len(a) == 8 and len(b) == 8


def test_unique_profile_name_trims_and_suffixes():
    from manager.app.controller import unique_profile_name
    assert unique_profile_name("  Propfirm A  ", {"Other"}) == "Propfirm A"
    assert unique_profile_name("", set()) == "Profile"
    assert unique_profile_name("A", {"a"}) == "A 2"
    assert unique_profile_name("A", {"A", "A 2"}) == "A 3"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_controller.py -k "profile" -v`
Expected: FAIL (`CopyProfile` / helpers not defined).

- [ ] **Step 3: Implement the model and helpers**

In `manager/app/controller.py`:

```python
from collections.abc import Iterable
from uuid import uuid4

SHARED_PROFILE_FIELDS = (
    "symbol_map_csv", "step_amount", "step_size", "max_lot",
    "max_trade_age_minutes", "normalize_sltp", "sizing_mode",
    "master_base_lot", "fixed_lot",
)


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
```

Add `profile_id: str = ""` to `AccountSpec`. Implement the four helpers with `dataclasses.replace` for `apply_profile`. `new_profile_id` re-draws `uuid4().hex[:8]` while it is in `existing_ids`. `unique_profile_name` strips, falls back when empty, and appends `" 2"`, `" 3"`, … case-insensitively until free.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_controller.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(profiles): add CopyProfile model and shared-field helpers"
```

---

### Task 2: Profile coercion and legacy migration helper

**Files:**
- Modify: `manager/app/controller.py` (import `parse_symbol_map` from `manager.engine.transform`)
- Test: `manager/tests/test_controller.py`

**Interfaces:**
- Consumes: Task 1 (`CopyProfile`, `profile_from_spec`, `new_profile_id`, `unique_profile_name`, `SHARED_PROFILE_FIELDS`); `manager.engine.transform.parse_symbol_map`.
- Produces:
  - `coerce_profiles(raw: object, *, log: Callable[[str], None] | None = None) -> list[CopyProfile]`
  - `migrate_legacy_profiles(slaves: list[AccountSpec]) -> tuple[list[CopyProfile], dict[str, str]]`

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_controller.py`:

```python
def test_coerce_profiles_drops_malformed_and_dedupes_ids():
    from manager.app.controller import coerce_profiles
    logs = []
    raw = [
        {"id": "p1", "name": "A", "step_amount": 100.0},
        {"id": "p2", "name": "B", "step_amount": "abc"},      # bad number
        {"id": "p3", "name": "C", "normalize_sltp": "yes"},   # bad bool
        {"id": "p4", "name": "D", "sizing_mode": "nonsense"}, # bad mode
        {"id": "p1", "name": "E", "step_amount": 200.0},      # duplicate id
        "not a dict",
    ]
    got = coerce_profiles(raw, log=logs.append)
    assert [p.id for p in got] == ["p1"]
    assert got[0].name == "A" and got[0].step_amount == 100.0
    assert len(logs) == 5


def test_coerce_profiles_fixes_empty_and_duplicate_names():
    from manager.app.controller import coerce_profiles
    raw = [{"id": "p1", "name": ""}, {"id": "p2", "name": "A"},
           {"id": "p3", "name": "a"}]
    got = coerce_profiles(raw)
    assert [p.name for p in got] == ["Profile p1", "A", "a 2"]


def test_coerce_profiles_tolerates_missing_or_non_list():
    from manager.app.controller import coerce_profiles
    assert coerce_profiles(None) == []
    assert coerce_profiles({"id": "p1"}) == []


def _spec(sid, **kw):
    from manager.app.controller import AccountSpec
    return AccountSpec(id=sid, terminal_path=f"C:/{sid}/terminal64.exe", **kw)


def test_migrate_groups_identical_slaves_and_leaves_lone_alone():
    from manager.app.controller import migrate_legacy_profiles
    slaves = [
        _spec("s1", symbol_map_csv="A=X,B=Y", step_amount=100.0),
        _spec("s2", symbol_map_csv="B=Y,A=X", step_amount=100.0),  # same map
        _spec("s3", step_amount=500.0),
    ]
    profiles, mapping = migrate_legacy_profiles(slaves)
    assert len(profiles) == 1
    assert profiles[0].name == "s1 + s2"
    assert profiles[0].symbol_map_csv in ("A=X,B=Y", "B=Y,A=X")
    assert mapping == {"s1": profiles[0].id, "s2": profiles[0].id}


def test_migrate_names_three_or_more_with_count():
    from manager.app.controller import migrate_legacy_profiles
    slaves = [_spec("s1"), _spec("s2"), _spec("s3")]
    profiles, mapping = migrate_legacy_profiles(slaves)
    assert len(profiles) == 1
    assert profiles[0].name == "s1 + 2 more"
    assert set(mapping) == {"s1", "s2", "s3"}


def test_migrate_no_groups_returns_empty():
    from manager.app.controller import migrate_legacy_profiles
    profiles, mapping = migrate_legacy_profiles([_spec("s1"), _spec("s2", max_lot=5.0)])
    assert profiles == [] and mapping == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_controller.py -k "coerce or migrate" -v`
Expected: FAIL (`coerce_profiles` / `migrate_legacy_profiles` not defined).

- [ ] **Step 3: Implement coercion and migration**

`coerce_profiles`: return `[]` unless `raw` is a list. (Add `Callable` to the
`collections.abc` import in `controller.py`.) Absent fields take the
`CopyProfile` defaults; only present fields are validated. For each dict,
require non-empty string `id` and `name`; require `symbol_map_csv` to be a
`str`; convert `step_amount`, `step_size`, `max_lot`, `max_trade_age_minutes`,
`master_base_lot`, `fixed_lot` with `float()` rejecting `bool`; require
`normalize_sltp` to be a real `bool`; require `sizing_mode` in
`{"balance_step", "copy_master", "fixed_lot"}`. On failure, log
`profile <id or ?> dropped: <reason>` and skip. Drop later duplicate ids with a
log. Fix names via `unique_profile_name`, using `Profile <id[:4]>` as fallback;
log when changed.

`migrate_legacy_profiles`: build a key per slave from the six floats, `normalize_sltp`, `sizing_mode`, and `tuple(sorted(parse_symbol_map(csv).items()))` (fall back to the raw `csv` on any exception). Preserve first-appearance order. For each group with ≥2 slaves, create `CopyProfile` via `profile_from_spec` with `new_profile_id` and name `"<first> + <second>"` (2) or `"<first> + <n-1> more"` (3+), run through `unique_profile_name` against names already minted. Return `(profiles, {slave_id: profile_id})`; lone groups create nothing.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_controller.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(profiles): validate stored profiles and migrate identical legacy slaves"
```

---

### Task 3: Extract `CopySettingsWidget`

**Files:**
- Create: `manager/gui/profile_editor.py`
- Test: `manager/tests/test_profile_editor.py` (new)

**Interfaces:**
- Consumes: `SHARED_PROFILE_FIELDS`, `AccountSpec`, `CopyProfile`; `parse_symbol_map`.
- Produces: `CopySettingsWidget(QWidget)` with:
  - attributes `symbol_table`, `add_sym_button`, `del_sym_button`, `sizing_mode`, `step_amount`, `step_size`, `max_lot`, `max_trade_age_minutes`, `master_base_lot`, `fixed_lot`, `normalize_sltp`;
  - `values() -> dict[str, object]` (keys exactly `SHARED_PROFILE_FIELDS`, numbers as floats);
  - `set_values(source: Mapping[str, object] | AccountSpec | CopyProfile) -> None`;
  - `set_editable(editable: bool) -> None` (disables every input and the Add/Remove Row buttons);
  - `_symbol_map_csv() -> str` (same `master=slave,...` format as today).

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_profile_editor.py`:

```python
import pytest

pytest.importorskip("PySide6")


def test_copy_settings_widget_constructs_with_expected_fields(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    assert w.symbol_table.columnCount() == 2
    assert w.symbol_table.horizontalHeaderItem(0).text() == "Master symbol (regex)"
    assert [w.sizing_mode.itemData(i) for i in range(w.sizing_mode.count())] == [
        "balance_step", "copy_master", "fixed_lot"]
    for name in ("step_amount", "step_size", "max_lot",
                 "max_trade_age_minutes", "master_base_lot", "fixed_lot",
                 "normalize_sltp"):
        assert getattr(w, name) is not None


def test_copy_settings_widget_values_and_set_values_round_trip(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    from manager.app.controller import AccountSpec
    w = CopySettingsWidget()
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD",
                       step_amount=500.0, step_size=0.02, max_lot=20.0,
                       max_trade_age_minutes=5.0, normalize_sltp=False,
                       sizing_mode="fixed_lot", master_base_lot=0.2,
                       fixed_lot=0.07)
    w.set_values(spec)
    assert w.symbol_table.rowCount() == 2
    out = w.values()
    assert out["symbol_map_csv"] == "EURUSD=EURUSD,GBPUSD=GBPUSD"
    assert out["step_amount"] == 500.0 and out["step_size"] == 0.02
    assert out["max_lot"] == 20.0 and out["max_trade_age_minutes"] == 5.0
    assert out["normalize_sltp"] is False
    assert out["sizing_mode"] == "fixed_lot"
    assert out["master_base_lot"] == 0.2 and out["fixed_lot"] == 0.07


def test_copy_settings_widget_set_editable_disables_everything(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    w.set_editable(False)
    assert not w.symbol_table.isEnabled()
    assert not w.sizing_mode.isEnabled()
    assert not w.step_amount.isEnabled()
    assert not w.normalize_sltp.isEnabled()
    assert not w.add_sym_button.isEnabled()
    w.set_editable(True)
    assert w.step_amount.isEnabled() and w.add_sym_button.isEnabled()


def test_copy_settings_widget_visibility_follows_mode(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    w.sizing_mode.setCurrentIndex(w.sizing_mode.findData("fixed_lot"))
    assert not w.fixed_lot.isHidden()
    assert w.step_amount.isHidden() and w.master_base_lot.isHidden()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_profile_editor.py -v`
Expected: FAIL (`manager.gui.profile_editor` does not exist).

- [ ] **Step 3: Implement `CopySettingsWidget`**

Create `manager/gui/profile_editor.py`. Move the UI construction from `SlaveEditor._build_ui` for: the symbol table (0×2, headers, Add/Remove Row), the sizing `QFormLayout` with mode combo and fields, mode-dependent visibility (`_update_sizing_visibility`), the normalize checkbox, and `_symbol_map_csv` / `_add_sym_row` / `_del_sym_row`. `set_values` accepts a mapping or dataclass (use `dataclasses.fields`/`getattr`); `set_editable` calls `setEnabled` on the table, each field, and both row buttons.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_profile_editor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/profile_editor.py manager/tests/test_profile_editor.py
git commit -m "refactor(gui): extract reusable CopySettingsWidget"
```

---

### Task 4: `ProfileEditor` dialog and helpers

**Files:**
- Modify: `manager/gui/profile_editor.py`
- Test: `manager/tests/test_profile_editor.py`

**Interfaces:**
- Consumes: Task 1 (`CopyProfile`, `new_profile_id`), Task 3 (`CopySettingsWidget`).
- Produces:
  - `ProfileEditor(profile: CopyProfile | None = None, *, existing_names: Iterable[str] = (), parent=None)` with `name_edit`, `copy_settings`, `set_profile(profile)`, `profile() -> CopyProfile | None`, overridden `accept()`;
  - `new_profile(*, existing_names: Iterable[str] = (), existing_ids: Iterable[str] = (), seed: CopyProfile | None = None, parent=None) -> CopyProfile | None`;
  - `edit_profile(profile: CopyProfile, *, existing_names: Iterable[str] = (), parent=None) -> CopyProfile | None`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_profile_editor.py`:

```python
def _accept_with_name(name):
    def stub(self):
        self.name_edit.setText(name)
        self.accept()
        return self.result()
    return stub


def test_profile_editor_prepopulates_and_returns_profile(qapp):
    from manager.gui.profile_editor import ProfileEditor
    from manager.app.controller import CopyProfile
    p = CopyProfile(id="p1", name="A", symbol_map_csv="EURUSD=EURUSD",
                    step_amount=500.0, fixed_lot=0.07)
    dlg = ProfileEditor(p)
    assert dlg.name_edit.text() == "A"
    assert dlg.copy_settings.values()["step_amount"] == 500.0
    dlg.accept()
    out = dlg.profile()
    assert out.id == "p1" and out.name == "A"
    assert out.symbol_map_csv == "EURUSD=EURUSD"
    assert out.step_amount == 500.0 and out.fixed_lot == 0.07


def test_profile_editor_rejects_empty_and_duplicate_names(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor
    from PySide6.QtWidgets import QDialog, QMessageBox
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a[2] if len(a) > 2 else ""))
    dlg = ProfileEditor(existing_names={"Propfirm A"})
    dlg.name_edit.setText("   ")
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted
    dlg.name_edit.setText("propfirm a")
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert len(warnings) == 2


def test_profile_editor_rejects_non_numeric_field(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor
    from PySide6.QtWidgets import QDialog, QMessageBox
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    dlg = ProfileEditor()
    dlg.name_edit.setText("A")
    dlg.copy_settings.step_amount.setText("abc")
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert warnings


def test_new_profile_generates_id_and_uses_seed(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor, new_profile
    from manager.app.controller import CopyProfile
    monkeypatch.setattr(ProfileEditor, "exec", _accept_with_name("Neu"))
    seed = CopyProfile(id="", name="", step_amount=500.0)
    out = new_profile(seed=seed, existing_ids={"aaaaaaaa"})
    assert out is not None
    assert out.id and out.id != "aaaaaaaa"
    assert out.name == "Neu" and out.step_amount == 500.0


def test_edit_profile_returns_edited_profile(qapp, monkeypatch):
    from manager.gui.profile_editor import ProfileEditor, edit_profile
    from manager.app.controller import CopyProfile
    monkeypatch.setattr(ProfileEditor, "exec", _accept_with_name("B"))
    out = edit_profile(CopyProfile(id="p1", name="A"))
    assert out is not None and out.id == "p1" and out.name == "B"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_profile_editor.py -v`
Expected: FAIL (`ProfileEditor` not defined).

- [ ] **Step 3: Implement `ProfileEditor` and the helpers**

Build a `QFormLayout` with a `name_edit` (`QLineEdit`) plus a `CopySettingsWidget`. Override `accept()`: trim the name; reject empty or case-insensitive duplicate against `existing_names`; try `float()` on the six numeric fields; on failure call `QMessageBox.warning(self, "Invalid profile", <reason>)` and return without `super().accept()`. `profile()` returns `None` unless the result is Accepted; otherwise builds a `CopyProfile` from the name, the id (from the pre-populated profile or a fresh one), and `copy_settings.values()`.

`new_profile`: construct a `CopyProfile` seed (generated `new_profile_id(existing_ids)` plus the seed's shared/name values), pass it and `existing_names` to `ProfileEditor`, and return `.profile()` after `exec()`. `edit_profile`: pass the profile and `existing_names`, return `.profile()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_profile_editor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/profile_editor.py manager/tests/test_profile_editor.py
git commit -m "feat(gui): add CopyProfile editor dialog"
```

---

### Task 5: Slave editor profile picker and own-settings handling

**Files:**
- Modify: `manager/gui/slave_editor.py`
- Test: `manager/tests/test_slave_editor.py`

**Interfaces:**
- Consumes: Task 1 (`AccountSpec`, `CopyProfile`, `apply_profile`), Task 3 (`CopySettingsWidget`), Task 4 (`ProfileEditor`).
- Produces:
  - `SlaveEditor(controller, profiles: Iterable[CopyProfile] = (), create_profile: Callable[[CopyProfile], None] | None = None, parent=None)`
  - attributes `profile_combo`, `copy_settings`, `profile_hint`; delegating read/write properties `symbol_table`, `sizing_mode`, `step_amount`, `step_size`, `max_lot`, `max_trade_age_minutes`, `master_base_lot`, `fixed_lot`, `normalize_sltp` (preserve existing tests);
  - `add_slave(parent_window, profiles=(), create_profile=None)` and `edit_slave(parent_window, spec, profiles=(), create_profile=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_slave_editor.py`:

```python
def _profile(pid="p1", name="A", **kw):
    from manager.app.controller import CopyProfile
    return CopyProfile(id=pid, name=name, **kw)


def test_profile_combo_lists_own_settings_profiles_and_new(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController(), profiles=[_profile()])
    assert dlg.profile_combo.currentData() == ""
    assert dlg.profile_combo.findData("p1") >= 0
    assert dlg.profile_combo.findData("__new__") >= 0


def test_selecting_profile_disables_fields_and_spec_uses_profile(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController([_inst("C:/s1/terminal64.exe")]),
                      profiles=[_profile(step_amount=500.0, max_lot=20.0,
                                         symbol_map_csv="A=X")])
    dlg.set_spec(AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0, max_lot=10.0))
    dlg.profile_combo.setCurrentIndex(dlg.profile_combo.findData("p1"))
    assert not dlg.step_amount.isEnabled()
    dlg.accept()
    out = dlg.spec()
    assert out.profile_id == "p1"
    assert out.step_amount == 500.0 and out.max_lot == 20.0
    assert out.symbol_map_csv == "A=X"


def test_own_profile_own_restores_the_original_own_values(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController(), profiles=[_profile(step_amount=500.0)])
    dlg.set_spec(AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0))
    dlg.copy_settings.step_amount.setText("150")
    dlg.profile_combo.setCurrentIndex(dlg.profile_combo.findData("p1"))
    assert dlg.copy_settings.step_amount.text() == "500.0"
    dlg.profile_combo.setCurrentIndex(dlg.profile_combo.findData(""))
    assert dlg.copy_settings.step_amount.text() == "150"
    assert dlg.copy_settings.step_amount.isEnabled()


def test_detaching_a_profile_slave_keeps_the_profile_values(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController(), profiles=[_profile(step_amount=500.0)])
    dlg.set_spec(AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0, profile_id="p1"))
    dlg.profile_combo.setCurrentIndex(dlg.profile_combo.findData(""))
    assert dlg.copy_settings.step_amount.text() == "500.0"
    dlg.accept()
    assert dlg.spec().profile_id == ""


def test_new_profile_action_calls_callback_and_selects_it(qapp, monkeypatch):
    from manager.gui import slave_editor as se
    created = []
    monkeypatch.setattr(se.ProfileEditor, "exec",
                        _accept_with_name_dialog("Neu"))
    dlg = se.SlaveEditor(FakeController(), profiles=[], create_profile=created.append)
    idx = dlg.profile_combo.findData("__new__")
    dlg.profile_combo.setCurrentIndex(idx)
    dlg._on_profile_activated(idx)
    assert len(created) == 1 and created[0].name == "Neu"
    assert dlg.profile_combo.currentData() == created[0].id


def test_new_profile_cancel_reverts_to_previous_selection(qapp, monkeypatch):
    from manager.gui import slave_editor as se
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(se.ProfileEditor, "exec",
                        lambda self: QDialog.DialogCode.Rejected)
    dlg = se.SlaveEditor(FakeController(), profiles=[])
    idx = dlg.profile_combo.findData("__new__")
    dlg.profile_combo.setCurrentIndex(idx)
    dlg._on_profile_activated(idx)
    assert dlg.profile_combo.currentData() == ""


def _accept_with_name_dialog(name):
    def stub(self):
        self.name_edit.setText(name)
        self.accept()
        return self.result()
    return stub
```

Update `test_slave_editor_symbol_table_round_trips_into_csv` to use the widget:

```python
def test_slave_editor_symbol_table_round_trips_into_csv(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    dlg.symbol_table.setRowCount(1)
    dlg.symbol_table.setItem(0, 0, _qitem("EURUSD"))
    dlg.symbol_table.setItem(0, 1, _qitem("EURUSD"))
    assert "EURUSD=EURUSD" in dlg.copy_settings.values()["symbol_map_csv"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_slave_editor.py -v`
Expected: FAIL (`profile_combo` / new constructor arguments missing).

- [ ] **Step 3: Implement the profile picker**

In `_build_ui`, after the identity form add a `QFormLayout` row "Copy profile" containing a `QHBoxLayout` with `profile_combo` and a `profile_hint` `QLabel`. Replace the inline settings construction with `self.copy_settings = CopySettingsWidget()` and add delegating properties for the widget attributes the tests use. Keep `_populate_terminals`, the terminal launch button, OK/Cancel, and the title behavior.

`_reload_profiles(select_id="")` fills: item `"Own settings (no profile)"` data `""`; one item per profile (name, data id); a disabled separator; `"New profile…"` data `"__new__"`. Block signals while filling/selecting. Connect `currentIndexChanged` to `_on_profile_changed` and `activated` to `_on_profile_activated`:

- `_on_profile_changed`: when `currentData()` is `"__new__"`, return without changing anything (the action fires on activation); otherwise set `self._last_pid = data` and call `_apply_profile_selection(data)`.
- `_on_profile_activated(index)`: acts only when the current data is `"__new__"`. Build `seed = CopyProfile(id="", name="", **self.copy_settings.values())` and open `ProfileEditor(seed, existing_names=[p.name for p in self.profiles])`. On accept: append the result to `self.profiles` (a local copy), call `create_profile(profile)` when set, then `_reload_profiles(profile.id)` and `_apply_profile_selection(profile.id)`. On reject: `_reload_profiles(self._last_pid)` and `_apply_profile_selection(self._last_pid)`.
- `_apply_profile_selection(pid)`: keep `self._current_pid` (initially `""`). If `pid == self._current_pid`, only refresh editability and the hint (no value change, no snapshot). When moving `""` → a profile, snapshot `copy_settings.values()` into `self._own_snapshot`, `set_values(profile)`, `set_editable(False)`. When moving a profile → `""`, `set_editable(True)` and restore `self._own_snapshot` when the slave started as own settings; when it started attached, keep the currently displayed values (detach). Update `self._current_pid` and the hint (`Settings come from profile '<name>' — edit it under Copy profiles.`), shown only for a profile.

`set_spec` sets `self._started_attached = bool(spec.profile_id)`, clears `_own_snapshot`, sets `_current_pid` and `_last_pid` to the profile id (`""` for own settings), selects the matching item with signals blocked, fills `copy_settings` from the profile (or the spec) without snapshotting, and sets editability. `spec()` starts from `AccountSpec(id=..., terminal_path=..., profile_id=pid)` and merges `copy_settings.values()` via `dataclasses.replace`; for own settings `pid` is `""`. `add_slave` / `edit_slave` pass `profiles` and `create_profile` through.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_slave_editor.py -v`
Expected: PASS (all pre-existing tests through the delegating properties).

- [ ] **Step 5: Commit**

```bash
git add manager/gui/slave_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): attach slaves to copy profiles from the slave editor"
```

---

### Task 6: Main window "Copy profiles" group and handlers

**Files:**
- Modify: `manager/gui/main_window.py`
- Test: `manager/tests/test_main_window.py`

**Interfaces:**
- Consumes: Tasks 1, 4, 5.
- Produces on `MainWindow`: `profile_list`, `add_profile_button`, `edit_profile_button`, `duplicate_profile_button`, `delete_profile_button`; `_profiles: list[CopyProfile]`; helpers `_profile_by_id`, `_profile_names(exclude_id="")`, `_profile_tooltip`, `_slave_label`, `_refresh_profile_list(select_id=None)`, `_refresh_slave_list`; handlers `_on_add_profile`, `_on_edit_profile`, `_on_duplicate_profile`, `_on_delete_profile`, `_apply_profile_change`, `_create_profile_from_editor`.

- [ ] **Step 1: Write the failing tests**

First update the three existing `_on_edit_slave` tests: their monkeypatched
`edit_slave` lambdas must accept the new keyword arguments
(`lambda parent, spec, **kw: ...`), and the row-label assertion in
`test_on_edit_slave_updates_row_label_and_saves_config` becomes
`"s1: terminal64.exe (own settings)"`.

Append to `manager/tests/test_main_window.py`:

```python
def _copy_profile(pid="p1", name="A", **kw):
    from manager.app.controller import CopyProfile
    return CopyProfile(id=pid, name=name, **kw)


def test_profiles_group_constructs(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.profile_list is not None
    assert w.add_profile_button.text() == "New…"
    assert w.edit_profile_button.text() == "Edit…"
    assert w.duplicate_profile_button.text() == "Duplicate…"
    assert w.delete_profile_button.text() == "Delete"


def test_add_profile_appends_selects_and_saves(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    w = MainWindow(FakeController(), store=store)
    monkeypatch.setattr("manager.gui.profile_editor.new_profile",
                        lambda **kw: _copy_profile())
    w._on_add_profile()
    assert [p.id for p in w._profiles] == ["p1"]
    assert w.profile_list.currentRow() == 0
    assert store.load_config()["profiles"][0]["name"] == "A"


def test_edit_profile_updates_attached_slaves_and_applies_live(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    c = FakeController()
    w = MainWindow(c, store=store)
    w._profiles = [_copy_profile(step_amount=100.0)]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0, profile_id="p1")]
    w._refresh_profile_list("p1")
    w._refresh_slave_list()
    c.started = True  # live apply path
    monkeypatch.setattr("manager.gui.profile_editor.edit_profile",
                        lambda profile, **kw: _copy_profile(step_amount=500.0))
    w._on_edit_profile()
    assert w._profiles[0].step_amount == 500.0
    assert w._slaves[0].step_amount == 500.0
    assert [sid for sid, _ in c.applied_edits] == ["s1"]
    assert store.load_config()["slaves"][0]["step_amount"] == 500.0


def test_duplicate_profile_gets_new_id_and_unique_copy_name(
        qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    w = MainWindow(FakeController(), store=SettingsStore(tmp_path / "s.json"))
    w._profiles = [_copy_profile(name="A")]
    w._refresh_profile_list("p1")
    seen = {}
    def fake_new(**kw):
        seen.update(kw)
        return _copy_profile(pid="p2", name="A (copy)")
    monkeypatch.setattr("manager.gui.profile_editor.new_profile", fake_new)
    w._on_duplicate_profile()
    assert seen["seed"].name == "A (copy)"
    assert [p.id for p in w._profiles] == ["p1", "p2"]


def test_delete_profile_blocked_while_attached(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from PySide6.QtWidgets import QMessageBox
    w = MainWindow(FakeController())
    w._profiles = [_copy_profile()]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             profile_id="p1")]
    w._refresh_profile_list("p1")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    w._on_delete_profile()
    assert warned and [p.id for p in w._profiles] == ["p1"]


def test_delete_profile_allowed_when_unused(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._profiles = [_copy_profile()]
    w._refresh_profile_list("p1")
    w._on_delete_profile()
    assert w._profiles == []


def test_slave_label_shows_profile_name_or_own_settings(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    w = MainWindow(FakeController())
    w._profiles = [_copy_profile(name="A")]
    p = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                    profile_id="p1")
    assert w._slave_label(p) == "s1: terminal64.exe (A)"
    p.profile_id = ""
    assert w._slave_label(p) == "s1: terminal64.exe (own settings)"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_main_window.py -k "profile" -v`
Expected: FAIL (no profile widgets/helpers).

- [ ] **Step 3: Implement the group and handlers**

Initialize `self._profiles: list[CopyProfile] = []` before `_build_ui`. In `_build_ui`, create a `QGroupBox("Copy profiles")` between the master group and the slave group with a `QListWidget` and a button row (`New…`, `Edit…`, `Duplicate…`, `Delete`), add it to `root`, and wire the buttons plus double-click-to-edit. Disable Edit/Duplicate/Delete without a selection (`_update_profile_buttons`, connected to `itemSelectionChanged`).

Implement the helpers and handlers exactly as named in Interfaces, using the behavior already specified in the design. `_refresh_profile_list` adds one item per profile, shows the name as the item text, stores the profile id as item data (`Qt.UserRole`), and selects `select_id` when given.

- `_profile_tooltip` returns e.g. `"balance step · step 100.0/0.01 · max 10.0 · age 10.0 min · 2 symbols · normalize on"`.
- `_on_add_profile`: `new_profile(existing_names=self._profile_names(), existing_ids={p.id for p in self._profiles})`; append, refresh, save.
- `_on_edit_profile`: pass `existing_names=self._profile_names(exclude_id=cur.id)`; call `_apply_profile_change` on the result.
- `_apply_profile_change`: replace the profile by id; for each slave with that `profile_id`, set `self._slaves[i] = apply_profile(spec, profile)` and, when `controller.is_running()`, call `apply_slave_edit` (log and continue on exception); refresh both lists; save.
- `_on_duplicate_profile`: build `seed = dataclasses.replace(src, id="", name=unique_profile_name(f"{src.name} (copy)", self._profile_names()))`; call `new_profile(seed=seed, ...)`.
- `_on_delete_profile`: if any slave references it, `QMessageBox.warning(self, "Copy profile in use", "Detach these slaves first: " + ", ".join(ids))` and return; else remove, refresh, save.
- `_create_profile_from_editor`: append only if the id is not present; refresh; save.
- `_on_add_slave` / `_on_edit_slave`: pass `profiles=list(self._profiles)` and `create_profile=self._create_profile_from_editor`; use `_slave_label` and `_refresh_slave_list()` instead of duplicating the label formatting.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_main_window.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "feat(gui): manage copy profiles in the main window"
```

---

### Task 7: Persistence, profile resolution, and one-time legacy migration

**Files:**
- Modify: `manager/gui/main_window.py` (`_config_dict`, `_load_config`)
- Test: `manager/tests/test_main_window.py`

**Interfaces:**
- Consumes: Task 2 (`coerce_profiles`, `migrate_legacy_profiles`), Task 6 (`_profiles`, refresh helpers), `apply_profile`.
- Produces: `_config_dict()` includes `"profiles": [dataclasses.asdict(p) for p in self._profiles]`; `_load_config()` resolves `profile_id`, coerces stored profiles, and runs the migration at the end.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_main_window.py`:

```python
def test_config_round_trip_restores_profiles_and_attachments(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    w = MainWindow(FakeController(), store=store)
    w.master_terminal.addItem("C:/m/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w._profiles = [_copy_profile(step_amount=500.0)]
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=500.0, profile_id="p1")]
    w._save_config()
    w2 = MainWindow(FakeController(), store=store)
    assert [p.id for p in w2._profiles] == ["p1"]
    assert w2._slaves[0].profile_id == "p1"
    assert w2.slave_list.item(0).text() == "s1: terminal64.exe (A)"


def test_load_migrates_identical_legacy_slaves_once(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "slaves": [
            {"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
             "step_amount": 100.0},
            {"id": "s2", "terminal_path": "C:/s2/terminal64.exe",
             "step_amount": 100.0},
            {"id": "s3", "terminal_path": "C:/s3/terminal64.exe",
             "step_amount": 500.0},
        ],
    })
    w = MainWindow(FakeController(), store=store)
    assert len(w._profiles) == 1
    assert w._profiles[0].name == "s1 + s2"
    assert w._slaves[0].profile_id == w._profiles[0].id
    assert w._slaves[1].profile_id == w._profiles[0].id
    assert w._slaves[2].profile_id == ""
    assert "migrated slaves s1, s2" in w.log_view.toPlainText()
    cfg = store.load_config()
    assert len(cfg["profiles"]) == 1
    assert cfg["master"]["terminal_path"] == "C:/m/terminal64.exe"
    w2 = MainWindow(FakeController(), store=store)
    assert len(w2._profiles) == 1  # second start does not migrate again


def test_malformed_profile_drops_and_slave_falls_back(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "profiles": [{"id": "p1", "name": "A", "step_amount": "abc"}],
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "profile_id": "p1", "step_amount": 123.0}],
    })
    w = MainWindow(FakeController(), store=store)
    assert w._profiles == []
    assert w._slaves[0].profile_id == ""
    assert w._slaves[0].step_amount == 123.0
    assert "dropped" in w.log_view.toPlainText()


def test_unknown_profile_id_falls_back_to_stored_settings(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "profiles": [],
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "profile_id": "gone", "step_amount": 123.0}],
    })
    w = MainWindow(FakeController(), store=store)
    assert w._slaves[0].profile_id == ""
    assert w._slaves[0].step_amount == 123.0
    assert "not found" in w.log_view.toPlainText()


def test_legacy_config_with_stale_profile_id_is_treated_as_legacy(
        qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({
        "master": {"terminal_path": "C:/m/terminal64.exe"},
        "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                    "profile_id": "gone", "step_amount": 100.0}],
    })
    w = MainWindow(FakeController(), store=store)
    assert w._profiles == []
    assert w._slaves[0].profile_id == ""
    assert store.load_config()["profiles"] == []  # migration marked as done
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest manager/tests/test_main_window.py -k "migrat or profile or fallback or round_trip" -v`
Expected: FAIL (no `profiles` in the saved config; no migration).

- [ ] **Step 3: Implement persistence and migration**

`_config_dict`: add `"profiles": [dataclasses.asdict(p) for p in self._profiles]` next to `slaves`. Slaves already serialize `profile_id` through `dataclasses.asdict`.

`_load_config`: at the start, `had_profiles_key = isinstance(cfg, dict) and "profiles" in cfg` and load `self._profiles = coerce_profiles(cfg.get("profiles") if isinstance(cfg, dict) else None, log=self.append_log)`. Keep the existing master, slave, and autostart restoration. For each slave, after `AccountSpec(**kwargs)`: if `spec.profile_id` is non-empty, look it up; if found, `self._slaves.append(apply_profile(spec, profile))`; if not, clear `profile_id` and log `profile <id> for slave <sid> not found — using stored settings`.

At the **end** of `_load_config` (after the autostart section, so a save cannot clobber the toggles), when `not had_profiles_key`: run `migrate_legacy_profiles(self._slaves)`; extend `self._profiles`; for each mapping entry, replace the slave with `apply_profile(slave, profile)`; log one `migrated slaves <ids> into profile '<name>'` line per profile; refresh both lists; call `_save_config()` (this also writes `profiles: []` when nothing qualified). Finally call `_refresh_slave_list()` so labels reflect attachments.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest manager/tests/test_main_window.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "feat(profiles): persist profiles and migrate legacy configs once"
```

---

### Task 8: Documentation and full-suite verification

**Files:**
- Modify: `README.md` (Usage step 4, Features list)
- Test: full suite

**Interfaces:**
- Consumes: all previous tasks.
- Produces: user-facing documentation for profiles and migration.

- [ ] **Step 1: Update the README**

In **Usage** step 4 (Slaves), replace the per-slave-settings wording with: create a **Copy profile** under the new **Copy profiles** group (New…), then select it in **Add Slave… / Edit Slave…**; a profile holds the symbol map and all lot-sizing/normalization settings, and editing it updates every attached slave live while copying. Keep "Own settings (no profile)" documented for standalone slaves. Add one sentence: after updating from a version without profiles, slaves that already had identical settings are grouped into one profile automatically (once); lone slaves stay on "Own settings".

Add a Features bullet: `- **Copy profiles** — define symbol map + lot sizing once, attach many slaves, edit once (live while copying).`

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`
Expected: all tests pass (no failures/errors).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(profiles): document copy profiles and legacy migration"
```

---

## Self-Review Notes

- Spec coverage: data model (Task 1), validation/migration helper (Task 2),
  shared widget (Task 3), editor (Task 4), slave picker/own-settings (Task 5),
  main-window group/handlers (Task 6), persistence + migration + live apply
  (Tasks 6–7), README (Task 8).
- The design's "profile editor stays open on invalid input" is tested in Task 4;
  own → profile → own is tested in Task 5; malformed profiles and stale
  `profile_id` are tested in Tasks 2 and 7.

