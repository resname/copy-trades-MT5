# Single-Window Quit-on-Close Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make closing the GUI window an orderly quit (stop the engine + exit), remove the system-tray icon and close-to-tray behavior, and launch the packaged GUI with `pythonw.exe` so no console window appears — a single-window app.

**Architecture:** The GUI window's `closeEvent` becomes the quit path: `controller.stop()` (blocking ≤5s, the same pattern the tray Quit and update-quit already use), `event.accept()`, then `QApplication.quit()`. The tray icon, `close_to_tray` signal, and `manager/gui/tray.py` are deleted; `manager/__main__.py`'s `build_app_graph` returns a 3-tuple. The installer launches the GUI via the venv's `pythonw.exe` (no console allocated) while keeping CLI subcommands (`--version`, `update`) on `python.exe` for console output.

**Tech Stack:** PySide6/Qt, Python 3.11+, PowerShell installer (`scripts/install.ps1`), pytest (GUI tests use the PySide6 venv at `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe`).

## Global Constraints

- Work directly on `main` (standing user instruction — no worktree, no feature branch).
- GUI tests run on the PySide6 venv: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q`. The full suite must stay green (the pre-existing `_slave_loop` teardown `PytestUnhandledThreadExceptionWarning` is a warning, not a failure).
- `gh` CLI is not on PATH — call `C:\Program Files\GitHub CLI\gh.exe` by full path (only relevant at push/release time, which the controller does after the final review).
- Demo accounts only; no credentials stored/piped/logged (N/A for this change, but do not introduce any).
- `controller.stop()` is blocking and safe on the main thread (already proven by the tray Quit and `_do_update_quit` paths). Do not make it async.
- `QApplication` is already imported at the top of `manager/gui/main_window.py` (line 9) — use it directly in `closeEvent`; do not add a local import.
- `sys` is already imported in `manager/__main__.py` (line 3) — use it directly for the `--version` stdout guard.

---

### Task 1: Remove tray + make window close the quit path

**Files:**
- Modify: `manager/gui/main_window.py` (docstring `:54-55`, signal `:57`, `closeEvent` `:398-404`)
- Modify: `manager/__main__.py` (import `:13`, `build_app_graph` `:25-38`, `main()` unpack `:59`, `--version` print `:49-52`)
- Delete: `manager/gui/tray.py`
- Delete: `manager/tests/test_tray.py`
- Modify: `manager/tests/test_main_entry.py` (test `:18-31`)
- Modify: `manager/tests/test_main_window.py` (docstring `:201-203`; append a new test after `:235`)

**Interfaces:**
- Consumes: `CopyController.stop()` (`manager/app/controller.py:245-257`) — blocking orderly shutdown; called from the main thread.
- Produces: `build_app_graph(app) -> (window, controller, bridge)` (3-tuple, was 4). `MainWindow.closeEvent` now quits instead of emitting `close_to_tray`. `manager.gui.tray` no longer exists.

- [ ] **Step 1: Write the failing test for closeEvent as the quit path**

Append to `manager/tests/test_main_window.py` (after the `test_about_to_quit_saves_once_not_twice` test, i.e. after line 235):

```python


def test_close_event_stops_controller_and_quits(qapp, monkeypatch):
    """closeEvent is the quit path: controller.stop() then event.accept() +
    QApplication.quit(). Replaces the old hide-to-tray behavior."""
    from manager.gui.main_window import MainWindow
    from PySide6.QtWidgets import QApplication
    quit_called = []
    monkeypatch.setattr(QApplication, "quit",
                        lambda *a, **k: quit_called.append(True))
    c = FakeController()
    w = MainWindow(c)

    class _Evt:
        def __init__(self):
            self.accepted = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    evt = _Evt()
    w.closeEvent(evt)
    assert c.stopped is True      # controller.stop() ran
    assert evt.accepted is True   # event accepted (window closes)
    assert quit_called            # QApplication.quit() invoked
```

`FakeController.stop()` already sets `self.stopped = True` (`test_main_window.py:18-19`), so this test reuses it.

- [ ] **Step 2: Run the test to verify it fails**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py::test_close_event_stops_controller_and_quits -v
```
Expected: FAIL — the current `closeEvent` ignores the event, hides, and emits `close_to_tray` (does not call `stop`, does not accept, does not quit).

- [ ] **Step 3: Make `closeEvent` the quit path and remove the `close_to_tray` signal**

