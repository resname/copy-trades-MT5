# Release, Installer & Auto-Updater Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the local-manager app the Claude-Code way — a one-liner `irm <url> | iex` that installs everything (Python + deps + the app) and creates a runnable `copytrades` command; re-running the same one-liner updates to the newest version and relaunches. GitHub Actions builds a pure-Python wheel and publishes it to GitHub Releases automatically on every push to `main`. The GUI checks for updates, notifies when one is available, and an "Update & restart" button launches the update path.

**Architecture:** No compiled binary (no PyInstaller), no `git`/build toolchain on the user's machine. CI builds a wheel and attaches it (under stable filenames) to each GitHub Release so `releases/latest/download/<name>` always serves the newest. `scripts/install.ps1` (winget-first Python install → venv → pip install wheel → `copytrades` launcher + Start Menu shortcut) is the `irm | iex` target and is idempotent (install = update). `manager/updater.py` (no Qt) compares the installed `__version__` to the latest `version.txt` and spawns a detached `irm install.ps1 | iex` then quits, so the installer can swap the wheel and relaunch.

**Tech Stack:** Python ≥3.11, PySide6/Qt, pywin32, psutil, MetaTrader5; `python -m build` (wheel); GitHub Actions (`windows-latest`); PowerShell 5.1+ (`install.ps1`); winget (Python bootstrap).

## Global Constraints

- **Windows-only runtime** (MetaTrader5, pywin32, DPAPI). The updater is headless but the app runs on Windows.
- **`manager/updater.py` has ZERO Qt imports** — it must be unit-testable in the headless env (PySide6 not installed). GUI wiring lives in `main_window.py` (skips without PySide6).
- **Existing suite must stay green:** `175 passed, 4 skipped` before and after every task.
- **PySide6 not installed in the test env (intentional).** GUI tests use `pytest.importorskip("PySide6")`. Do NOT pip install.
- **Demo accounts only; credentials via pipe not argv; DPAPI at rest** — these existing constraints are untouched by this plan; the updater handles no credentials.
- **Stable release asset filenames:** `install.ps1`, `manager-latest.whl`, `manager-latest.whl.sha256`, `version.txt` — all served by `https://github.com/resname/copy-trades-MT5/releases/latest/download/<name>`.
- **Release trigger:** every push to `main` becomes the `latest` release (bleeding-edge). `v*` tag pushes also create a named release (secondary).
- **Python install method:** winget-first (`winget install --id Python.Python.3.12 -e --silent …`), official python.org silent installer as fallback.
- **Single source of truth for version:** `manager/_version.py.__version__`. `pyproject.toml` uses `dynamic = ["version"]` reading it; the built wheel and the app's `--version` both read it. CI overwrites `_version.py` at build time (never committed back); the committed value is `0.1.0.dev0`.
- **Security:** HTTPS only; SHA256-verify the wheel before install; never half-swap (failed download leaves the existing install intact).

---

## File Structure

```
manager/_version.py                 NEW  __version__ = "0.1.0.dev0" (CI overwrites at build)
manager/updater.py                   NEW  check_for_update + apply_update_and_restart (no Qt)
manager/__main__.py                  MOD  --version + `update` subcommand
manager/gui/main_window.py           MOD  Check for updates + Update available + Update & restart
manager/tests/test_version.py        NEW
manager/tests/test_updater.py        NEW  (headless)
manager/tests/test_main_entry.py     MOD  add --version + update tests
manager/tests/test_main_window_updates.py  NEW  (GUI, skip w/o PySide6)
scripts/install.ps1                  NEW  installer/updater (committed; attached to each release)
scripts/smoke-install.ps1            NEW  local smoke for install.ps1
.github/workflows/release.yml        NEW  build wheel → release (auto on main)
pyproject.toml                       MOD  [project.scripts] copytrades; dynamic version
```

---

### Task 1: Version infrastructure

**Files:**
- Create: `manager/_version.py`
- Modify: `pyproject.toml`
- Test: `manager/tests/test_version.py`

