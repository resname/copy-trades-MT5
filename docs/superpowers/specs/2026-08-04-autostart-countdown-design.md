# Auto-start on Windows Boot + 15 s Auto-Copy Countdown

Date: 2026-08-04
Status: Approved (design)

## Problem

After a reboot, the user must manually relaunch the app and click Start to
resume copying. For an unattended or always-on setup this is friction: a
reboot stops copying until a human intervenes. The user wants two things,
independently toggleable:

1. **Launch the app automatically at Windows login** (OS-level autostart).
2. **Auto-start copying on every app launch**, after a **15 s cancellable
   countdown** — so a reboot (with toggle 1 on) resumes copying hands-off,
   but a user who opens the app to change config still has 15 s to abort.

Both are off by default; existing installs see no change until the user opts in.

## Goal

Add two checkboxes in a new "Auto-start" group box in the main window:

- **"Launch on Windows startup"** — creates/removes a shortcut in the Windows
  Startup folder (`shell:startup`) so Windows launches the app at login.
- **"Auto-start copying on launch (15 s countdown)"** — on every app launch
  with this toggle on and a master + slaves configured, run a 15 s countdown
  shown via a dedicated **Cancel** button; at 0, fire the normal Start path. If
  Start cannot run (no config, Algo Trading off, any error), **log and give up
  with no modal** — a modal would block an unattended reboot with nobody
  watching.

## Approach decisions (from brainstorming)

- **OS autostart mechanism: Startup folder shortcut** (Approach A). A `.lnk`
  in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CopyTradesMT5.lnk`
  targeting the same `pythonw.exe -m manager` as the Start Menu shortcut. Created
  via a PowerShell one-liner (WScript.Shell COM) shelled out from the app — the
  same technique `install.ps1` already uses for the Start Menu shortcut, so
  **no new dependency** (no `pywin32`). Rejected: HKCU `Run` registry value
  (less user-visible, users distrust invisible autostarts) and
  installer-managed (terrible UX — would re-run the installer to toggle).
- **Countdown trigger: on every launch when the toggle is on** (not only when
  launched via OS autostart). Simple and predictable; the user can always
  cancel within 15 s.
- **Countdown UX: a dedicated Cancel button** that appears in the Start/Stop
  row during the countdown only, then disappears. Start stays as Start (it is
  disabled during the countdown). The status panel also shows the countdown.
- **Failure handling: log and give up, no modal.** If no master/slaves are
  configured, skip silently with a log line. If Algo Trading is off (or any
  other start error), log it and leave the app idle — no modal. A modal would
  block an unattended reboot.
- **Settings UI: inline checkboxes** in a small "Auto-start" group box in the
  main window (no separate Settings dialog — matches the app's flat
  single-window layout).

## Design

### 1. New module: `manager/platform/autostart.py`

Qt-free, testable helper managing the Windows Startup shortcut. Functions:

- `startup_lnk_path() -> Path` —
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CopyTradesMT5.lnk`.
  On non-Windows (tests/dev on another OS), falls back to
  `Path.home()/.config/autostart/CopyTradesMT5.lnk` so the module is importable
  without path explosions. Only Windows is a supported target per the README.
- `is_autostart_enabled() -> bool` — `startup_lnk_path().exists()`.
- `enable_autostart(target_exe: str, arguments: str = "-m manager",
  working_dir: str | None = None) -> None` — shells out to PowerShell to create
  the `.lnk` via `WScript.Shell` COM. Sets `TargetPath=target_exe`,
  `Arguments=arguments`, `WorkingDirectory=working_dir` (only if not None),
  `Description="CopyTrades MT5 Local Manager"`. The PowerShell script is built
  with the `target_exe` path quoted as a single-quoted string (paths are
  validated to be a single argument — `shlex`/manual quoting, no shell
  injection). Uses `subprocess.run(["powershell", "-NoProfile", "-Command",
  <script>], check=True, capture_output=True, text=True)`. Raises a clear
  `AutostartError` on non-zero exit so the GUI can log and revert the toggle.
- `disable_autostart() -> None` — deletes the `.lnk` if present (idempotent;
  missing-file is a no-op).

The GUI passes `target_exe=str(sys.executable)` (the venv `pythonw.exe` under
the packaged launch, `python.exe` in dev) and `arguments="-m manager"`. No
`working_dir` is set (pass `None`) — Windows defaults the shortcut's working
directory sensibly; the app does not depend on it.

