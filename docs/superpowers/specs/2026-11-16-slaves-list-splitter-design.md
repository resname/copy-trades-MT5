# Resizable slaves list — Design

Date: 2026-11-16
Status: Proposed (design)

## Intent

The main window's slave list shares the window's spare vertical space with the
Copy-profiles list and the log. With many slaves the user can only see a few
rows and has no way to give the list more room without also stretching
everything else. The user wants an explicit way to enlarge the slave list.

## Goal

Give the Slaves group an interactive, persisted height: a draggable divider
between the Slaves group and the lower block (Auto-start, Start/Stop, updates,
Status, Log). Dragging down enlarges the list; the lower block gives up the
space. The chosen position survives restarts.

## Non-goals

- Independently resizing the Copy-profiles list (it stays as today).
- A pop-out/detached slave window.
- Making a slave-list maximization toggle (variant C).
- Changing Start/Stop or copy behavior.

## Approaches considered

- **A — Vertical splitter between the Slaves group and the lower block
  (chosen).** Targeted at the requested list, standard Qt interaction, small
  change, position can be persisted.
- **B — Larger default height + stretch priority.** No new control; the list
  starts taller and absorbs window growth before the profile list/log. Rejected
  because it is not interactive.
- **C — "Maximize list" button.** Hides Auto-start/Status/Log; all-or-nothing
  and adds a control. Rejected.

## Design

In `MainWindow._build_ui`:

- Create `self.content_splitter = QSplitter(Qt.Orientation.Vertical)`.
- Top pane: the existing `slave_box` (`QGroupBox("Slaves")`) with the list and
  its buttons.
- Bottom pane: a plain `QWidget` (`self.lower_panel`) with a `QVBoxLayout`
  holding the existing widgets, in their current order: `autostart_box`,
  `controls`, `updates_row`, `update_progress`, "Status" label,
  `status_view`, "Log" label, `log_view`.
- Root layout becomes: `master_box`, `profile_box`, `content_splitter`.
  Master and Copy profiles keep their current fixed position; the splitter
  fills the rest of the window.
- `content_splitter.setChildrenCollapsible(False)`;
  `setStretchFactor(0, 1)` (Slaves absorbs window growth) and
  `setStretchFactor(1, 0)`.
- `self.slave_list.setMinimumHeight(80)` so the list cannot be collapsed to a
  sliver.
- Default splitter sizes: `[240, 420]` (Slaves, lower block), applied only when
  no saved sizes exist.

Persistence (`manager/gui/main_window.py`, `settings.json` under `config`):

```json
"ui": {"splitter_sizes": [240, 420]}
```

- `_config_dict()` adds `"ui": {"splitter_sizes": [int(s) for s in
  self.content_splitter.sizes()]}`.
- `_load_config()` reads `cfg.get("ui")`; accepts only a list/tuple of exactly
  two non-negative ints whose sum is positive, then calls
  `content_splitter.setSizes([...])`. Anything else is ignored with a log line.
- Existing save points (start, edits, quit) persist the current position; no
  new signal wiring is required.

## Constraints

- No changes to profile/slave data, engine, worker, or IPC.
- Existing widget attributes (`autostart_box`, `status_view`, `log_view`, …)
  stay on `MainWindow`; only their parent layout changes.
- GUI tests keep running offscreen; no real terminals/tasks.

## Testing

- `test_main_window.py`:
  - the splitter exists, is vertical, non-collapsible, and contains the slaves
    group and the lower panel;
  - `_config_dict()` exposes `ui.splitter_sizes` with two ints;
  - saving a chosen size and constructing a new window restores it;
  - malformed/absent saved sizes fall back to defaults without raising;
  - giving the slaves pane more size increases the visible `slave_list` height
    after show/processEvents (or at least the splitter's sizes reflect it).
- Existing suite must stay green.

## What's left to the builder

- Exact default sizes and whether to expose `slave_list.setMinimumHeight(80)`
  as a named constant.
- README wording (one line in Usage: drag the divider to enlarge the list).

## Release scope

`v0.1.38` contains this change plus the already-implemented long-name layout
fix (combo/hint width caps and full-text tooltips).
