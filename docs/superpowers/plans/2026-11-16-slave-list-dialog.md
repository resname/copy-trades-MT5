# Slave-List Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the unusable splitter and add a modal, large "Slave list" dialog with Add / Remove / Edit that shares the main window's handlers.

**Architecture:** `MainWindow` regains its pre-splitter layout and exposes selection-independent helpers (`_remove_slave_by_id`, `_edit_slave_by_id`, `_on_add_slave` returning the added spec). A new `SlaveListDialog` renders `_slaves` with `_slave_label`, delegates actions to those helpers, and refreshes after each change. No slave data or copy logic changes.

**Tech Stack:** Python ≥3.11, PySide6 ≥6.6, pytest (offscreen Qt), JSON settings.

**Spec:** `docs/superpowers/specs/2026-11-16-slave-list-dialog-design.md`

## Global Constraints

- Python ≥3.11; tests from the repo root with `py -3.12 -m pytest`.
- No changes to slave/profile data, engine, worker, or IPC.
- The dialog is a view + action surface; `MainWindow._slaves` stays the single source of truth.
- Save and live-apply stay in `MainWindow` handlers.
- Old `settings.json` `ui` keys are ignored and never written back.
- UI copy English; existing widget attributes stay on `MainWindow`.
- Full suite green.

## Review Focus

- **Old config with `ui.splitter_sizes`**: loads without raising, and a subsequent save does not write `ui` back.
- **Dialog with zero slaves**: empty list, Remove/Edit disabled, Add still works.
- **Add while the dialog is open**: the new slave appears in the dialog and is selected; the main list also refreshes.
- **Remove/Edit while copying**: live apply targets the selected slave id only.
- **Selection after a refresh**: stays on the same slave where possible; button enablement follows it.

---

### Task 1: Remove the splitter and its persistence

**Files:**
- Modify: `manager/gui/main_window.py`
- Test: `manager/tests/test_main_window.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: root layout with `master_box`, `profile_box`, `self.slave_box`, `self.autostart_box`, controls, updates, Status, Log; `_config_dict()` without `ui`; `_load_config()` without the splitter-size block.

- [ ] **Step 1: Update the tests**

Delete `test_content_splitter_holds_slaves_and_lower_panel`, `test_splitter_sizes_round_trip`, `test_malformed_splitter_sizes_fall_back`, `test_out_of_range_splitter_sizes_fall_back`, and `test_giving_the_slaves_pane_more_size_enlarges_the_list`.

Append:

```python
def test_old_ui_config_is_ignored_and_not_rewritten(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "profiles": [], "slaves": [],
                       "ui": {"splitter_sizes": [999, 1]}})
    w = MainWindow(FakeController(), store=store)  # must not raise
    w._save_config()
    cfg = store.load_config()
    assert "ui" not in cfg
    assert cfg["master"]["terminal_path"] == "C:/m/terminal64.exe"
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -k "old_ui_config" -v`
Expected: FAIL (`ui` is still written by `_config_dict`).

- [ ] **Step 3: Implement the revert**

In `manager/gui/main_window.py`:
- Remove `QSplitter` from the imports and `DEFAULT_SPLITTER_SIZES`.
- Remove `self.slave_list.setMinimumHeight(80)`.
- Remove `self.lower_panel` and its layout; add the widgets back to the root layout in the pre-splitter order after `self.slave_box`: `self.autostart_box`, `controls`, `updates_row`, `self.update_progress`, `QLabel("Status")`, `self.status_view`, `QLabel("Log")`, `self.log_view`. Remove `self.content_splitter` and its configuration.
- Remove the `"ui"` entry from `_config_dict()`.
- Remove the `ui_cfg` validation block from `_load_config()`.
- Keep `self.slave_box` (the attribute is used by tests and the dialog).

- [ ] **Step 4: Run the tests to verify GREEN**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "revert(gui): remove the splitter and its persisted sizes"
```

---

### Task 2: Selection-independent slave helpers

**Files:**
- Modify: `manager/gui/main_window.py`
- Test: `manager/tests/test_main_window.py`

**Interfaces:**
- Consumes: existing `_on_add_slave`, `_refresh_slave_list`, `_save_config`, `slave_editor.edit_slave`.
- Produces:
  - `_on_add_slave() -> AccountSpec | None` (returns the appended spec)
  - `_remove_slave_at(row: int) -> bool`
  - `_remove_slave_by_id(slave_id: str) -> bool`
  - `_edit_slave_at(row: int) -> bool`
  - `_edit_slave_by_id(slave_id: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_main_window.py`:

```python
def test_on_add_slave_returns_the_added_spec(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    import manager.gui.slave_editor as se
    w = MainWindow(FakeController())
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe")
    monkeypatch.setattr(se, "add_slave", lambda parent, **kw: spec)
    assert w._on_add_slave() is spec
    assert w._slaves == [spec]
    monkeypatch.setattr(se, "add_slave", lambda parent, **kw: None)
    assert w._on_add_slave() is None


def test_remove_slave_by_id_removes_the_right_one_and_saves(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    w = MainWindow(FakeController(), store=SettingsStore(tmp_path / "s.json"))
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe"),
                 AccountSpec(id="s2", terminal_path="C:/s2/terminal64.exe")]
    w._refresh_slave_list()
    assert w._remove_slave_by_id("s2") is True
    assert [s.id for s in w._slaves] == ["s1"]
    assert w.slave_list.count() == 1
    assert w._remove_slave_by_id("gone") is False


def test_edit_slave_by_id_updates_the_right_one_and_applies_live(
        qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    import manager.gui.slave_editor as se
    c = FakeController()
    c.started = True
    w = MainWindow(c)
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                             step_amount=100.0),
                 AccountSpec(id="s2", terminal_path="C:/s2/terminal64.exe",
                             step_amount=100.0)]
    w._refresh_slave_list()
    edited = AccountSpec(id="s2", terminal_path="C:/s2/terminal64.exe",
                          step_amount=500.0)
    monkeypatch.setattr(se, "edit_slave",
                        lambda parent, spec, **kw: edited)
    assert w._edit_slave_by_id("s2") is True
    assert w._slaves[1].step_amount == 500.0
    assert w.slave_list.item(1).text().startswith("s2:")
    assert c.applied_edits == [("s2", edited)]
    assert w._edit_slave_by_id("gone") is False
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -k "by_id or returns_the_added" -v`
Expected: FAIL (helpers/return values missing).

- [ ] **Step 3: Implement the helpers**

In `manager/gui/main_window.py`:
- `_on_add_slave` returns `spec` on success and `None` on cancel; keep the existing append/refresh/select/save.
- `_remove_slave_at(row)`: return `False` when the row is out of range; otherwise `del self._slaves[row]`, `_refresh_slave_list()`, `_save_config()`, return `True`.
- `_remove_slave_by_id(slave_id)`: find the index by `spec.id`, delegate to `_remove_slave_at` (return `False` when absent).
- `_edit_slave_at(row)`: move the existing `_on_edit_slave` body here; return `False` for no row/cancel/error, `True` after replacing, refreshing, saving and (when running) `apply_slave_edit`.
- `_edit_slave_by_id(slave_id)`: find the index, delegate.
- `_on_remove_slave` / `_on_edit_slave` keep their signatures and delegate using `self.slave_list.currentRow()`.

- [ ] **Step 4: Run the tests to verify GREEN**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "refactor(gui): add slave remove/edit helpers addressable by id"
```

---

### Task 3: Slave-list dialog, button, README

**Files:**
- Create: `manager/gui/slave_list_dialog.py`
- Modify: `manager/gui/main_window.py`
- Modify: `README.md`
- Test: `manager/tests/test_main_window.py`

**Interfaces:**
- Consumes: Task 2 helpers; `MainWindow._slaves`, `_slave_label(spec)`.
- Produces: `SlaveListDialog(manager, parent=None)` with `list_widget`, `add_button`, `remove_button`, `edit_button`, `close_button`; `open_slave_list(parent_window) -> None`; `MainWindow.open_slave_list_button` and `_on_open_slave_list`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_main_window.py`:

