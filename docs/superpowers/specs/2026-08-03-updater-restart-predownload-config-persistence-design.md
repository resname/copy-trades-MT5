# Updater restart-fix + pre-download + config persistence — Design

**Date:** 2026-08-03
**Status:** Design (pre-implementation)
**Branch:** `feat/updater-restart-config-persistence` (off `origin/main` dd03a70,
which includes the merged manual-login PR #14).

## Goal

Four changes to the local manager, bundled because they overlap in the
update/restart path:

1. **GUI button tweak** — swap the Launch/Install button order and relabel
   "Launch terminal" → "Open terminal for login" (both the master form and the
   slave editor), so the login purpose is explicit.
2. **Updater restart bug** — clicking "Update & restart" closes the app and
   reinstalls the wheel, but the manager never relaunches. Replace the fragile
   relaunch mechanism with a robust, tested one.
3. **Periodic checks already exist** (1-hour `QTimer` + 10-second startup
   check). Add **auto pre-download**: when a check finds an update, download +
   SHA256-verify the wheel to a local cache in the background so "Update &
   restart" runs the cached installer — restart in seconds, and the network
   leaves the restart critical path.
4. **Config persistence** — the master terminal + slaves (with per-slave
   options) are currently in-memory only and are lost on any full restart.
   Persist them to `settings.json` and auto-restore on startup. The settings
   file already lives in `%APPDATA%` (roaming), separate from the install dir
   in `%LOCALAPPDATA%`, so persistence across *updates* is free once we save
   at all.

## Background / Problem

**Restart bug.** `updater.apply_update_and_restart` spawns a detached
PowerShell: `& ([scriptblock]::Create((irm '<install.ps1 url>'))) -Yes`, then
calls `on_quit()` (which stops the engine and `QApplication.quit()`s).
`install.ps1` (run inline, with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`)
stops the old app, downloads the wheel, `pip install --force-reinstall`s it, and
finally `Start-Process -FilePath $PyVenv -ArgumentList "-m","manager"` to
relaunch. Observed symptom: the wheel *is* upgraded, but the manager does not
come back. The relaunch step (`Start-Process` from inside a no-console
detached `powershell -Command`) is the fragile, untested part — neither
existing test (`test_apply_update_and_restart_spawns_and_quits`,
`test_background_spawn_actually_runs_command_body`) covers the real relaunch.

**Config persistence.** `MainWindow._slaves` is a plain list rebuilt only from
the slave editor; nothing writes it to `SettingsStore`. The store has an
`accounts` setdefault (used internally by `assign`) but the GUI never saves or
loads the running config. So a tray-Quit or an update-restart loses the master
terminal and slave set.

## Decisions (from brainstorming)

1. **Approach A for the updater** (chosen over B/C): an in-package Python
   updater + pre-download cache + a fully-detached helper module that waits
   for the manager to exit, reinstalls the cached wheel, and `Popen`-relaunches
   the manager. `install.ps1` stays for *fresh* installs only; updates no
   longer call it. This replaces the fragile `Start-Process`-in-a-no-console-
   powershell relaunch with a clean, mock-testable `Popen` relaunch.
2. **Button order**: `Install MetaTrader` (left) → `Open terminal for login`
   (right), in both `main_window.py` and `slave_editor.py`.
3. **Launch relabel**: "Launch terminal" → **"Open terminal for login"** in
   both files.
4. **Periodic check**: keep the 1-hour interval + 10s startup check; add
   auto pre-download on detection.
5. **Config persistence**: save on Start, on slave add/remove/edit, and on
   orderly quit; auto-restore on startup.

## Architecture / Data Flow

### Update flow (Approach A)

```
1-hour QTimer fires (or user clicks "Check for updates")
  → check_for_update()                                  [network: version.txt]
  → if info.available:
      background _UpdateWorker runs download_update()   [network: wheel + .sha256]
        → verify SHA256, write %LOCALAPPDATA%\CopyTradesMT5\updates\<v>.whl
        → label: "Update available: vX (ready — restart in seconds)"
      on failure: label stays "Update available: vX" (fall back to download-on-click)

User clicks "Update & restart" (engine idle)
  → apply_update_and_restart(on_quit, cached_wheel=<path or None>)
      ensure verified wheel: use cache, else download+verify NOW (before quit)
        on failure: abort — do NOT call on_quit (app stays open, user informed)
      Popen([pythonw, "-m", "manager.update_helper", wheel, str(parent_pid)],
            creationflags=CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS|CREATE_NO_WINDOW,
            close_fds=True)                             [fully decoupled from manager]
      on_quit()  → controller.stop() + QApplication.quit()  [manager exits]

helper process (survives manager exit):
  poll until parent_pid no longer exists                [old manager released venv]
  pip install --force-reinstall <cached wheel>          [no network]
  Popen([pythonw, "-m", "manager"], detached)           [relaunch — the tested step]
  exit (log to %LOCALAPPDATA%\CopyTradesMT5\updates\update.log)
```

No network is in the restart critical path once a wheel is cached. The relaunch
is a `Popen` from a surviving detached helper, not a `Start-Process` inside a
no-console PowerShell. A failed relaunch leaves evidence in `update.log`
(fixing the silent-failure class).

### Config flow

```
startup:  MainWindow.__init__ → _populate_terminals → _load_config()
            store.load()["config"] → master terminal + slaves list
            set master_terminal current text; rebuild self._slaves + slave_list widget

edits:   Add/Remove/Edit slave → _save_config()
start:   _on_start → controller.start(...) → _save_config()
quit:    tray Quit / update on_quit → _save_config()
            store.save({"config": {"master": {...}, "slaves": [...]}, ...rest})
```

## Component Changes

### `manager/updater.py`

Keep `parse_version`, `current_version`, `_fetch_text`, `latest_version`,
`check_for_update`, `UpdateInfo`. Add:

```python
UPDATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) \
             / "CopyTradesMT5" / "updates"

def _wheel_urls() -> tuple[str, str]:
    # (WHEEL_URL, WHEEL_SHA_URL) — already module constants
    return WHEEL_URL, WHEEL_SHA_URL

def download_update(dest_dir: Path | None = None) -> Path:
    """Download WHEEL_URL + WHEEL_SHA_URL into dest_dir (default UPDATE_DIR),
    verify SHA256, return the verified wheel path. Raises UpdateDownloadError
    on network failure or checksum mismatch. Cleans older cached wheels so
    only the latest verified one remains."""

def cached_update() -> Path | None:
    """Return the path of the cached+verified wheel for the latest known
    version, or None if no usable cache. (Verified = SHA matches the .sha256
    sitting beside it; if either file is missing/mismatched, treat as absent
    and delete the stale pair.)"""

def apply_update_and_restart(on_quit, cached_wheel: Path | None = None) -> None:
    """Ensure a verified wheel (cached_wheel, else cached_update(), else
    download_update now). On failure, return WITHOUT calling on_quit (the
    caller should surface a message). On success: spawn the detached helper
    with (wheel, parent_pid=os.getpid()), then call on_quit()."""
```

The detached-spawn flags move into a shared helper so `update_helper.py` can
reuse the same constants. The existing `_BG_FLAGS` (for the old powershell
spawn) is replaced.

### `manager/update_helper.py` (new — import-light, standalone)

A module whose `main(argv)` is run as `pythonw -m manager.update_helper
<wheel> <parent_pid>`. Responsibilities:

- Parse `<wheel>` (verified wheel path) and `<parent_pid>`.
- Poll `psutil.pid_exists(parent_pid)` (or `os.kill(pid, 0)` portably) until the
  parent is gone. Cap the wait (e.g. 60s); if the parent is still alive then
  (a hung manager must not block the update forever), **force-terminate it**
  (`psutil.Process(parent_pid).kill()` wrapped in a try/except) before
  proceeding — this avoids a pip install failing on venv files the old process
  still holds.
- Run the venv's pip: `<venv>/Scripts/python.exe -m pip install --upgrade
  --force-reinstall <wheel>` (resolve the venv python from `sys.executable`).
- `Popen([pythonw, "-m", "manager"], creationflags=CREATE_NEW_PROCESS_GROUP |
  DETACHED_PROCESS | CREATE_NO_WINDOW, close_fds=True)` to relaunch.
- Append a line to `%LOCALAPPDATA%\CopyTradesMT5\updates\update.log` at each
  step (start / parent-gone / install-ok / install-fail / relaunch-ok) so a
  failed relaunch is diagnosable.
- Exit code 0 on success, non-zero on a failed step (logged).

It must be import-light (no PySide6, no manager package imports beyond stdlib +
`psutil`) so it survives running while the old package is being overwritten.

### `manager/gui/main_window.py`

- **Buttons**: swap the two `term_row.addWidget` calls so `install_metatrader_button`
  is added first (left), `launch_terminal_button` second (right). Relabel
  `launch_terminal_button = QPushButton("Open terminal for login")`. (Same change
  in `slave_editor.py`.)
- **Config persistence**:
  - `_save_config()`: read current `self.master_terminal.currentText()` + `self._slaves`
    (each as its full AccountSpec dict), build `{"master": {...}, "slaves": [...]}`,
    merge into the store's data (preserve `accounts`/`provisioned_instances`/`global`)
    and `store.save()`.
  - `_load_config()`: on init, read `store.load().get("config")`; set
    `master_terminal` current text (even if not in the discovered list — combo is
    editable); rebuild `self._slaves` and the slave list widget labels.
  - Call `_save_config()` in `_on_start` (after a successful start), in
    `_on_add_slave`/`_on_remove_slave`, and in the orderly-quit path (the tray
    Quit handler — wire into the existing quit flow). There is no "edit slave"
    path today (slaves are add/remove only), so no edit hook is needed.
- **Auto pre-download**: in `_on_update_checked`, when `info.available`, start a
  background `_UpdateWorker` running `updater.download_update`; on success set
  the label to `Update available: vX (ready — restart in seconds)` and stash the
  cached wheel path; on failure leave the label at `Update available: vX`.
  `_on_update_restart` passes the stashed cached wheel to
  `apply_update_and_restart`. If no stash, pass `None` (the updater downloads on
  click, verified before quit).

### `manager/gui/slave_editor.py`

- **Buttons**: the slave editor has only a Launch button (no Install button),
  so there is no order swap here — **relabel only**: "Launch terminal" →
  "Open terminal for login" (consistent with the master form). Update the
  class docstring (line 17: "a Launch-terminal button") to match.

### `manager/__main__.py`

- The `update` subcommand path (`updater.apply_update_and_restart(on_quit=
  lambda: sys.exit(0))`) is unchanged in shape; it now benefits from the cached
  wheel + robust helper automatically.

### `manager/settings/store.py`

- Add convenience accessors (thin wrappers over `load`/`save`) so the GUI does
  not hand-merge dicts:
  - `load_config() -> dict` (returns `data.get("config", {})`).
  - `save_config(config: dict) -> None` (load-merge-`config`-save, preserving
    the other top-level keys).
- No change to the file path (`%APPDATA%\CopyTradesMT5\settings.json`) or atomic
  write. The `accounts` key stays for `assign`'s internal use; `config` is the
  GUI's restorable state.

### `install.ps1`

- **Unchanged** for the fresh-install one-liner path (Python/venv/shortcut
  setup). Updates no longer call it. (Optional, **out of scope** unless
  requested: a `--wheel <path>` param to use a cached wheel for fresh install.)

### `README.md` / `docs/TESTING.md`

- README: update the Features bullet that names the buttons ("Launch terminal /
  Install MetaTrader buttons" → "Install MetaTrader / Open terminal for login
  buttons"); note config persistence + auto pre-download in the updater blurb.
- `docs/TESTING.md`: add `test_update_helper.py` to the section-3 table; note the
  updater tests now cover download/cache/apply.

## Testing

- `test_updater.py`:
  - `download_update`: mock `urllib.request.urlopen`; assert SHA mismatch raises
    `UpdateDownloadError` and leaves no partial wheel; assert success returns a
    path whose content matches the fake wheel and whose SHA matches.
  - `cached_update`: cache present + verified → returns path; cache missing →
    `None`; cache present but SHA mismatch → `None` and stale files deleted.
  - `apply_update_and_restart`: mock the helper `Popen` + `on_quit`; with a
    cached wheel → asserts it spawns the helper with `(wheel, parent_pid)` and
    calls `on_quit`; with no cache + a forced download failure → asserts it does
    **not** call `on_quit` (abort path) and does not spawn the helper.
- `test_update_helper.py` (new):
  - Mock the parent-pid poll, the pip invocation, and the relaunch `Popen`;
    assert the sequence wait → install → relaunch and that install does not run
    while the parent pid still exists.
- GUI tests (`test_main_window.py`, `test_slave_editor.py`):
  - Button order + label assertions (Install left, "Open terminal for login"
    right).
  - Config round-trip: add slaves, call `_save_config`, construct a new
    `MainWindow` with the same store, assert slaves + master terminal restored.
  - Auto-pre-download: `check_for_updates_now` with a mocked
    `check_for_update` returning `available=True` triggers a
    `download_update` worker; on success the label shows "ready".
- HARD gate: "no new failures, suite green." GUI gate = the **PySide6 venv**
  suite (`C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m
  pytest -q`), per the `gui-tests-need-pyside6-venv` memory lesson. Headless
  suite (system python) runs the non-GUI tests including
  `test_update_helper.py` + `test_updater.py`.

## Security Posture & Constraints

- **No change** to the demo-only / no-credentials posture from the manual-login
  feature. The updater handles only the manager wheel; no credentials are
  involved.
- The downloaded wheel is **SHA256-verified** before use (same guarantee as
  today's `install.ps1`). A hash mismatch aborts the update and leaves the
  existing install untouched.
- The helper writes a local `update.log` containing only step markers + exit
  codes (no credentials, no trade data).
- Capture artifacts (pcaps, Frida logs) remain gitignored; untouched.
- Demo accounts only — unchanged; the manual terminal login discipline is
  unaffected.

## Out of Scope

- The copy engine, linkage, transform, snapshot-diff, recovery logic —
  untouched.
- The fresh-install `install.ps1` flow — untouched (still the one-liner install
  path).
- The manual-login / terminal-path-only GUI work from PR #14 — untouched except
  the button label/order.
- A `--wheel <path>` param for `install.ps1` (YAGNI; skipped unless requested).
- Changing the periodic-check interval (stays 1 hour).

## Global Constraints

- Demo accounts only — never a real account (unchanged).
- No credentials are stored, piped, or logged by the manager (unchanged). The
  update log contains no credentials.
- The update wheel is SHA256-verified before install; a mismatch aborts and
  leaves the existing install untouched.
- Tests: headless suite green AND the PySide6 venv GUI suite green (no skips)
  before merge — the headless suite alone is not the gate for GUI work.
- Windows-only (MetaTrader 5 + the existing installer). The helper uses
  Windows process-creation flags; guard non-Windows paths with the existing
  `sys.platform == "win32"` convention.