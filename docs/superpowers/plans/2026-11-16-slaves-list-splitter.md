# Resizable Slaves List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the slaves list an interactive, persisted height via a vertical splitter between the Slaves group and the lower window block.

**Architecture:** `MainWindow._build_ui` places the existing Slaves group and a new lower panel (Auto-start, controls, updates, Status, Log) inside a vertical `QSplitter`. The splitter sizes serialize under `config.ui.splitter_sizes`; loading validates them and falls back to defaults.

**Tech Stack:** Python ≥3.11, PySide6 ≥6.6, pytest (offscreen Qt), JSON settings in `%APPDATA%\CopyTradesMT5\settings.json`.

**Spec:** `docs/superpowers/specs/2026-11-16-slaves-list-splitter-design.md`

## Global Constraints

- Python ≥3.11; run tests from the repo root with `py -3.12 -m pytest`.
- No changes to profile/slave data, engine, worker, or IPC.
- Existing widget attributes (`slave_list`, `autostart_box`, `status_view`, `log_view`, buttons, …) stay on `MainWindow`; only their parent layout changes.
- The splitter must be non-collapsible and the list must keep at least 80 px.
- Invalid/absent saved sizes never raise; defaults apply.
- Existing suite stays green.
- README stays English; UI copy unchanged.

## Review Focus

- **Malformed `ui.splitter_sizes`** (string, wrong length, negative, bool, zero sum): ignored with a log line, defaults remain, no exception at startup.
- **Old `settings.json` without `ui`**: loads normally; default splitter sizes apply.
- **Very short window / splitter clamps**: the layout still shows every group; no section disappears.
- **Drag then quit**: the current sizes are persisted by the existing save points (including `aboutToQuit`).
- **Reparented widgets**: Status/Log/Auto-start/Start-Stop still behave and are reachable after moving into `lower_panel`.

---

### Task 0: Commit the already-verified long-name fix

The long-name layout fix is implemented in the working tree and verified (4 regression tests red→green, full suite 517 passed / 1 skipped). It ships in the same release, so commit it before starting the splitter work.

**Files:** `manager/gui/main_window.py`, `manager/gui/profile_editor.py`, `manager/gui/slave_editor.py`, `manager/tests/test_main_window.py`, `manager/tests/test_slave_editor.py`

- [ ] **Step 1: Re-run the full suite**

Run: `py -3.12 -m pytest -q`
Expected: 517 passed, 1 skipped.

- [ ] **Step 2: Commit**

```bash
git add manager/gui/main_window.py manager/gui/profile_editor.py manager/gui/slave_editor.py manager/tests/test_main_window.py manager/tests/test_slave_editor.py
git commit -m "fix(gui): keep long names from widening dialogs and add full-text tooltips"
```

---

### Task 1: Splitter layout, persistence, README

**Files:**
- Modify: `manager/gui/main_window.py`
- Test: `manager/tests/test_main_window.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: existing `MainWindow` widgets (`slave_list`, `autostart_box`, `start_button`, `status_view`, `log_view`, …).
- Produces: `self.slave_box`, `self.lower_panel`, `self.content_splitter`; `_config_dict()` includes `"ui": {"splitter_sizes": [int, int]}`; `_load_config()` applies validated sizes.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_main_window.py`:

```python
def test_content_splitter_holds_slaves_and_lower_panel(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSplitter
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert isinstance(w.content_splitter, QSplitter)
    assert w.content_splitter.orientation() == Qt.Orientation.Vertical
    assert w.content_splitter.childrenCollapsible() is False
    assert w.content_splitter.widget(0) is w.slave_box
    assert w.content_splitter.widget(1) is w.lower_panel
    assert w.slave_list.minimumHeight() >= 80


def test_splitter_sizes_round_trip(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    w = MainWindow(FakeController(), store=store)
    w.content_splitter.setSizes([500, 200])
    w._save_config()
    assert store.load_config()["ui"]["splitter_sizes"] == [500, 200]
    w2 = MainWindow(FakeController(), store=store)
    w2.resize(900, 1400)
    w2.show()
    qapp.processEvents()
    sizes = w2.content_splitter.sizes()
    assert abs(sizes[0] - 500) <= 80 and abs(sizes[1] - 200) <= 80


def test_malformed_splitter_sizes_fall_back(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(tmp_path / "s.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "profiles": [], "slaves": [],
                       "ui": {"splitter_sizes": ["x", -1]}})
    w = MainWindow(FakeController(), store=store)
    sizes = w.content_splitter.sizes()
    assert len(sizes) == 2 and sum(sizes) > 0
    assert "splitter" in w.log_view.toPlainText().lower()


def test_giving_the_slaves_pane_more_size_enlarges_the_list(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    w = MainWindow(FakeController())
    w._slaves = [AccountSpec(id=f"s{i}",
                             terminal_path=f"C:/s{i}/terminal64.exe")
                 for i in range(30)]
    w._refresh_slave_list()
    w.resize(900, 1600)
    w.show()
    qapp.processEvents()
    w.content_splitter.setSizes([700, 150])
    qapp.processEvents()
    big = w.slave_list.height()
    w.content_splitter.setSizes([120, 730])
    qapp.processEvents()
    small = w.slave_list.height()
    assert big > small
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -k "splitter or slaves_pane" -v`
Expected: FAIL (`content_splitter` / `slave_box` / `lower_panel` missing).

- [ ] **Step 3: Implement the splitter and persistence**

In `manager/gui/main_window.py`:

- Add `QSplitter` to the `PySide6.QtWidgets` import.
- Add module constant `DEFAULT_SPLITTER_SIZES = (240, 420)`.
- In `_build_ui`:
  - keep `self.slave_box = slave_box` (the existing `QGroupBox("Slaves")`), and add `self.slave_list.setMinimumHeight(80)`.
  - create `self.lower_panel = QWidget()` with a `QVBoxLayout`; move the existing `autostart_box`, `controls`, `updates_row`, `update_progress`, "Status" label, `status_view`, "Log" label and `log_view` additions into it (same order).
  - create `self.content_splitter = QSplitter(Qt.Orientation.Vertical)`, add `self.slave_box` and `self.lower_panel`, `setChildrenCollapsible(False)`, `setStretchFactor(0, 1)`, `setStretchFactor(1, 0)`, `setSizes(list(DEFAULT_SPLITTER_SIZES))`.
  - root layout: `master_box`, `profile_box`, `self.content_splitter` (instead of the individual lower widgets).
- `_config_dict`: add `"ui": {"splitter_sizes": [int(s) for s in self.content_splitter.sizes()]}` next to `"autostart"`.
- `_load_config`: after the autostart section, read `ui = cfg.get("ui")` when `cfg` is a dict. Accept only a list/tuple of exactly two non-negative ints (bools excluded) with a positive sum; call `self.content_splitter.setSizes([int(a), int(b)])`. Otherwise, log `invalid ui.splitter_sizes ignored` (only when a `ui` value was present) and keep the defaults. Never raise.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -3.12 -m pytest manager/tests/test_main_window.py -v`
Expected: PASS.

- [ ] **Step 5: Update the README**

In the Usage section (near the Slaves step), add one sentence: drag the divider between the Slaves list and the lower panel to give the list more room; the position is remembered.

- [ ] **Step 6: Run the full suite**

Run: `py -3.12 -m pytest -q`
Expected: all pass (currently 517 passed, 1 skipped; the new tests raise the count).

- [ ] **Step 7: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py README.md docs/superpowers/specs/2026-11-16-slaves-list-splitter-design.md docs/superpowers/plans/2026-11-16-slaves-list-splitter.md
git commit -m "feat(gui): make the slaves list resizable and persist its height"
```