In `manager/gui/main_window.py`, replace the class docstring's close clause (lines 54-55):
```python
    thread. Close is intercepted to emit close_to_tray (the tray icon hides
    the window instead of quitting)."""
```
with:
```python
    thread. Closing the window is the orderly quit path (controller.stop() then
    QApplication.quit()); there is no tray."""
```

Remove the signal declaration. Replace:
```python
    close_to_tray = Signal()

    def __init__(self, controller, store=None, parent=None):
```
with:
```python
    def __init__(self, controller, store=None, parent=None):
```

Replace the `closeEvent` block (lines 398-404):
```python
    # ---- close-to-tray ----
    def closeEvent(self, event):
        """Intercept the window close: hide to tray instead of quitting. The
        tray menu's Quit is the real orderly shutdown path."""
        event.ignore()
        self.hide()
        self.close_to_tray.emit()
```
with:
```python
    # ---- close = quit ----
    def closeEvent(self, event):
        """Closing the window is the orderly quit path: stop the engine (join
        workers), accept the close, then quit the app — mirrors the update-quit
        path in _do_update_quit."""
        self._controller.stop()
        event.accept()
        QApplication.quit()
```

`QApplication` is already imported at the top of the file (line 9). `Signal` stays imported (still used by `_UpdateWorker`/`_DownloadWorker`).

- [ ] **Step 4: Drop the tray from `manager/__main__.py` and return a 3-tuple**

In `manager/__main__.py`, remove the tray import. Delete the line:
```python
from manager.gui.tray import TrayIcon
```
(the line between `from manager.gui.main_window import MainWindow` and `from manager.gui.settings.store import SettingsStore`).

Replace `build_app_graph` (lines 25-38):
```python
def build_app_graph(app: QApplication):
    store = SettingsStore()
    terminal_manager = TerminalManager(store=store)
    bridge = _StatusBridge()
    controller = CopyController(
        terminal_manager=terminal_manager, store=store,
        on_status=lambda s: bridge.status.emit(s),
        on_log=lambda m: bridge.log.emit(m))
    window = MainWindow(controller, store=store)
    bridge.status.connect(window.append_status)
    bridge.log.connect(window.append_log)
    tray = TrayIcon(controller)
    tray.install(window)
    return window, tray, controller, bridge
```
with:
```python
def build_app_graph(app: QApplication):
    store = SettingsStore()
    terminal_manager = TerminalManager(store=store)
    bridge = _StatusBridge()
    controller = CopyController(
        terminal_manager=terminal_manager, store=store,
        on_status=lambda s: bridge.status.emit(s),
        on_log=lambda m: bridge.log.emit(m))
    window = MainWindow(controller, store=store)
    bridge.status.connect(window.append_status)
    bridge.log.connect(window.append_log)
    return window, controller, bridge
```

In `main()` (line 59), replace the 4-tuple unpack:
```python
    window, tray, controller, bridge = build_app_graph(app)
```
with:
```python
    window, controller, bridge = build_app_graph(app)
```

- [ ] **Step 5: Harden the `--version` print against `pythonw` (`sys.stdout is None`)**

In `manager/__main__.py`, replace the `--version` block (lines 49-52):
```python
    if "--version" in args:
        from manager._version import __version__
        print(__version__)
        return 0
```
with:
```python
    if "--version" in args:
        from manager._version import __version__
        if sys.stdout is not None:
            print(__version__)
        return 0
```
`sys` is already imported (line 3). Under `pythonw`, `sys.stdout` is `None`; under normal console/pytest it is not `None`, so `test_main_version_flag` (which uses `capsys`) still passes.

- [ ] **Step 6: Delete `manager/gui/tray.py` and `manager/tests/test_tray.py`**

Delete both files:
```powershell
git rm manager/gui/tray.py manager/tests/test_tray.py
```

- [ ] **Step 7: Update `manager/tests/test_main_entry.py` for the 3-tuple + tray deletion**