This is the only new file that touches the OS.

### 2. `manager/gui/main_window.py` — Auto-start group box + countdown

**UI construction (`_build_ui`):** add a `QGroupBox("Auto-start")` between the
slave box and the Start/Stop row, containing two `QCheckBox`es:

```python
self.autostart_boot_checkbox = QCheckBox("Launch on Windows startup")
self.autostart_copy_checkbox = QCheckBox(
    "Auto-start copying on launch (15 s countdown)")
```

Add a dedicated **Cancel** button to the Start/Stop controls row:

```python
self.autostart_cancel_button = QPushButton("Cancel")
self.autostart_cancel_button.setVisible(False)
controls.addWidget(self.autostart_cancel_button)
```

Hidden by default; shown only during the countdown.

**Wiring:** `autostart_boot_checkbox.toggled.connect(self._on_autostart_boot_toggled)`,
`autostart_copy_checkbox.toggled.connect(self._on_autostart_copy_toggled)`,
`autostart_cancel_button.clicked.connect(self._cancel_autostart_copy)`.

**State:** `self._countdown_timer: QTimer | None = None`,
`self._countdown_remaining: int = 0`.

**Countdown flow — `_maybe_begin_autostart_copy()` (called at the end of
`__init__`, after `_load_config()`):**

1. If `autostart_copy_checkbox` is unchecked → return.
2. If no master terminal set (`self.master_terminal.currentText().strip()` is
   empty) OR `not self._slaves` →
   `self.append_log("auto-start skipped: no master/slaves configured")` and
   return (no countdown).
3. Otherwise begin: disable Start + Stop, show the Cancel button, set
   `_countdown_remaining = 15`, set the Cancel button text to
   `f"Cancel (15 s)"`, append `"Auto-start in 15 s — click Cancel to abort"`
   to the status view, create a 1000 ms `QTimer` (single-shot=False) connected
   to `self._autostart_tick`, and start it.
4. `_autostart_tick()`: decrement `_countdown_remaining`; if > 0, update the
   Cancel button text to `f"Cancel ({n} s)"`; if == 0, stop the timer, hide the
   Cancel button, re-enable Start/Stop, and call `self._start_silent()`.
5. `_cancel_autostart_click()` (Cancel button): stop the timer, hide the Cancel
   button, re-enable Start/Stop, append `"auto-start cancelled"` to the log.

**Shared Start body — `_do_start(show_modal: bool)`:**

`_on_start` (manual click) and the auto-start countdown-at-0 path share one
method so the Start body is not duplicated. `show_modal` controls only whether
an `AlgoTradingDisabledError` raises a `QMessageBox.warning` (manual: yes,
auto-start: no). On any other `Exception`, both paths log and leave the app
idle.

```python
def _do_start(self, show_modal: bool) -> None:
    """Shared Start body. show_modal=True (manual click) shows the Algo
    Trading modal on failure; show_modal=False (auto-start) logs only."""
    terminal_path = self.master_terminal.currentText().strip()
    if not terminal_path:
        self.append_log("select a master terminal first")
        return
    master = AccountSpec(id="master", terminal_path=terminal_path)
    try:
        self._controller.start(master, list(self._slaves))
        self.set_running(True)
        self._save_config()
    except AlgoTradingDisabledError as exc:
        self.append_log(f"start failed: {exc}")
        if show_modal:
            QMessageBox.warning(self, "Algo Trading disabled", str(exc))
    except Exception as exc:
        self.append_log(f"start failed: {exc}")

def _start_silent(self) -> None:
    """Auto-start path: same Start body, never shows a modal."""
    self._do_start(show_modal=False)
```

`_on_start` (manual click) becomes a one-liner: `self._do_start(show_modal=True)`.
The blank-terminal guard inside `_do_start` covers both paths (the auto-start
path's "no master/slaves configured" check in `_maybe_begin_autostart_copy`
already guards the countdown entry, so the blank check in `_do_start` is a
safety net for it). The success path (`set_running(True)` + `_save_config()`)
is identical for both.

**Toggle handlers:**

