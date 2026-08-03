# Release, Installer & Auto-Updater — Design

> **Status:** design — pending user review → then `writing-plans` produces the
> implementation plan.

## Goal

Ship the local-manager app to the user the way Claude Code ships: a one-liner
`irm <url> | iex` that installs everything (Python + deps + the app) and creates
a runnable command; re-running the same one-liner updates to the newest version
and relaunches. GitHub Actions builds the release artifacts and publishes them
to GitHub Releases automatically. The GUI also checks for updates, notifies the
user when one is available, and can launch the update path itself.

No compiled binary (no PyInstaller), no `git` or build toolchain required on the
user's machine — just Windows.

## Architecture

```
push to main ──► GitHub Actions ──► tests → version → build wheel → GitHub Release
                                                       assets (stable names):
                                                       install.ps1
                                                       manager-latest.whl
                                                       manager-latest.whl.sha256
                                                       version.txt
                          │
                          ▼  (stable URL: releases/latest/download/<name>)
   ┌──────────────────────────────────────────────────────────────┐
   │ install.ps1  (irm https://.../releases/latest/download/install.ps1 | iex)  │
   │   1. ensure Python ≥3.11 (install per-user if missing)                   │
   │   2. venv at %LOCALAPPDATA%\CopyTradesMT5\venv                            │
   │   3. pip install --upgrade --force-reinstall manager-latest.whl          │
   │   4. create `copytrades` command on PATH + Start Menu shortcut            │
   │   5. (update path) stop running app, upgrade, relaunch                   │
   └──────────────────────────────────────────────────────────────┘
                          │
                          ▼
   copytrades  ──►  venv python -m manager  ──►  GUI (PySide6)
                          │
                          ▼  (hourly + on-demand)
   manager/updater.py  ──►  GET releases/latest/download/version.txt
                          compare to manager._version.__version__
                          if newer → GUI "Update available" + "Update & restart"
                                     ──► spawn detached `irm install.ps1 | iex`
                                         ──► controller.stop() + QApplication.quit()
                                         ──► install.ps1 swaps wheel, relaunches
```

## Components

### 1. `.github/workflows/release.yml` (new)

Trigger: `push` to `main`, and `push` of a `v*` tag (stable). On push to main:
1. Checkout, set up Python 3.12 on `windows-latest`.
2. `pip install -e .` + run `pytest -q` (gate: must be `175 passed, 4 skipped`
   — the existing green baseline).
3. Set the build version: write `manager/_version.py` with
   `__version__ = "0.1.<github.run_number>"` (single source of truth; pyproject
   reads it via `dynamic = ["version"]` + `tool.setuptools.dynamic`).
4. `python -m build --wheel` → `manager-0.1.<n>-py3-none-any.whl` (pure Python,
   no compiler needed).
5. Compute `manager-latest.whl.sha256` (SHA256 of the wheel).
6. Write `version.txt` containing `0.1.<n>`.
7. Tag `v0.1.<n>` and create a GitHub Release with assets under **stable
   filenames** so `releases/latest/download/<name>` always serves the newest:
   - `install.ps1` (copied from `scripts/install.ps1` in the repo)
   - `manager-latest.whl` (the built wheel, renamed)
   - `manager-latest.whl.sha256`
   - `version.txt`

On a `v*` tag push: same build/release steps, but the tag/version comes from the
tag (not the run number), producing a named stable release.

> **Decision:** every push to `main` makes the newest commit the `latest`
> release, so the updater always pulls bleeding-edge — matches "automatically
> update once changes are made."

### 2. `scripts/install.ps1` (new, committed; CI attaches it to each release)

The `irm <url> | iex` target. Idempotent — install and update are the same
command.

Parameters: `-InstallDir` (default `$env:LOCALAPPDATA\CopyTradesMT5`),
`-Yes` (non-interactive), `-SkipLaunch`.

