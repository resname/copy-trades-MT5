# Single-Window Quit-on-Close (hide console, remove tray)

Date: 2026-08-04
Status: Approved (design)

## Problem

The app currently opens **two windows**: a console window (because the Start
Menu shortcut and `copytrades` shim launch `python.exe`, which is
console-attached) and the Qt GUI. The close behavior does not match the user's
expectation:

- **GUI close** (`manager/gui/main_window.py:399-404`) is intercepted —
  `event.ignore(); self.hide(); close_to_tray.emit()` — so it **hides to the
  tray and keeps running**. It does not stop the copier and does not exit.
- The only orderly exit today is the tray menu's **Quit**
  (`manager/gui/tray.py:52-54`: `controller.stop(); QApplication.quit()`).
- **Console close** is not handled at all — Windows sends `CTRL_CLOSE_EVENT`
  and kills the process after a few seconds (abrupt; no orderly worker
  shutdown).

The user wants closing a window to **stop the copier and close everything**.
They chose to **hide the console entirely** (launch the GUI via `pythonw.exe`,
so no console is allocated) and **remove the tray entirely**, making the GUI
window close the single orderly quit path. Since there is then only one
visible window, "close either → close both + stop the copier" is satisfied:
closing the GUI runs `controller.stop()` and exits the process, and any
(hidden) console dies with it.

## Goal

A single-window app: launch the GUI with `pythonw.exe` (no console), make the
GUI window's close an orderly quit (`controller.stop()` then exit), and
remove the system-tray icon and close-to-tray behavior. CLI subcommands
(`copytrades --version`, `copytrades update`) keep their console output by
routing through `python.exe`.

## Signal & inputs available