**Interfaces:**
- Produces: `manager._version.__version__` (str); the `copytrades` console script; a wheel whose version comes from `_version.py`.

- [ ] **Step 1: Write the failing test**

```python
# manager/tests/test_version.py
def test_version_is_string():
    from manager._version import __version__
    assert isinstance(__version__, str)
    assert __version__ and __version__[0].isdigit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q manager/tests/test_version.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'manager._version'`

- [ ] **Step 3: Write the implementation**

```python
# manager/_version.py
"""Single source of truth for the installed version.

Committed value is a dev placeholder. CI overwrites this file at build time
with the real version (e.g. ``0.1.42``) before building the wheel; the
overwrite is never committed back. pyproject reads it via
``tool.setuptools.dynamic`` so the wheel version and the app's ``--version``
both come from here.
"""
__version__ = "0.1.0.dev0"
```

Modify `pyproject.toml`: remove the static `version = "0.1.0"` line from `[project]`, add `dynamic = ["version"]` to `[project]`, add a `[project.scripts]` table, and add `[tool.setuptools.dynamic]`. The resulting relevant sections:

```toml
[project]
name = "copy-trades-mt5-manager"
dynamic = ["version"]
requires-python = ">=3.11"
dependencies = [
    "PySide6>=6.6",
    "pywin32>=306",
    "psutil>=5.9",
    "MetaTrader5>=5.0.45",
]

[project.scripts]
copytrades = "manager.__main__:main"

[tool.setuptools.dynamic]
version = {attr = "manager._version.__version__"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q manager/tests/test_version.py`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: `176 passed, 4 skipped` (175 + the new test_version).

- [ ] **Step 6: Commit**

```bash
git add manager/_version.py manager/tests/test_version.py pyproject.toml
git commit -m "feat(build): version infra (_version.py + dynamic version + copytrades script)"
```

---

### Task 2: Updater module (headless, no Qt)

**Files:**
- Create: `manager/updater.py`
- Test: `manager/tests/test_updater.py`

**Interfaces:**
- Consumes: `manager._version.__version__`
- Produces: `UpdateInfo(available, current, latest)`, `current_version()`, `parse_version(s)`, `latest_version(timeout)`, `check_for_update(timeout)`, `apply_update_and_restart(on_quit)`; module constants `INSTALL_PS1_URL`, `VERSION_URL`, `WHEEL_URL`, `WHEEL_SHA_URL`.

- [ ] **Step 1: Write the failing tests**

```python
# manager/tests/test_updater.py
import subprocess
from unittest.mock import MagicMock

import pytest

from manager import updater
from manager.updater import UpdateInfo, parse_version, check_for_update, apply_update_and_restart


def test_parse_version_numeric_not_lex():
    assert parse_version("0.1.10") > parse_version("0.1.9")
    assert parse_version("0.1.5") == parse_version("0.1.5")


def test_parse_version_drops_suffix():
    assert parse_version("0.1.0.dev0") == (0, 1, 0)
    assert parse_version("0.1.42") == (0, 1, 42)


def test_current_version_reads_module():
    from manager._version import __version__
    assert updater.current_version() == __version__


def test_check_for_update_available(monkeypatch):
    monkeypatch.setattr(updater, "_fetch_text", lambda url, t: "0.1.99")
    info = check_for_update()
    assert info.available is True
    assert info.latest == "0.1.99"
    assert info.current == updater.current_version()


def test_check_for_update_same_version(monkeypatch):
    monkeypatch.setattr(updater, "_fetch_text",
                        lambda url, t: updater.current_version())
    info = check_for_update()
    assert info.available is False


def test_check_for_update_network_failure(monkeypatch):
    monkeypatch.setattr(updater, "_fetch_text", lambda url, t: None)
    info = check_for_update()
    assert info.available is False
    assert info.latest is None


def test_apply_update_and_restart_spawns_and_quits(monkeypatch):
    calls = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    quit_called = []
    apply_update_and_restart(on_quit=lambda: quit_called.append(True))
    assert len(calls) == 1
    _args, kwargs = calls[0]
    cmd = kwargs.get("args") or (_args[0] if _args else None)
    assert cmd is not None
    assert any("install.ps1" in str(part) for part in cmd)
    assert quit_called == [True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q manager/tests/test_updater.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'manager.updater'`