Steps:
1. **Ensure Python ≥3.11.** `python --version`; if missing or `<3.11`, install
   via **winget** (preferred): `winget install --id Python.Python.3.12 -e
   --accept-source-agreements --accept-package-agreements --silent` (winget is
   present on Windows 11 / recent Windows 10; installs per-user or machine per
   winget's default, no manual UAC prompt in the silent path). If winget is
   unavailable, fall back to the official python.org per-user silent installer
   (download `python-3.12.x-amd64.exe`, run `/quiet InstallAllUsers=0
   PrependPath=1 Include_pip=1`, no admin). Refresh the current session PATH.
2. **Venv:** `python -m venv "$InstallDir\venv"` (create if absent; reuse if
   present).
3. **Download + verify wheel:** fetch `manager-latest.whl` and
   `manager-latest.whl.sha256` from
   `https://github.com/resname/copy-trades-MT5/releases/latest/download/`. Verify
   the SHA256; abort (leaving the existing install untouched) on mismatch.
4. **Install/upgrade:** `& "$InstallDir\venv\Scripts\pip.exe" install --upgrade
   --force-reinstall "$wheel"`.
5. **Launcher:** create `$InstallDir\bin\copytrades.cmd` that runs
   `& "$InstallDir\venv\Scripts\python.exe" -m manager @args`. Add `$InstallDir\bin`
   to the user PATH via `setx` (idempotent). Create a Start Menu shortcut
   `CopyTradesMT5.lnk` → `python -m manager`.
6. **Update safety (step 5.0, before reinstall):** detect a running app process
   (a process whose command line contains `manager` running from this venv). If
   found and interactive (no `-Yes`), prompt:
   *"The app is running — stop & update? A live copy session will be
   interrupted."* On decline, abort. On confirm (or `-Yes`), stop the manager
   process gracefully (`taskkill` the manager process; the existing
   `controller.stop()` orderly-shutdown path also runs when the GUI quits).
   Wait for the process to exit before reinstalling.
7. **Relaunch** (unless `-SkipLaunch`): `Start-Process` the launcher detached.

### 3. `manager/_version.py` (new, committed with a dev placeholder)

Committed content: `__version__ = "0.1.0.dev0"`. CI overwrites it at build time
(see §1 step 3) — the overwrite lives only in the build checkout, never
committed back. Single source of truth: `pyproject.toml` uses
`[project] dynamic = ["version"]` and
`[tool.setuptools.dynamic] version = {attr = "manager._version.__version__"}`,
so the built wheel's version and the app's `--version` both read from here.

### 4. `pyproject.toml` (modified)

- Add `[project.scripts] copytrades = "manager.__main__:main"` so
  `pip install` provides a `copytrades` command (in addition to
  `python -m manager`).
- Switch `[project] version = "0.1.0"` → `dynamic = ["version"]` and add
  `[tool.setuptools.dynamic]` reading `manager._version.__version__`.

### 5. `manager/updater.py` (new)

Pure-Python, **no Qt imports** (testable headless), a small HTTP fetch, and a
detached-process spawn. Functions:

- `current_version() -> str` — returns `manager._version.__version__`.
- `parse_version(s) -> tuple[int, ...]` — numeric tuple compare so `0.1.10 >
  0.1.9` (not lex).
- `latest_version(timeout=5.0) -> str | None` — GET
  `https://github.com/resname/copy-trades-MT5/releases/latest/download/version.txt`
  (follows redirects to the latest release's `version.txt`). Returns `None` on
  network/parse failure (never raises).
- `check_for_update(timeout=5.0) -> UpdateInfo` where
  `UpdateInfo(available: bool, current: str, latest: str | None)`. `available`
  is True only when `latest` parses and `parse_version(latest) >
  parse_version(current)`.
- `apply_update_and_restart(on_quit) -> None` — spawns a **detached** PowerShell
  running the `irm <url> | iex` one-liner (so the newest install.ps1 logic
  always runs):
  `subprocess.Popen(["powershell","-NoProfile","-Command",
   "irm https://github.com/resname/copy-trades-MT5/releases/latest/download/install.ps1 | iex"],
   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
   close_fds=True)`
  then calls `on_quit()` (the GUI's orderly shutdown: `controller.stop()` +
  `QApplication.quit()`). The detached install.ps1 polls for the manager process
  to exit, then upgrades + relaunches.
- `INSTALL_PS1_URL`, `VERSION_URL`, `WHEEL_URL`, `WHEEL_SHA_URL` module
  constants (the stable `releases/latest/download/<name>` URLs) so tests can
  monkeypatch them.

### 6. `manager/gui/main_window.py` (modified)

- A **"Check for updates"** action in a `Help`/`Update` menu (and a tray menu
  item). On trigger: run `updater.check_for_update()` off the GUI thread (a
  small `QThread`/`concurrent.futures` worker that posts the result back via a
  Qt signal — reuse the `_StatusBridge` cross-thread pattern), then show the
  result: "Up to date (v0.1.42)" or "Update available: v0.1.43".
- An **"Update available"** indicator (status-bar label + a tray notification
  once) that appears when a check finds a newer version, with an **"Update &
  restart"** button. The button is **enabled only when the copy engine is idle**
  (`controller.is_running()` is False); when the engine is running it is
  disabled with tooltip "Stop copying before updating."
- **"Update & restart"** calls `updater.apply_update_and_restart(on_quit=...)`
  where `on_quit` does `self._controller.stop()` then `QApplication.quit()`.
- **Periodic check:** a `QTimer` checks once ~10 s after launch (to not block
  startup) and every hour thereafter. All checks are off-thread; a failure
  shows "couldn't check for updates" and never disturbs the app.

### 7. `manager/__main__.py` (modified)

- `main(argv)` handles a `--version` flag → print `current_version()` and exit.
- `main(argv)` handles a `update` subcommand → run the update path:
  `updater.apply_update_and_restart(on_quit=lambda: sys.exit(0))` (the CLI
  equivalent of the GUI button, for `copytrades update`).

### 8. `manager/tests/test_updater.py` (new, headless)

- `parse_version` ordering: `0.1.10 > 0.1.9`, equal, `0.1.0.dev0 < 0.1.5`.
- `check_for_update` with HTTP mocked (`monkeypatch` the URL constants and a
  fake `urllib.request.urlopen` returning a `version.txt` body): newer →
  `available=True`; same → `False`; older → `False`; network failure →
  `available=False, latest=None` (no raise).
- `apply_update_and_restart` with `subprocess.Popen` mocked: asserts it is
  called with the powershell one-liner containing `INSTALL_PS1_URL` and the
  detached `creationflags`, and that `on_quit` is called exactly once. Does
  not actually spawn or quit.

### 9. `manager/tests/test_main_window_updates.py` (new, GUI — skips without PySide6)

- "Check for updates" menu action exists and is wired to the updater
  (`updater.check_for_update` mocked).
- "Update & restart" button enabled iff `controller.is_running()` is False.
- A found update toggles the "Update available" indicator.

### 10. CI smoke for `install.ps1` (best-effort, in `release.yml` after release)

A best-effort step (continue-on-error, so it can't block releases) that runs
`install.ps1` against the just-built wheel in a temp dir twice and asserts the
launcher + venv are created and the second run is an in-place upgrade. This
guards the installer logic without requiring a real GUI/MT5.

## Data flow

- **Build → release:** main push → CI versions, builds wheel, attaches stable
  assets. `releases/latest/download/<name>` always serves the newest.
- **Install:** `irm …/install.ps1 | iex` → Python + venv + wheel + launcher.
- **Run:** `copytrades` (PATH) or Start Menu shortcut → `python -m manager`.
- **Check:** GUI QTimer / button → `updater.check_for_update()` (off-thread) →
  GET `version.txt` → compare to `__version__`.
- **Apply:** "Update & restart" (engine idle) →
  `updater.apply_update_and_restart` → detached `irm install.ps1 | iex` →
  `on_quit` (orderly engine stop + app quit) → detached install.ps1 waits for
  exit, reinstalls the latest wheel, relaunches.

## Update safety (trading app)

- **Never half-swap.** The wheel is downloaded to a temp file and SHA256-verified
  before `pip install --force-reinstall`. A failed download/checksum aborts and
  leaves the existing install intact.
- **Engine-idle gate.** The GUI "Update & restart" button is disabled while a
  copy session is running — killing a live mirroring session is dangerous. The
  CLI `copytrades update` and the raw `irm … | iex` one-liner do not enforce
  this (they're manual/external) but `install.ps1` prompts on a detected running
  app before stopping it.
- **Orderly shutdown.** `on_quit` runs `controller.stop()` (which stops the
  supervisor, joins workers, shuts down) before the app exits — workers are not
  orphaned.

## Security

- HTTPS only (GitHub Releases / GitHub API). The wheel is SHA256-verified
  against a checksum asset. No credentials are handled by the updater.
- **Trust model for `irm | iex`:** `install.ps1` is committed to the repo
  (human-reviewable), versioned per release, and served over HTTPS from GitHub
  Releases. This is the same model as the Claude Code installer the user cited.
  The user trusts `resname/copy-trades-MT5` releases (their own repo).

## Error handling

- Python install fails → `install.ps1` prints the python.org download URL and
  exits with a clear message (does not silently fail).
- Wheel download / SHA256 mismatch → abort, existing install untouched.
- Update while running and user declines → abort, no change.
- No network / GitHub API rate-limited → `check_for_update` returns
  `available=False, latest=None`; GUI shows "couldn't check for updates" and
  never crashes.

## Testing

- Existing suite stays green: `175 passed, 4 skipped`.
- New: `test_updater.py` (headless, version compare + mocked HTTP + mocked
  `subprocess.Popen`) and `test_main_window_updates.py` (GUI, skips without
  PySide6).
- Best-effort CI smoke for `install.ps1` idempotency.

## File layout (added/modified)

```
.github/workflows/release.yml      NEW  build → wheel → release (auto on main)
scripts/install.ps1                NEW  installer/updater (committed; attached to each release)
manager/_version.py                NEW  __version__ (dev placeholder; CI overwrites at build)
manager/updater.py                 NEW  check_for_update + apply_update_and_restart (no Qt)
manager/gui/main_window.py         MOD  Check for updates + Update available + Update & restart
manager/__main__.py                MOD  --version + `update` subcommand
manager/tests/test_updater.py      NEW  headless updater tests
manager/tests/test_main_window_updates.py  NEW  GUI update UI (skip w/o PySide6)
pyproject.toml                     MOD  [project.scripts] copytrades; dynamic version
```

## Decisions (resolved with user)

1. **Release trigger:** **every push to `main` becomes the `latest` release**
   (bleeding-edge auto-update). The `v*`-tag path still works as a secondary
   stable-release trigger but is not required.
2. **Python install method in `install.ps1`:** **winget-first**
   (`winget install --id Python.Python.3.12 -e --silent …`), with the official
   python.org per-user silent installer as a fallback when winget is missing.

## Self-review

- **Placeholder scan:** none — all sections concrete; URLs, filenames, and
  signatures are specified.
- **Internal consistency:** the stable-filename scheme (`install.ps1`,
  `manager-latest.whl`, `version.txt`) is consistent across §1, §2, §5; the
  detached-spawn + `on_quit` pattern is consistent across §5, §6, §7; the
  engine-idle gate is consistent across §6 and "Update safety."
- **Scope:** single implementation plan — build/CI, installer, updater module,
  GUI wiring, tests. No compiled-binary / PyInstaller scope creep.
- **Ambiguity:** the two open decisions above are the only judgment calls;
  defaults are chosen and flagged for review.