- `CopyController.stop()` (`manager/app/controller.py:245-257`) is a blocking
  orderly shutdown: `sup.stop()` (sets the stop flag, closes pipes → workers
  `mt5.shutdown` on pipe EOF), `sup.join(timeout=5.0)` (waits up to 5s for the
  supervisor's daemon thread), then `sup.shutdown()`. It is already called on
  the main thread by the tray's `on_quit` (`tray.py:52-54`) — the same proven
  pattern `closeEvent` will reuse.
- The existing tray Quit (`tray.py:52-54`) is `self._controller.stop();
  QApplication.quit()` — the exact pattern `closeEvent` will mirror. Calling
  `QApplication.quit()` explicitly (rather than relying on
  `QApplication.quitOnLastWindowClosed`, which defaults to `True`) makes exit
  deterministic even if a dialog/top-level widget lingers. The existing
  `aboutToQuit → _save_config` hook (`main_window.py:70`) keeps saving config
  on exit — unchanged.
- The in-app updater already relaunches the manager with `pythonw.exe`
  (`manager/updater.py:164-169` `_helper_exe` prefers the venv's `pythonw.exe`;
  `manager/update_helper.py:144-148` `_relaunch` uses `sys.executable`, which
  is `pythonw.exe` when the helper runs under it). So the manager GUI is
  **already proven to run windowless** — this design just makes the primary
  shortcut/shim launch match.
- Worker subprocesses are `multiprocessing.Process` (`supervisor.py:140-149`)
  and share the parent's console; under `pythonw` they run windowless too — no
  worker changes.

## Design

### 1. `manager/gui/main_window.py` — close becomes the quit path

- Remove the `close_to_tray = Signal()` declaration (`main_window.py:57`) and
  any references to it.
- Rewrite `closeEvent` (`main_window.py:399-404`) to:
  1. call `self._controller.stop()` (blocking, ≤5s — same pattern as the tray
     Quit; safe on the main thread),
  2. `event.accept()` (let the window close),
  3. `QApplication.quit()` (deterministic exit — mirrors the tray Quit exactly,
     so it does not rely on `QApplication.quitOnLastWindowClosed` defaulting to
     `True`, which a lingering dialog/top-level widget could defeat).
  `aboutToQuit → _save_config` then saves config as it does today.
- Remove any tray references from this file.
- The `aboutToQuit → _save_config` connection (`main_window.py:70`) is
  **unchanged** — it still fires on quit and saves config.

### 2. `manager/__main__.py` — drop the tray

- Remove `from manager.gui.tray import TrayIcon`, and in `build_app_graph`
  remove `tray = TrayIcon(controller)` and `tray.install(window)`.
- `build_app_graph` returns a **3-tuple** `(window, controller, bridge)`
  instead of the 4-tuple `(window, tray, controller, bridge)`.
- Harden the `--version` `print` (`__main__.py:51`) against `pythonw`'s
  `sys.stdout is None` (guard with a `sys.stdout is not None` check), even
  though the `copytrades` shim routes `--version` to `python.exe`. Defensive,
  in case the GUI path is ever launched with `pythonw` and `--version` together.

### 3. `manager/gui/tray.py` — delete

- Delete the file. Delete `manager/tests/test_tray.py`.

### 4. Tests

- `manager/tests/test_main_entry.py`: `build_app_graph` now returns 3 values;
  drop the `assert tray is not None` line and unpack 3 values.
- `manager/tests/test_tray.py`: delete.
- `manager/tests/test_main_window.py`: the `aboutToQuit → _save_config` tests
  (around `:201-233`) stay valid — `closeEvent → quit → aboutToQuit` still
  saves config. Update their docstrings/comments that say "tray Quit" to
  "window close". Add a test that `closeEvent` calls `controller.stop()` and
  accepts the event (the new quit path). Remove any test asserting the
  `close_to_tray` signal is emitted.
- The `_save_config` tests (`test_main_window.py:141-163`) are unaffected.

### 5. `scripts/install.ps1` — launch GUI with `pythonw.exe`; CLI stays on `python.exe`

- Add `$PyWenv = Join-Path $Venv "Scripts\pythonw.exe"` (sibling of `$PyVenv`).
- Start Menu shortcut (`install.ps1:250-251`): `$sc.TargetPath = $PyWenv`,
  `$sc.Arguments = "-m manager"` → no console for the GUI launch.
- End-of-install launch (`install.ps1:260`):
  `Start-Process -FilePath $PyWenv -ArgumentList "-m","manager" -WorkingDirectory $InstallDir`.
- `copytrades.cmd` shim (`install.ps1:234-238`) branches on args — bare GUI
  launch uses `pythonw`; any CLI subcommand uses `python` (keeps console
  output for `--version` / `update`):
  ```bat
  @echo off
  if "%~1"=="" (
    "<PYWENV>" -m manager
  ) else (
    "<PYVENV>" -m manager %*
  )
  ```
  (`<PYWENV>` / `<PYVENV>` are the resolved paths written into the `.cmd`.)

### 6. `README.md` — update docs

- Quick Start step 6 (`README.md:49-50`): replace "Close the window to tray
  (workers keep running); tray Quit for an orderly stop. The app auto-checks
  for updates hourly." → "Close the window for an orderly stop — the engine
  stops and the app exits. (The app auto-checks for updates hourly.)"
- Features bullet "System tray" (`README.md:81-83`): remove the tray bullet;
  the close-to-tray description there is gone.
- Usage step 6 "Tray" (`README.md:190-192`): replace with a "Close" step —
  "Close the window to stop the engine and exit (single-window app; no tray)."
- File layout (`README.md:236`): remove the `tray.py` line.
- Any other mention of close-to-tray / tray.

## Behavior summary

- `copytrades` (no args) / Start Menu shortcut → `pythonw.exe -m manager` →
  GUI only, no console window.
- Close the GUI (X) → `controller.stop()` (workers joined ≤5s, copier stops)
  → window closes → app exits. Orderly.
- `copytrades --version` / `copytrades update` → `python.exe -m manager …`
  (console) → CLI output works as before.
- Dev mode `python -m manager` → still has a console (dev logs visible);
  close still quits. The `pythonw` change only affects the packaged
  shortcut/shim — devs keep their console.

## Backward compatibility / rollout

- **Close=quit works immediately on the next in-app update** for existing
  installs: closing the GUI exits the process, and a `python.exe` console
  window closes with its process regardless of `pythonw`. So the functional
  requirement (close GUI → stop copier + exit) takes effect as soon as the new
  wheel is installed.
- **The no-console launch needs a one-liner reinstall** to take effect: the
  in-app updater reinstalls the wheel but does **not** rewrite the Start Menu
  shortcut or `copytrades.cmd` (those are created by `install.ps1`). Users run
  the one-liner (`irm …/install.ps1 | iex`) to get the windowless launch. This
  is the standard reinstall/update flow already documented in the README.
- Config persistence is unchanged (`aboutToQuit → _save_config`).

## Risks / notes

- **Frozen window during stop**: `closeEvent` blocks ~≤5s while
  `controller.stop()` joins workers; the window will not repaint during that.
  Identical to the existing tray-Quit behavior; acceptable for a quit.
- **`pythonw` `stdout=None`**: the GUI path does not `print` (verified — the
  only `print` in the manager is `--version` in `__main__.py`, routed to
  `python.exe` and additionally guarded). Tracebacks under `pythonw` write to
  `stderr=None` — this is pre-existing behavior (the in-app updater already
  relaunches the manager with `pythonw`), not a new risk.
- **Dev mode keeps the console** intentionally (`python -m manager` from a
  shell) — good for dev logs; closing still quits.
- **Workers** are `multiprocessing.Process` children sharing the parent's
  console; under `pythonw` they run windowless (as today via the update
  relaunch). No worker/IPC changes.
- **The tray removal is a visible behavior change** (no more "run in
  background" via close-to-tray). The user explicitly chose this: closing the
  GUI now quits. To run in the background, minimize the window normally
  (taskbar) — the engine keeps running while minimized.