- `_on_autostart_boot_toggled(checked: bool)`:
  - if `checked`: try `autostart.enable_autostart(sys.executable, "-m manager")`;
    on `Exception`, `self.append_log(f"autostart enable failed: {exc}")` and
    block-signal-set the checkbox back to unchecked (revert) so the UI matches
    reality.
  - if unchecked: `autostart.disable_autostart()` (idempotent).
  - finally: `self._save_config()`.
- `_on_autostart_copy_toggled(_checked: bool)`: `self._save_config()` only. No
  OS side effect. Does **not** start a countdown mid-session — the countdown
  only fires on launch.

**`sys` import:** add `import sys` at the top of `main_window.py` (not
currently imported — `subprocess` and `webbrowser` are). Needed for
`sys.executable` in the boot toggle handler.

### 3. Config persistence

The two toggles are stored inside the existing `"config"` blob (which
`SettingsStore.save_config`/`load_config` already round-trip under
`settings.json`'s `"config"` key) as a sub-dict:

```python
def _config_dict(self) -> dict:
    return {
        "master": {"terminal_path": self.master_terminal.currentText().strip()},
        "slaves": [dataclasses.asdict(s) for s in self._slaves],
        "autostart": {
            "on_boot": self.autostart_boot_checkbox.isChecked(),
            "auto_copy": self.autostart_copy_checkbox.isChecked(),
        },
    }
```

`_load_config`: read `cfg.get("autostart", {})` (defaulting to `{}` when
absent). Set:
- `auto_copy` checkbox → `bool(autostart.get("auto_copy", False))`.
- `on_boot` checkbox → `autostart.is_autostart_enabled()` **synced to reality**
  (whether the `.lnk` actually exists), not the stored value — so if the user
  deleted the Startup shortcut manually, the checkbox reflects that. The stored
  `on_boot` value is informational only; the `.lnk`'s existence is the source of
  truth. Use a `blockSignals` set so syncing does not trigger the toggle
  handler (which would re-create/delete the `.lnk` or save).

Both default to `False`/unchecked when the `autostart` key is absent — so
existing installs see no behavior change on update until they opt in.

`_save_config` persists on toggle (each handler calls it) and on `aboutToQuit`
as today. `dataclasses.asdict` for slaves is unchanged.

### 4. `manager/__main__.py`

No change to `build_app_graph`. The autostart shortcut target (`sys.executable`)
is read inside `MainWindow`'s toggle handler, not at graph-build time. No new
CLI flag (the countdown fires on every launch when the toggle is on; the
"only via OS autostart" launch-flag approach was rejected in brainstorming).

### 5. Tests

**`manager/tests/test_autostart.py` (new):**
- `test_startup_lnk_path_under_startup_folder` — the path ends with
  `Startup\CopyTradesMT5.lnk` (Windows) or the non-Windows fallback; assert the
  function returns a `Path` and is deterministic.
- `test_is_autostart_enabled_reflects_file_existence` — monkeypatch
  `startup_lnk_path` to a `tmp_path` file; `False` when absent, `True` after
  creating an empty file there.
- `test_enable_autostart_runs_powershell_with_quoted_target` — monkeypatch
  `subprocess.run` to capture the command; assert the first args are
  `["powershell", "-NoProfile", "-Command", <script>]`, the script contains
  the quoted `target_exe` path and `-m manager`, and `check=True` is used. No
  real PowerShell execution.
- `test_enable_autostart_raises_on_nonzero_exit` — monkeypatched
  `subprocess.run` raises `CalledProcessError`; `enable_autostart` raises
  `AutostartError`.
- `test_disable_autostart_idempotent` — monkeypatch `startup_lnk_path` to
  `tmp_path`; `disable_autostart()` does not raise whether or not the file
  exists; removes it if present.

**`manager/tests/test_main_window.py` (extend):**
- `test_autostart_checkboxes_default_off` — a fresh `MainWindow` has both
  checkboxes unchecked.
- `test_autostart_copy_countdown_skipped_when_toggle_off` — toggle off, config
  present → no countdown timer, no log line.
- `test_autostart_copy_countdown_skipped_when_config_incomplete` — toggle on,
  no master/slaves → log line `"auto-start skipped: no master/slaves
  configured"`, no timer, `FakeController.started` False.
- `test_autostart_copy_countdown_fires_start_at_zero` — toggle on, config
  present; call `_maybe_begin_autostart_copy()` then drive the timer to
  completion (call `_autostart_tick()` 15 times, or set
  `_countdown_remaining = 1` and tick once for determinism); assert
  `FakeController.started` True, Cancel button hidden, Start/Stop re-enabled.
- `test_autostart_cancel_button_aborts` — begin countdown, click Cancel →
  timer stopped, log `"auto-start cancelled"`, `FakeController.started` False.
- `test_start_silent_logs_on_algo_trading_disabled_no_modal` — FakeController
  whose `start` raises `AlgoTradingDisabledError`; call `_start_silent()`;
  assert a log line contains `"auto-start failed"` and no `QMessageBox` was
  shown (monkeypatch `QMessageBox.warning` to record).
- `test_do_start_manual_shows_modal_on_algo_trading_disabled` — the manual
  `_on_start` path still shows the modal (regression guard for the shared
  `_do_start` refactor).
- `test_autostart_config_round_trip` — save with `autostart` sub-dict, new
  `MainWindow` restores both checkbox states (auto_copy from stored, on_boot
  from `is_autostart_enabled`).
- `test_boot_checkbox_syncs_to_lnk_existence_on_load` — store config with
  `on_boot: True` but no `.lnk` on disk → loaded checkbox is unchecked (reality
  wins); with a `.lnk` present → checked.

**`manager/tests/test_settings_store.py` (extend):**
- `test_save_then_load_config_round_trip_with_autostart` — config with an
  `autostart` sub-dict round-trips unchanged alongside master + slaves.

### 6. README

- Features: add a bullet — "**Auto-start on boot + auto-copy** — optionally
  launch the app at Windows login and auto-start copying 15 s after launch (one
  click to cancel); fails silently (logged) if the terminals aren't ready."
- Usage: add a note describing the two checkboxes, the Cancel button, and the
  no-modal failure behavior.

## Behavior summary

- **Boot toggle on** → app launches at Windows login (Startup `.lnk`).
- **Auto-copy toggle on + master + slaves configured** → on every app launch,
  a 15 s countdown with a Cancel button; at 0, copying starts (log-only on
  failure, no modal). Toggle off or config incomplete → no countdown.
- **Both off** (the default for existing installs) → no change from today.
- **Manual Start** still shows the Algo Trading modal as today (unchanged).

## Backward compatibility

Existing installs: `autostart` key absent → both checkboxes default unchecked
→ no new behavior until the user opts in. The countdown only fires when the
toggle is on, so a user who updates and never touches the checkboxes sees no
change. The OS-autostart `.lnk` is only created if the user explicitly checks
the box. `save_config`/`load_config` remain backward compatible (the new
`autostart` sub-dict is optional; old configs without it load with both
toggles off).

## Risks / notes

- **PowerShell availability:** the autostart helper shells out to `powershell`.
  PowerShell 5.1 is present on every supported Windows 10/11 (and
  `install.ps1` already depends on it). If a host somehow lacks it,
  `enable_autostart` raises `AutostartError`, the toggle reverts, and the log
  explains it — no crash.
- **Shortcut target drift after a reinstall:** if the user re-runs the
  one-liner installer (which can move the venv), an old Startup `.lnk` may point
  at a stale `pythonw.exe`. Mitigated: the boot checkbox syncs to
  `is_autostart_enabled()` on load, and toggling it off→on rewrites the `.lnk`
  with the current `sys.executable`. A user who reinstalls and re-checks the
  box gets a fresh shortcut. (Auto-refreshing the target on every launch is
  YAGNI — the toggle is the control.)
- **Auto-start races terminal readiness:** `controller.start()` already gates
  on slave readiness + Algo Trading and raises on failure; `_start_silent`
  catches that and logs (no modal). No new race.
- **Frozen window during auto-start start:** `_start_silent` calls
  `controller.start()`, which blocks ~≤90 s on the readiness gate in the worst
  case. This runs on the GUI thread (same as manual Start today), so the
  window may appear unresponsive during that window. Acceptable — identical to
  the existing manual Start behavior, and the user opted in.
- **Countdown timer lifetime:** the `QTimer` is a child of the `MainWindow`;
  `closeEvent` quits the app, which destroys the window and its timer. No
  lingering timer. The `_cleanup_qt_widgets` conftest fixture already deletes
  top-level widgets per test, so countdown timers do not leak across tests.