- [ ] **Step 3: Write the implementation**

```python
# manager/updater.py
"""Update checking + applying for the local manager.

Headless (no Qt). Compares the installed ``manager._version.__version__`` to
the latest release's ``version.txt`` on GitHub Releases. ``apply_update_and_restart``
spawns a detached PowerShell running ``irm <install.ps1> | iex`` (so the newest
installer logic always runs) and then calls ``on_quit`` so the caller can stop
the engine and exit; the detached installer waits for this process to exit,
reinstalls the latest wheel, and relaunches.
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from dataclasses import dataclass

REPO = "resname/copy-trades-MT5"
BASE = f"https://github.com/{REPO}/releases/latest/download"
INSTALL_PS1_URL = f"{BASE}/install.ps1"
VERSION_URL = f"{BASE}/version.txt"
WHEEL_URL = f"{BASE}/manager-latest.whl"
WHEEL_SHA_URL = f"{BASE}/manager-latest.whl.sha256"


@dataclass
class UpdateInfo:
    available: bool
    current: str
    latest: str | None


def parse_version(s: str) -> tuple[int, ...]:
    """Numeric tuple compare so ``0.1.10 > 0.1.9`` (not lex). Drops non-numeric
    suffixes (``0.1.0.dev0`` -> ``(0, 1, 0)``)."""
    parts: list[int] = []
    for tok in str(s).strip().split("."):
        num = ""
        for ch in tok:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def current_version() -> str:
    from manager._version import __version__
    return __version__


def _fetch_text(url: str, timeout: float) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return None


def latest_version(timeout: float = 5.0) -> str | None:
    return _fetch_text(VERSION_URL, timeout)


def check_for_update(timeout: float = 5.0) -> UpdateInfo:
    cur = current_version()
    latest = latest_version(timeout)
    available = False
    if latest is not None:
        try:
            available = parse_version(latest) > parse_version(cur)
        except Exception:
            available = False
    return UpdateInfo(available=available, current=cur, latest=latest)


def apply_update_and_restart(on_quit) -> None:
    """Spawn a detached ``irm INSTALL_PS1_URL | iex`` (newest installer logic),
    then call ``on_quit()`` so the caller stops the engine and exits. The
    detached installer polls for this process to exit, swaps the wheel, and
    relaunches."""
    cmd = ["powershell", "-NoProfile", "-Command",
           f"irm {INSTALL_PS1_URL} | iex"]
    kwargs: dict = {"close_fds": True}
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    subprocess.Popen(cmd, **kwargs)
    on_quit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q manager/tests/test_updater.py`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: `183 passed, 4 skipped` (176 + 7 new updater tests).

- [ ] **Step 6: Commit**

```bash
git add manager/updater.py manager/tests/test_updater.py
git commit -m "feat(updater): version compare + check_for_update + apply_update_and_restart (no Qt)"
```

---

### Task 3: CLI `--version` + `update` subcommand

**Files:**
- Modify: `manager/__main__.py`
- Modify: `manager/tests/test_main_entry.py`

**Interfaces:**
- Consumes: `manager.updater.apply_update_and_restart`, `manager._version.__version__`
- Produces: `main(["--version"])` prints version and returns 0; `main(["update"])` spawns the update and returns 0.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_main_entry.py` (after the existing tests; the file already has `pytest.importorskip("PySide6")` at module top):

```python
def test_main_version_flag(capsys):
    import manager.__main__ as entry
    from manager._version import __version__
    rc = entry.main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert __version__ in out


