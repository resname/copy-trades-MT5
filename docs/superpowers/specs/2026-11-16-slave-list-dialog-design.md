# Slave-list dialog (replaces the splitter) — Design

Date: 2026-11-16
Status: Proposed (design)

## Intent

The v0.1.38 splitter does not give the user a usable way to enlarge the slave
list. Measured root cause: the lower block (Auto-start, controls, Status, Log)
has a 360 px minimum height and the splitter handle is only 4 px tall. At a
900 px window the list moves from 83 to 143 px; at 700 px it cannot move at
all. The interaction is structurally too constrained.

Replace it with a dedicated, large slave-list window.

## Goal

A **Open slave list…** button in the Slaves group opens a modal dialog with a
large list and the same Add / Remove / Edit actions. Changes go through the
existing main-window handlers, so persistence, live apply and profile
attachment behave exactly as today. The main window list stays unchanged.

## Non-goals

- A non-modal list window synchronized permanently with the main window.
- A larger Copy-profiles list (that list stays as today).
- Changing slave/copy behavior, workers, IPC, or the profile model.
- Keeping the splitter as an alternative.

## Approaches considered

- **A — Modal "Slave list" dialog with Add/Remove/Edit (chosen).** Simple,
  no second source of truth, full dialog height, reuses existing handlers.
- **B — Non-modal synchronized window.** Best for leaving open beside the
  manager, but needs selection/action sync in both directions and more tests.
  Rejected for this round.
- **C — "Maximize list" toggle in the main window.** No popup; hides the lower
  block. Rejected because it is all-or-nothing and still not a large view.

The splitter is removed entirely: `QSplitter`, `DEFAULT_SPLITTER_SIZES`, the
`lower_panel` wrapper, the 80 px list minimum, and `ui.splitter_sizes`
persistence. Root layout returns to the pre-v0.1.38 order. Old `settings.json`
files may still contain `ui`; it is ignored.

## Design

### Main-window refactor (`manager/gui/main_window.py`)

Extract selection-independent helpers so the dialog and the main list share one
implementation:

- `_remove_slave_at(row: int) -> bool` — remove the slave row, save, refresh.
- `_remove_slave_by_id(slave_id: str) -> bool` — find the row by id, delegate.
- `_edit_slave_at(row: int) -> bool` — open the slave editor, replace on
  accept, refresh, save, live-apply when running; returns whether a change was
  applied.
- `_edit_slave_by_id(slave_id: str) -> bool` — find the row by id, delegate.
- Existing `_on_remove_slave()` / `_on_edit_slave()` resolve the current main
  row and delegate, preserving current behavior and tests.
- `_on_add_slave()` gains a return value: the appended `AccountSpec` on success,
  else `None` (the dialog selects the new row).
- Add `self.open_slave_list_button = QPushButton("Open slave list…")` to the
  Slaves button row; its handler calls `open_slave_list(self)`.

### New dialog (`manager/gui/slave_list_dialog.py`)

- `SlaveListDialog(QDialog, manager)`:
  - modal, resizable, title "Slaves", default size 640×600 (large list area).
  - `list_widget: QListWidget`; each item text is `manager._slave_label(spec)`,
    item data is the slave id, item tooltip is the full label.
  - buttons: **Add Slave…**, **Remove Slave**, **Edit Slave…**, **Close**;
    double-click edits; Remove/Edit enabled only with a selection.
  - `_refresh()` rebuilds from `manager._slaves` and preserves the selected
    slave id; after Add, select the returned spec's id.
  - Add calls `manager._on_add_slave()`; Remove calls
    `manager._remove_slave_by_id(selected_id)`; Edit calls
    `manager._edit_slave_by_id(selected_id)`. The main window's handlers save
    the config and live-apply edits, so the dialog only refreshes its view.
- `open_slave_list(parent_window) -> None`: create the dialog against
  `parent_window`, `exec()` it.

The dialog is a view + action surface; it never owns slave specs.

### Tests

Remove the four splitter tests. Add:

- `test_slave_list_dialog_lists_labels_and_tooltips` — items match
  `_slave_label`; tooltips full text; selection enables buttons.
- `test_slave_list_dialog_add_selects_new_slave` — monkeypatched
  `_on_add_slave` returns a spec; the dialog refreshes and selects it.
- `test_slave_list_dialog_remove_and_edit_delegate_by_id` — remove/edit call
  the manager helpers with the selected id, then refresh.
- `test_slave_list_dialog_double_click_edits` — double-click delegates to the
  edit helper.
- `test_open_slave_list_button_opens_dialog` — button click constructs a
  `SlaveListDialog` (monkeypatched `exec` returns immediately).
- `test_remove_and_edit_by_id_operate_on_the_right_slave` — main-window
  helpers update the intended spec, save, and live-apply edits.
- `test_old_ui_config_is_ignored` — a stored `ui.splitter_sizes` does not raise
  and is not written back.

Existing main-window tests must stay green. Full suite with
`py -3.12 -m pytest -q`.

### README

Replace the "drag the divider" sentence with: **Open slave list…** opens a
large modal list with the same add/remove/edit actions.

## Release scope

`v0.1.39`: removes the splitter, adds the slave-list dialog; no data or copy
behavior changes.

## What's left to the builder

- Exact dialog margins/spacing; use the project's zero-margin pattern where
  embedding widgets.
- Whether to remember the dialog's last size in-session (optional; not
  required).