Replace the test (lines 18-31):
```python
def test_main_assembles_window_tray_controller(qapp, monkeypatch):
    # patch the real TerminalManager + SettingsStore so assembly needs no disk/MT5
    import manager.__main__ as entry
    monkeypatch.setattr(entry, "TerminalManager", lambda *a, **k: FakeTerminalManager())
    monkeypatch.setattr(entry, "SettingsStore", lambda *a, **k: _FakeStore())
    # don't run the event loop; just build the graph (returns 4: window, tray, ctrl, bridge)
    w, tray, controller, bridge = entry.build_app_graph(qapp)
    assert w is not None
    assert tray is not None
    assert controller is not None
    assert bridge is not None
    # the controller's status callback is wired to the window via the bridge
    assert hasattr(w, "append_status")
```
with:
```python
def test_main_assembles_window_controller_bridge(qapp, monkeypatch):
    # patch the real TerminalManager + SettingsStore so assembly needs no disk/MT5
    import manager.__main__ as entry
    import importlib
    monkeypatch.setattr(entry, "TerminalManager", lambda *a, **k: FakeTerminalManager())
    monkeypatch.setattr(entry, "SettingsStore", lambda *a, **k: _FakeStore())
    # don't run the event loop; just build the graph (returns 3: window, ctrl, bridge)
    w, controller, bridge = entry.build_app_graph(qapp)
    assert w is not None
    assert controller is not None
    assert bridge is not None
    # the controller's status callback is wired to the window via the bridge
    assert hasattr(w, "append_status")
    # the tray module is gone
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("manager.gui.tray")
```

- [ ] **Step 8: Update the `aboutToQuit` test docstring in `test_main_window.py`**

In `manager/tests/test_main_window.py`, replace the docstring (lines 201-203):
```python
    """Guards the aboutToQuit→_save_config hook (requirement #6): both tray
    Quit and update-quit reduce to QApplication.quit() → aboutToQuit, so this
    one test covers both quit paths. If the connect line is removed, this fails."""
```
with:
```python
    """Guards the aboutToQuit→_save_config hook: both window-close and
    update-quit reduce to QApplication.quit() → aboutToQuit, so this one test
    covers both quit paths. If the connect line is removed, this fails."""
```