def test_main_update_subcommand(monkeypatch):
    import manager.__main__ as entry
    import manager.updater as updater
    called = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit: called.append(on_quit))
    rc = entry.main(["update"])
    assert rc == 0
    assert len(called) == 1  # on_quit passed through, not invoked here
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q manager/tests/test_main_entry.py`
Expected: FAIL — `--version` returns nonzero / no output; `update` not handled.

- [ ] **Step 3: Write the implementation**

Modify `main()` in `manager/__main__.py`. Keep the existing `build_app_graph`, `_StatusBridge`, and the GUI path exactly; only add subcommand handling at the top of `main()` and split argv cleanly. The new `main`:

```python
def main(argv=None) -> int:
    if argv is None:
        args = sys.argv[1:]
        gui_args = sys.argv
    else:
        args = list(argv)
        gui_args = list(argv)

    if "--version" in args:
        from manager._version import __version__
        print(__version__)
        return 0
    if args and args[0] == "update":
        from manager import updater
        updater.apply_update_and_restart(on_quit=lambda: sys.exit(0))
        return 0

    app = QApplication.instance() or QApplication(gui_args)
    window, tray, controller, bridge = build_app_graph(app)
    window.show()
    return app.exec()
```

`_StatusBridge`, `build_app_graph`, and the `if __name__ == "__main__":` guard stay unchanged. The top-level `from PySide6...` imports stay (the module is GUI-scoped; `--version`/`update` are exercised under importorskip in tests and work at runtime because the installer installed PySide6).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q manager/tests/test_main_entry.py`
Expected: PASS (existing 2 + 2 new). Note: these skip without PySide6 (module-level importorskip).

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: `185 passed, 4 skipped` (183 + 2 new).

- [ ] **Step 6: Commit**

```bash
git add manager/__main__.py manager/tests/test_main_entry.py
git commit -m "feat(cli): --version and `update` subcommand"
```

---

### Task 4: GUI update UI (check + notify + update & restart)

**Files:**
- Modify: `manager/gui/main_window.py`
- Test: `manager/tests/test_main_window_updates.py`

**Interfaces:**
- Consumes: `manager.updater.check_for_update`, `manager.updater.apply_update_and_restart`, `controller.is_running()`, `controller.stop()`.
- Produces: `MainWindow` gains `update_label`, `check_update_button`, `update_restart_button`; methods `check_for_updates_now()`, `_on_update_checked(info)`, `_on_update_restart()`, `_do_update_quit()`; a periodic `QTimer`.

- [ ] **Step 1: Write the failing tests**

```python
# manager/tests/test_main_window_updates.py
import pytest

pytest.importorskip("PySide6")

from manager.updater import UpdateInfo


class FakeController:
    def __init__(self, running=False):
        self._running = running
        self.stopped = []
    def is_running(self):
        return self._running
    def stop(self):
        self.stopped.append(True)
    def discover_instances(self):
        return []


def test_update_ui_exists(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.check_update_button.text().lower().startswith("check")
    assert w.update_restart_button.isVisible() is False


def test_update_available_enables_restart_only_when_idle(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert "0.1.2" in w.update_label.text()
    assert w.update_restart_button.isVisible() is True
    assert w.update_restart_button.isEnabled() is True


def test_update_available_disables_restart_while_running(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController(running=True))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert w.update_restart_button.isEnabled() is False


def test_up_to_date_hides_restart(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._on_update_checked(UpdateInfo(available=False, current="0.1.1", latest="0.1.1"))
    assert "up to date" in w.update_label.text().lower()
    assert w.update_restart_button.isVisible() is False


def test_check_failed_label(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._on_update_checked(UpdateInfo(available=False, current="0.1.1", latest=None))
    assert "couldn't" in w.update_label.text().lower()


def test_update_restart_calls_updater_and_quits(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit: calls.append(on_quit))
    w = MainWindow(FakeController(running=False))
    w._on_update_restart()
    assert len(calls) == 1
    # the on_quit passed in is the window's _do_update_quit (bound method)
    assert calls[0] == w._do_update_quit


def test_update_restart_refuses_while_running(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    import manager.updater as updater
    calls = []
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit: calls.append(on_quit))
    w = MainWindow(FakeController(running=True))
    w._on_update_restart()
    assert calls == []  # refused; nothing spawned
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q manager/tests/test_main_window_updates.py`
Expected: FAIL — `MainWindow` has no `check_update_button` / `update_restart_button`.