```python
def _slave(sid):
    from manager.app.controller import AccountSpec
    return AccountSpec(id=sid, terminal_path=f"C:/{sid}/terminal64.exe")


def test_slave_list_dialog_lists_labels_tooltips_and_selection(qapp):
    from manager.gui.main_window import MainWindow
    from manager.gui.slave_list_dialog import SlaveListDialog
    w = MainWindow(FakeController())
    w._slaves = [_slave("s1"), _slave("s2")]
    w._refresh_slave_list()
    dlg = SlaveListDialog(w)
    assert dlg.list_widget.count() == 2
    assert dlg.list_widget.item(1).text() == w._slave_label(w._slaves[1])
    assert dlg.list_widget.item(1).toolTip() == w._slave_label(w._slaves[1])
    assert not dlg.remove_button.isEnabled()
    assert not dlg.edit_button.isEnabled()
    dlg.list_widget.setCurrentRow(1)
    assert dlg.remove_button.isEnabled() and dlg.edit_button.isEnabled()


def test_slave_list_dialog_with_no_slaves_is_empty(qapp):
    from manager.gui.main_window import MainWindow
    from manager.gui.slave_list_dialog import SlaveListDialog
    dlg = SlaveListDialog(MainWindow(FakeController()))
    assert dlg.list_widget.count() == 0
    assert not dlg.remove_button.isEnabled()
    assert not dlg.edit_button.isEnabled()


def test_slave_list_dialog_add_selects_the_new_slave(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui.slave_list_dialog import SlaveListDialog
    from PySide6.QtCore import Qt
    w = MainWindow(FakeController())
    spec = _slave("s1")

    def add():
        w._slaves.append(spec)
        return spec
    monkeypatch.setattr(w, "_on_add_slave", add)
    dlg = SlaveListDialog(w)
    dlg.add_button.click()
    assert dlg.list_widget.count() == 1
    assert dlg.list_widget.currentItem().data(Qt.ItemDataRole.UserRole) == "s1"


def test_slave_list_dialog_remove_and_edit_delegate_by_id(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui.slave_list_dialog import SlaveListDialog
    w = MainWindow(FakeController())
    w._slaves = [_slave("s1")]
    calls = []
    monkeypatch.setattr(w, "_remove_slave_by_id",
                        lambda sid: calls.append(("remove", sid)) or True)
    monkeypatch.setattr(w, "_edit_slave_by_id",
                        lambda sid: calls.append(("edit", sid)) or True)
    dlg = SlaveListDialog(w)
    dlg.list_widget.setCurrentRow(0)
    dlg.remove_button.click()
    dlg.list_widget.setCurrentRow(0)
    dlg.edit_button.click()
    assert calls == [("remove", "s1"), ("edit", "s1")]


def test_slave_list_dialog_double_click_edits(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui.slave_list_dialog import SlaveListDialog
    w = MainWindow(FakeController())
    w._slaves = [_slave("s1")]
    calls = []
    monkeypatch.setattr(w, "_edit_slave_by_id",
                        lambda sid: calls.append(sid) or True)
    dlg = SlaveListDialog(w)
    dlg.list_widget.itemDoubleClicked.emit(dlg.list_widget.item(0))
    assert calls == ["s1"]


def test_open_slave_list_button_opens_the_dialog(qapp, monkeypatch):
    import manager.gui.slave_list_dialog as sld
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    opened = []

    class _D:
        def __init__(self, manager, parent=None):
            opened.append(manager)

        def exec(self):
            return 0
    monkeypatch.setattr(sld, "SlaveListDialog", _D)
    w.open_slave_list_button.click()
    assert opened == [w]
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -k "slave_list_dialog or opens_the_dialog" -v`
Expected: FAIL (`manager.gui.slave_list_dialog` / `open_slave_list_button` missing).

- [ ] **Step 3: Implement the dialog and button**

Create `manager/gui/slave_list_dialog.py` with `SlaveListDialog(QDialog)`:
- constructor `(manager, parent=None)`: modal, title "Slaves", `resize(640, 600)`, vertical layout containing a `QListWidget` (stretch 1) and a button row (`Add Slave…`, `Remove Slave`, `Edit Slave…`, `Close`).
- `_refresh(select_id="")`: preserve the selected id when `select_id` is empty; rebuild items with `manager._slave_label(spec)`, item data `Qt.ItemDataRole.UserRole = spec.id`, tooltip = label; restore the selection; call `_update_buttons()`.
- `_selected_id() -> str` (empty when no selection); `_update_buttons()` enables Remove/Edit only with a selection.
- Add: `spec = self._manager._on_add_slave()`; if not `None`, `_refresh(spec.id)`; else `_refresh()`.
- Remove: `sid = self._selected_id()`; if set, `self._manager._remove_slave_by_id(sid)`; `_refresh()`.
- Edit: `sid = self._selected_id()`; if set, `self._manager._edit_slave_by_id(sid)`; `_refresh()`.
- Double-click on the list calls the edit path; `close_button` rejects; selection changes call `_update_buttons`.
- Module helper `open_slave_list(parent_window)`: create `SlaveListDialog(parent_window, parent=parent_window)` and `exec()` it.

In `manager/gui/main_window.py`: add `self.open_slave_list_button = QPushButton("Open slave list…")` to the Slaves button row, wire it to `_on_open_slave_list`, which lazily imports and calls `open_slave_list(self)`.

- [ ] **Step 4: Update the README**

Replace the sentence about dragging the divider with: **Open slave list…** opens a large modal list with the same Add / Remove / Edit actions.

- [ ] **Step 5: Run the tests to verify GREEN**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `py -3.12 -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add manager/gui/main_window.py manager/gui/slave_list_dialog.py manager/tests/test_main_window.py README.md docs/superpowers/specs/2026-11-16-slave-list-dialog-design.md docs/superpowers/plans/2026-11-16-slave-list-dialog.md
git commit -m "feat(gui): open a large slave list dialog"
```