- [ ] **Step 9: Run the full suite to verify green**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q
```
Expected: all green (the pre-existing `_slave_loop` teardown `PytestUnhandledThreadExceptionWarning` may still appear as a warning — not a failure).

- [ ] **Step 10: Commit**

```bash
git add manager/gui/main_window.py manager/__main__.py manager/tests/test_main_entry.py manager/tests/test_main_window.py
git commit -m "feat(gui): window close is the quit path; remove tray + close-to-tray" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```
(`git rm` already staged the two deletions.)

---

### Task 2: Launch the packaged GUI with `pythonw.exe` (no console)

**Files:**
- Modify: `scripts/install.ps1` (`$PyWenv` definition + `copytrades.cmd` block `:234-238`; Start Menu shortcut `:250`; end-of-install launch `:260`)
- Create: `manager/tests/test_install_script.py`

**Interfaces:** None (installer + a static source check). No Python runtime interface changes.

- [ ] **Step 1: Write the failing static check for the installer using `pythonw.exe` for the GUI**

Create `manager/tests/test_install_script.py`:
```python
"""Static source checks on scripts/install.ps1 — guards the GUI-uses-pythonw
invariant (no console window) and the branched copytrades.cmd (CLI subcommands
keep their console output). Reads the script text; does not execute it."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")


def test_installer_defines_pythonw_venv_path():
    assert '$PyWenv = Join-Path $Venv "Scripts\\pythonw.exe"' in INSTALL_PS1


def test_start_menu_shortcut_targets_pythonw():
    assert "$sc.TargetPath = $PyWenv" in INSTALL_PS1


def test_end_of_install_launches_pythonw():
    assert "Start-Process -FilePath $PyWenv -ArgumentList \"-m\", \"manager\"" in INSTALL_PS1


def test_copytrades_cmd_branches_gui_vs_cli():
    # bare GUI launch (no args) -> pythonw (windowless); args -> python (console)
    assert 'if "%~1"==""' in INSTALL_PS1
    assert '"$PyWenv" -m manager' in INSTALL_PS1
    assert '"$PyVenv" -m manager %*' in INSTALL_PS1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_install_script.py -v
```
Expected: FAIL — the current installer uses `$PyVenv` (python.exe) for everything and the `copytrades.cmd` is a single non-branching line.

- [ ] **Step 3: Add `$PyWenv` and branch the `copytrades.cmd` shim**

In `scripts/install.ps1`, replace the `copytrades.cmd` block (lines 232-238):
```powershell
# 6. Launcher + PATH
$Bin = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$Cmd = Join-Path $Bin "copytrades.cmd"
@"
@echo off
"$PyVenv" -m manager %*
"@ | Set-Content -Path $Cmd -Encoding ASCII
```
with:
```powershell
# 6. Launcher + PATH
$Bin = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$PyWenv = Join-Path $Venv "Scripts\pythonw.exe"
$Cmd = Join-Path $Bin "copytrades.cmd"
@"
@echo off
if "%~1"=="" (
  "$PyWenv" -m manager
) else (
  "$PyVenv" -m manager %*
)
"@ | Set-Content -Path $Cmd -Encoding ASCII
```
`$PyWenv` is defined here (step 6), in scope for the shortcut (step 7) and the launch (end of script) which follow.

- [ ] **Step 4: Point the Start Menu shortcut at `pythonw.exe`**

In `scripts/install.ps1`, replace the shortcut `TargetPath` line (line 250):
```powershell
  $sc.TargetPath = $PyVenv
```
with:
```powershell
  $sc.TargetPath = $PyWenv
```

- [ ] **Step 5: Launch with `pythonw.exe` at end of install**

In `scripts/install.ps1`, replace the end-of-install launch (line 260):
```powershell
  Start-Process -FilePath $PyVenv -ArgumentList "-m", "manager" -WorkingDirectory $InstallDir
```
with:
```powershell
  Start-Process -FilePath $PyWenv -ArgumentList "-m", "manager" -WorkingDirectory $InstallDir
```

- [ ] **Step 6: Run the static checks to verify they pass**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_install_script.py -v
```
Expected: PASS (4 tests).

- [ ] **Step 7: Run the full suite to confirm no regressions**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q
```
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add scripts/install.ps1 manager/tests/test_install_script.py
git commit -m "feat(install): launch GUI via pythonw.exe (no console); keep CLI subcommands on python.exe" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: README — document the single-window close=quit behavior

**Files:**
- Modify: `README.md` (Quick Start step 6 `:49-50`; Features "System tray" bullet `:89-91`; PySide6 dep line `:129`; Usage step 6 `:199-201`; file layout `:216` and `:245`)

**Interfaces:** None (documentation only).

- [ ] **Step 1: Update Quick Start step 6**

In `README.md`, replace lines 49-50:
```markdown
6. Close the window to tray (workers keep running); tray **Quit** for an orderly
   stop. The app auto-checks for updates hourly.
```
with:
```markdown
6. Close the window for an orderly stop — the engine stops and the app exits.
   The app auto-checks for updates hourly.
```

- [ ] **Step 2: Replace the Features "System tray" bullet**

In `README.md`, replace lines 89-91:
```markdown
- **System tray** — close-to-tray keeps the workers running in the background;
  tray Quit does an orderly shutdown (stops the engine, joins workers, then
  quits the app).
```
with:
```markdown
- **Single-window close = stop** — closing the window stops the engine (joins
  workers) and exits the app; no system tray, no background-running mode.
```

- [ ] **Step 3: Update the PySide6 dependency description**

In `README.md`, replace line 129:
```markdown
  - `PySide6>=6.6` — GUI + system tray
```
with:
```markdown
  - `PySide6>=6.6` — GUI
```

- [ ] **Step 4: Replace Usage step 6 (Tray → Close)**

In `README.md`, replace lines 199-201:
```markdown
6. **Tray**: closing the window hides it to the tray — workers keep running.
   Use the tray **Quit** for an orderly shutdown (stop engine → join workers →
   quit).
```
with:
```markdown
6. **Close**: closing the window stops the engine (joins workers) and exits
   the app — there is no tray. Minimize the window to keep it running in the
   background.
```

- [ ] **Step 5: Update the file-layout `__main__.py` description**

In `README.md`, replace line 216:
```markdown
  __main__.py            App entry: QApplication + window + tray + status bridge
```
with:
```markdown
  __main__.py            App entry: QApplication + window + status bridge
```

- [ ] **Step 6: Remove the file-layout `tray.py` line**

In `README.md`, replace the two lines (244-245):
```markdown
    slave_editor.py      Add/edit slave account dialog
    tray.py              System tray (close-to-tray + orderly quit)
```
with:
```markdown
    slave_editor.py      Add/edit slave account dialog
```

- [ ] **Step 7: Verify no stray tray/close-to-tray mentions remain**

Run (POSIX, in the repo root):
```bash
grep -n -i "tray\|close-to-tray\|close to tray" README.md
```
Expected: no output (all tray mentions removed). If any remain, update them to match the single-window close=quit wording.

- [ ] **Step 8: Commit**

```bash
git add README.md
git commit -m "docs: single-window close=quit; remove tray/close-to-tray references" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```