- [ ] **Step 3: Write the implementation**

Modify `manager/gui/main_window.py`:

(a) Update imports to include `QTimer`, `QThread`:

```python
from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
)
```

(b) Add a module-level worker class above `MainWindow`:

```python
class _UpdateWorker(QThread):
    """Runs updater.check_for_update off the GUI thread; emits the UpdateInfo
    on done (Qt marshals the signal back to the GUI thread)."""
    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        self.done.emit(self._fn())
```

(c) In `_build_ui`, after the `controls` layout and before the Status `QLabel`, add an updates row:

```python
        # Updates
        self.update_label = QLabel("")
        self.check_update_button = QPushButton("Check for updates")
        self.update_restart_button = QPushButton("Update & restart")
        self.update_restart_button.setVisible(False)
        updates_row = QHBoxLayout()
        updates_row.addWidget(self.update_label, 1)
        updates_row.addWidget(self.check_update_button)
        updates_row.addWidget(self.update_restart_button)
```

and add `root.addLayout(updates_row)` after `root.addLayout(controls)`.

(d) Wire the buttons (next to the other `clicked.connect` calls):

```python
        self.check_update_button.clicked.connect(self.check_for_updates_now)
        self.update_restart_button.clicked.connect(self._on_update_restart)
```

(e) In `__init__`, after `_populate_terminals()`, start the periodic timer (no event loop in tests → never fires; safe):

```python
        self._update_worker = None
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(3600 * 1000)
        self._update_timer.timeout.connect(self.check_for_updates_now)
        self._update_timer.start()
        QTimer.singleShot(10_000, self.check_for_updates_now)
```

(f) Add the update methods (after `set_running`):

```python
    # ---- updates ----
    def check_for_updates_now(self) -> None:
        from manager import updater
        self.update_label.setText("Checking for updates…")
        self._update_worker = _UpdateWorker(updater.check_for_update, self)
        self._update_worker.done.connect(self._on_update_checked)
        self._update_worker.start()

    def _on_update_checked(self, info) -> None:
        self._update_worker = None
        if info.latest is None and not info.available:
            self.update_label.setText("Couldn't check for updates")
            self.update_restart_button.setVisible(False)
            return
        if info.available:
            self.update_label.setText(f"Update available: v{info.latest}")
            self.update_restart_button.setVisible(True)
            self.update_restart_button.setEnabled(not self._controller.is_running())
        else:
            self.update_label.setText(f"Up to date (v{info.current})")
            self.update_restart_button.setVisible(False)

    def _on_update_restart(self) -> None:
        if self._controller.is_running():
            self.append_log("stop copying before updating")
            return
        from manager import updater
        updater.apply_update_and_restart(on_quit=self._do_update_quit)

    def _do_update_quit(self) -> None:
        self._controller.stop()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q manager/tests/test_main_window_updates.py`
Expected: PASS (7 tests). (Skips without PySide6.)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: `192 passed, 4 skipped` (185 + 7 new GUI tests, which skip without PySide6 → still `4 skipped` because the module-level importorskip collapses the whole module into 1 skip; the 4 skipped count is per-module. The non-GUI count grows to 192-... — run and confirm no failures; exact passed count = previous 185 + the 7 new tests if PySide6 present, else +0 passed and the skipped count stays 4). The gate is: **no new failures**, suite green.

- [ ] **Step 6: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window_updates.py
git commit -m "feat(gui): update check + Update available + Update & restart (engine-idle gated)"
```

---

### Task 5: `install.ps1` installer/updater

**Files:**
- Create: `scripts/install.ps1`
- Create: `scripts/smoke-install.ps1`

**Interfaces:**
- Consumes: the GitHub Releases stable assets `manager-latest.whl`, `manager-latest.whl.sha256` (and is itself attached as `install.ps1`).
- Produces: `%LOCALAPPDATA%\CopyTradesMT5\venv`, `…\bin\copytrades.cmd`, a Start Menu shortcut, an installed `copy-trades-mt5-manager` wheel.

This task is a PowerShell script; it is verified by `scripts/smoke-install.ps1` (run locally on Windows) and by the release workflow's smoke step (Task 6), not by pytest.

- [ ] **Step 1: Write `scripts/install.ps1`**

```powershell
#requires -Version 5.1
<#
.SYNOPSIS
  Install or update the CopyTrades MT5 local manager (Claude-Code style).
.DESCRIPTION
  Idempotent: run once to install, run again to update. Ensures Python >=3.11
  (winget-first, python.org fallback), creates a venv, pip-installs the latest
  wheel from GitHub Releases (SHA256-verified), and creates a `copytrades`
  command on PATH + a Start Menu shortcut. On an update with the app running,
  prompts before stopping it (a live copy session would be interrupted).
.EXAMPLE
  irm https://github.com/resname/copy-trades-MT5/releases/latest/download/install.ps1 | iex
#>
[CmdletBinding()]
param(
  [string]$InstallDir = "$env:LOCALAPPDATA\CopyTradesMT5",
  [switch]$Yes,
  [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"
$Repo = "resname/copy-trades-MT5"
$Base = "https://github.com/$Repo/releases/latest/download"
$WheelUrl = "$Base/manager-latest.whl"
$ShaUrl = "$Base/manager-latest.whl.sha256"

function Resolve-Py {
  foreach ($c in @("python", "py")) {
    try {
      $v = & $c --version 2>$null
      if ($v -match "Python (\d+)\.(\d+)") {
        $maj = [int]$Matches[1]; $min = [int]$Matches[2]
        if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
          return (Get-Command $c).Source
        }
      }
    } catch {}
  }
  return $null
}

# 1. Ensure Python >=3.11
$PyExe = Resolve-Py
if (-not $PyExe) {
  Write-Host "Python >=3.11 not found. Installing..."
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if ($winget) {
    winget install --id Python.Python.3.12 -e --silent `
      --accept-source-agreements --accept-package-agreements
  } else {
    $inst = "$env:TEMP\python-3.12.7-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $inst
    Start-Process -FilePath $inst -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_pip=1" -Wait
  }
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
  $PyExe = Resolve-Py
  if (-not $PyExe) {
    throw "Python install failed. Install Python >=3.11 from https://www.python.org/downloads/ then re-run."
  }
}

# 2. venv
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Venv = Join-Path $InstallDir "venv"
if (-not (Test-Path $Venv)) {
  Write-Host "Creating venv at $Venv"
  & $PyExe -m venv $Venv
}
$Pip = Join-Path $Venv "Scripts\pip.exe"
$PyVenv = Join-Path $Venv "Scripts\python.exe"

# 3. Stop a running app before reinstall (update safety)
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match "manager" -and $_.CommandLine -match ([regex]::Escape($Venv)) })
if ($running.Count -gt 0) {
  if (-not $Yes) {
    $choice = Read-Host "The app is running. Stop & update? A live copy session will be interrupted. [y/N]"
    if ($choice -notmatch "^[yY]") { Write-Host "Aborted."; return }
  }
  foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
  for ($i = 0; $i -lt 20; $i++) {
    $still = $false
    foreach ($p in $running) { if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) { $still = $true } }
    if (-not $still) { break }
    Start-Sleep -Milliseconds 500
  }
}

# 4. Download + SHA256-verify wheel
$tmp = Join-Path $env:TEMP ("copytrades-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$wheelPath = Join-Path $tmp "manager-latest.whl"
Invoke-WebRequest $WheelUrl -OutFile $wheelPath
$expected = ((Invoke-WebRequest $ShaUrl).Content.Trim() -split '\s+')[0]
$actual = (Get-FileHash $wheelPath -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected.ToLower()) {
  Write-Error "Wheel checksum mismatch (expected $expected got $actual). Aborting; existing install untouched."
  return
}

# 5. Install/upgrade
Write-Host "Installing/upgrading..."
& $Pip install --upgrade --force-reinstall $wheelPath

# 6. Launcher + PATH
$Bin = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$Cmd = Join-Path $Bin "copytrades.cmd"
@"
@echo off
"$PyVenv" -m manager %*
"@ | Set-Content -Path $Cmd -Encoding ASCII
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$Bin*") {
  [System.Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ";$Bin"), "User")
  $env:Path = "$env:Path;$Bin"
}

# 7. Start Menu shortcut
$Lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\CopyTradesMT5.lnk"
try {
  $ws = New-Object -ComObject WScript.Shell
  $sc = $ws.CreateShortcut($Lnk)
  $sc.TargetPath = $PyVenv
  $sc.Arguments = "-m manager"
  $sc.WorkingDirectory = $InstallDir
  $sc.Description = "CopyTrades MT5 Local Manager"
  $sc.Save()
} catch { Write-Warning "Could not create Start Menu shortcut: $_" }

Write-Host "Installed. Run with: copytrades  (or Start Menu: CopyTradesMT5)"

if (-not $SkipLaunch) {
  Start-Process -FilePath $PyVenv -ArgumentList "-m", "manager" -WorkingDirectory $InstallDir
  Write-Host "Launched."
}
```

- [ ] **Step 2: Write `scripts/smoke-install.ps1` (local verification)**

```powershell
#requires -Version 5.1
# Local smoke for install.ps1: runs it twice into a temp dir (idempotent) and
# checks the launcher exists. Run: powershell -File scripts/smoke-install.ps1
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:TEMP ("ct-smoke-" + [guid]::NewGuid().ToString("N"))
powershell -File "$PSScriptRoot\install.ps1" -InstallDir $dir -Yes -SkipLaunch
powershell -File "$PSScriptRoot\install.ps1" -InstallDir $dir -Yes -SkipLaunch
$launcher = Join-Path $dir "bin\copytrades.cmd"
if (-not (Test-Path $launcher)) { throw "smoke FAILED: launcher missing at $launcher" }
Write-Host "smoke OK: $launcher exists; install.ps1 is idempotent"
```

- [ ] **Step 3: Run the smoke locally**

Run: `powershell -File scripts/smoke-install.ps1`
Expected: `smoke OK: ...launcher exists; install.ps1 is idempotent` (requires Python ≥3.11 present and network to fetch the latest release). If the latest release does not exist yet (pre-Task-6), this is expected to fail on the wheel download — note it and proceed; the definitive verification is the release workflow's smoke in Task 6.

- [ ] **Step 4: Run the full pytest suite (unchanged)**

Run: `pytest -q`
Expected: `192 passed, 4 skipped` (no Python code changed; scripts are not imported).

- [ ] **Step 5: Commit**

```bash
git add scripts/install.ps1 scripts/smoke-install.ps1
git commit -m "feat(install): install.ps1 — winget-first Python, venv, wheel, copytrades launcher (idempotent)"
```

---

### Task 6: GitHub Actions release workflow

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: `pyproject.toml` (build), `manager/_version.py` (overwritten), `scripts/install.ps1` (attached as `install.ps1`).
- Produces: a GitHub Release per main push, with `install.ps1`, `manager-latest.whl`, `manager-latest.whl.sha256`, `version.txt`, tagged `v0.1.<run_number>`.

This task is verified by the workflow running on push (and the smoke step), not by pytest.

- [ ] **Step 1: Write `.github/workflows/release.yml`**

```yaml
name: release

on:
  push:
    branches: [main]
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  release:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install
        run: pip install -e .

      - name: Test
        run: pytest -q

      - name: Set version
        shell: bash
        run: |
          if [ "${GITHUB_REF_TYPE}" = "tag" ]; then
            VER="${GITHUB_REF_NAME#v}"
          else
            VER="0.1.${GITHUB_RUN_NUMBER}"
          fi
          printf '__version__ = "%s"\n' "$VER" > manager/_version.py
          echo "BUILD_VERSION=$VER" >> "$GITHUB_ENV"

      - name: Build wheel
        run: |
          pip install build
          python -m build --wheel

      - name: Stage release assets (stable names)
        shell: pwsh
        run: |
          $whl = Get-ChildItem dist\*.whl | Select-Object -First 1
          Copy-Item $whl.FullName dist\manager-latest.whl
          (Get-FileHash $whl.FullName -Algorithm SHA256).Hash.ToLower() |
            Out-File -Encoding ascii -NoNewfile dist\manager-latest.whl.sha256
          "$env:BUILD_VERSION" | Out-File -Encoding ascii -NoNewfile dist\version.txt
          Copy-Item scripts/install.ps1 dist\install.ps1

      - name: Create release
        shell: bash
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh release create "v$BUILD_VERSION" \
            dist/install.ps1 dist/manager-latest.whl dist/manager-latest.whl.sha256 dist/version.txt \
            --target "$GITHUB_SHA" --title "v$BUILD_VERSION" \
            --notes "Auto-built release v$BUILD_VERSION from $GITHUB_SHA"

      - name: install.ps1 smoke (best-effort)
        continue-on-error: true
        shell: pwsh
        run: |
          $dir = Join-Path $env:RUNNER_TEMP "ct-smoke"
          powershell -File scripts/install.ps1 -InstallDir $dir -Yes -SkipLaunch
          if (-not (Test-Path (Join-Path $dir "bin\copytrades.cmd"))) { throw "launcher missing" }
```

- [ ] **Step 2: Validate the workflow locally (syntax only)**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/release.yml'))"` (if PyYAML is available; otherwise skip — the workflow's syntax is exercised on push).
Expected: no exception.

- [ ] **Step 3: Run the full pytest suite (unchanged)**

Run: `pytest -q`
Expected: `192 passed, 4 skipped`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): build wheel + publish GitHub Release (auto on main) with install.ps1 smoke"
```

- [ ] **Step 5: Push and observe the first release**

Push the branch; on merge to `main` the workflow runs and creates `v0.1.<N>`. Verify `releases/latest/download/install.ps1`, `manager-latest.whl`, `version.txt` are reachable, then run the install one-liner end-to-end.

---

## Self-Review

1. **Spec coverage:** Every spec component maps to a task — `_version.py`+pyproject (T1, §1,3,4); `updater.py` (T2, §5); `__main__` `--version`/`update` (T3, §7); GUI update UI (T4, §6); `install.ps1` (T5, §2); `release.yml` (T6, §1). Stable-asset scheme (§1) is in T6's stage step. Engine-idle gate (§4/§6) is in T4's `_on_update_restart`. SHA256 verify (§4) is in T5. Winget-first (§2 decision) is in T5. Release trigger every-push-to-main (§1 decision) is in T6's `on: push: branches: [main]`.

2. **Placeholder scan:** None — every step has concrete code, exact filenames, signatures, and test cases.

3. **Type consistency:** `UpdateInfo(available, current, latest)` is identical across `updater.py` (T2) and the GUI tests (T4). `apply_update_and_restart(on_quit)` signature is identical across `updater.py` (T2), `__main__` (T3, `on_quit=lambda: sys.exit(0)`), and `main_window` (T4, `on_quit=self._do_update_quit`). `current_version()`/`parse_version()` consistent across T2 tests and impl. `controller.is_running()`/`stop()` are the existing controller API (verified in Plan 4). `check_for_update()` takes `timeout=5.0` and returns `UpdateInfo` consistently.

4. **Test-count note:** the "expected passed counts" (176 → 183 → 185 → 192) assume PySide6 is absent so GUI tests skip and only headless tests add up; the hard gate is "no new failures, suite green." Task 4's GUI tests live under one module-level `importorskip` so they contribute 1 skip (not 7), keeping the skipped count at 4.

5. **Known infra caveat:** Tasks 5 and 6 produce PowerShell/YAML verified by execution (local smoke + the workflow run on push), not by pytest — consistent with how Plan 4's docs task was handled. The implementer/reviewer should not require a pytest test for these.