# Auto-start on Boot + 15 s Auto-Copy Countdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two toggleable settings to the manager — launch the app at Windows login (via a Startup-folder `.lnk`), and auto-start copying 15 s after every app launch with a cancellable countdown — both off by default.

**Architecture:** A new Qt-free `manager/platform/autostart.py` manages the Windows Startup shortcut (PowerShell + WScript.Shell COM, no new deps). `MainWindow` gains an "Auto-start" group box with two checkboxes + a dedicated Cancel button, a 15 s `QTimer` countdown on launch, and a shared `_do_start(show_modal)` Start body so manual Start keeps its Algo-Trading modal while the auto-start path logs only (no modal). The two toggles persist in the existing `config` blob under an `autostart` sub-dict.

**Tech Stack:** PySide6/Qt (`QCheckBox`, `QTimer`, `QPushButton`), Python 3.11+ stdlib (`subprocess`, `os`, `pathlib`), PowerShell 5.1 (present on every supported Windows 10/11), pytest (GUI tests use the PySide6 venv at `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe`).

## Global Constraints

- Work directly on `main` (standing user instruction — no worktree, no feature branch).
- GUI tests run on the PySide6 venv: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q`. The full suite must stay green (the pre-existing `_slave_loop` teardown `PytestUnhandledThreadExceptionWarning` is a warning, not a failure).
- `gh` CLI is not on PATH — call `C:\Program Files\GitHub CLI\gh.exe` by full path (only relevant at push/release time, after the final review).
- Demo accounts only; no credentials stored/piped/logged (N/A for this change, but do not introduce any).
- `QApplication` is already imported at the top of `manager/gui/main_window.py`; `QTimer` is already imported (`from PySide6.QtCore import Qt, Signal, QTimer, QThread`). `QCheckBox` is NOT yet imported — add it. `sys` is NOT yet imported in `main_window.py` — add it.
- `controller.start()` is blocking and safe on the GUI thread (same as manual Start today). Do not make it async.
- The `config` blob is treated opaquely by `SettingsStore.save_config`/`load_config` — adding an `autostart` sub-dict requires no store changes.
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1: OS autostart helper (`manager/platform/autostart.py`)

**Files:**
- Create: `manager/platform/__init__.py` (empty package marker)
- Create: `manager/platform/autostart.py`
- Test: `manager/tests/test_autostart.py`

**Interfaces:**
- Produces: `manager.platform.autostart.startup_lnk_path() -> pathlib.Path`, `is_autostart_enabled() -> bool`, `enable_autostart(target_exe: str, arguments: str = "-m manager", working_dir: str | None = None) -> None`, `disable_autostart() -> None`, and `class AutostartError(RuntimeError)`. Task 2 imports this module as `from manager.platform import autostart`.

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_autostart.py`:

```python
import subprocess
from pathlib import Path

import pytest

from manager.platform import autostart
from manager.platform.autostart import AutostartError


def test_startup_lnk_path_is_a_path_named_copytradesmt5_lnk():
    p = autostart.startup_lnk_path()
    assert isinstance(p, Path)
    assert p.name == "CopyTradesMT5.lnk"
    # deterministic
    assert autostart.startup_lnk_path() == p


def test_is_autostart_enabled_reflects_file_existence(monkeypatch, tmp_path):
    lnk = tmp_path / "CopyTradesMT5.lnk"
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    assert autostart.is_autostart_enabled() is False
    lnk.touch()
    assert autostart.is_autostart_enabled() is True


def test_enable_autostart_runs_powershell_with_quoted_target(monkeypatch, tmp_path):
    lnk = tmp_path / "CopyTradesMT5.lnk"
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    autostart.enable_autostart("C:/some path/pythonw.exe", "-m manager")
    cmd = captured["cmd"]
    assert cmd[0] == "powershell"
    assert cmd[1] == "-NoProfile"
    assert cmd[2] == "-Command"
    script = cmd[3]
    # target exe path is single-quoted in the PowerShell script
    assert "'C:/some path/pythonw.exe'" in script
    assert "-m manager" in script
    assert "WScript.Shell" in script
    assert captured["kw"]["check"] is True


def test_enable_autostart_raises_autostart_error_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "CopyTradesMT5.lnk")

    def raising_run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(autostart.subprocess, "run", raising_run)
    with pytest.raises(AutostartError):
        autostart.enable_autostart("C:/py/pythonw.exe")


def test_disable_autostart_is_idempotent(monkeypatch, tmp_path):
    lnk = tmp_path / "CopyTradesMT5.lnk"
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    # absent -> no raise
    autostart.disable_autostart()
    assert not lnk.exists()
    # present -> removed
    lnk.touch()
    autostart.disable_autostart()
    assert not lnk.exists()
    # absent again -> no raise
    autostart.disable_autostart()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_autostart.py -v
```
Expected: FAIL — `manager.platform.autostart` does not exist (ModuleNotFoundError / ImportError).

- [ ] **Step 3: Create the package + module**

Create an empty `manager/platform/__init__.py` (zero bytes is fine; add a one-line docstring for clarity):

```python
"""Platform-specific helpers (Windows autostart, etc.)."""
```

Create `manager/platform/autostart.py`:

```python
# manager/platform/autostart.py
"""Windows Startup-folder shortcut management for autostart-on-login.

Creates/removes a .lnk in shell:startup via PowerShell + WScript.Shell COM
(the same technique scripts/install.ps1 uses for the Start Menu shortcut), so
no pywin32 dependency is needed. Qt-free and unit-testable with a mocked
subprocess.run."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class AutostartError(RuntimeError):
    """Raised when the Windows Startup shortcut cannot be created/removed."""


def startup_lnk_path() -> Path:
    """Path to the autostart shortcut. On Windows, the Startup folder under
    %APPDATA%; on other OSes a ~/.config/autostart fallback so the module is
    importable in tests/dev without path explosions (only Windows is a
    supported target per the README)."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home())
        return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "CopyTradesMT5.lnk")
    return Path.home() / ".config" / "autostart" / "CopyTradesMT5.lnk"


def is_autostart_enabled() -> bool:
    """True iff the Startup .lnk currently exists (the source of truth for the
    boot checkbox state)."""
    return startup_lnk_path().exists()


def _ps_quote(s: str) -> str:
    """Single-quote a string for a PowerShell -Command argument. A literal
    single quote is escaped by doubling it."""
    return "'" + s.replace("'", "''") + "'"


def enable_autostart(target_exe: str, arguments: str = "-m manager",
                     working_dir: str | None = None) -> None:
    """Create the Windows Startup .lnk pointing at target_exe (with arguments)
    via PowerShell + WScript.Shell COM. Raises AutostartError on non-zero exit
    or if powershell is missing, so the GUI can log and revert the toggle."""
    lnk = startup_lnk_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$sc = $ws.CreateShortcut({_ps_quote(str(lnk))})",
        f"$sc.TargetPath = {_ps_quote(target_exe)}",
        f"$sc.Arguments = {_ps_quote(arguments)}",
    ]
    if working_dir is not None:
        lines.append(f"$sc.WorkingDirectory = {_ps_quote(working_dir)}")
    lines.append("$sc.Description = 'CopyTrades MT5 Local Manager'")
    lines.append("$sc.Save()")
    script = "; ".join(lines)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise AutostartError(f"failed to create Startup shortcut: {exc}") from exc


def disable_autostart() -> None:
    """Delete the Startup .lnk if present (idempotent; missing file is a
    no-op)."""
    lnk = startup_lnk_path()
    try:
        lnk.unlink()
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_autostart.py -v
```
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q
```
Expected: all green (the pre-existing `_slave_loop` teardown warning may appear).

- [ ] **Step 6: Commit**

```bash
git add manager/platform/__init__.py manager/platform/autostart.py manager/tests/test_autostart.py
git commit -m "feat(platform): Windows Startup shortcut helper for autostart-on-login" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Auto-start checkboxes, config persistence, and shared `_do_start`

**Files:**
- Modify: `manager/gui/main_window.py` (imports `:1-15`; `__init__` state + launch hook `:57-78`; `_build_ui` autostart group + Cancel button `:108-160`; wiring `:162-174`; `_config_dict` `:185-189`; `_load_config` `:199-223`; `_on_start` `:321-335`)
- Modify: `manager/tests/test_main_window.py` (append tests)
- Modify: `manager/tests/test_settings_store.py` (append one test)

**Interfaces:**
- Consumes: `manager.platform.autostart` (`is_autostart_enabled`, `enable_autostart`, `disable_autostart`) from Task 1; `sys.executable`.
- Produces: `MainWindow` with `autostart_boot_checkbox`, `autostart_copy_checkbox`, `autostart_cancel_button` attributes; `_do_start(show_modal: bool, label: str = "start") -> None`; `_start_silent() -> None`; `_on_autostart_boot_toggled(checked: bool) -> None`; `_on_autostart_copy_toggled(checked: bool) -> None`; config blob gains an `autostart` sub-dict. Task 3 builds the countdown on top of these.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_main_window.py`:

```python


def test_autostart_checkboxes_default_off(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert not w.autostart_boot_checkbox.isChecked()
    assert not w.autostart_copy_checkbox.isChecked()
    assert not w.autostart_cancel_button.isVisible()


def test_autostart_config_round_trip(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": True, "auto_copy": True}})
    lnk = tmp_path / "CopyTradesMT5.lnk"
    lnk.touch()
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    c = FakeController()
    w = MainWindow(c, store=store)
    # auto_copy restored from stored value; on_boot synced to .lnk existence
    assert w.autostart_copy_checkbox.isChecked() is True
    assert w.autostart_boot_checkbox.isChecked() is True


def test_boot_checkbox_syncs_to_lnk_existence_on_load(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    lnk = tmp_path / "CopyTradesMT5.lnk"
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    store = SettingsStore(path=tmp_path / "settings.json")
    # stored on_boot True but no .lnk on disk -> checkbox unchecked (reality wins)
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [], "autostart": {"on_boot": True, "auto_copy": False}})
    w = MainWindow(FakeController(), store=store)
    assert w.autostart_boot_checkbox.isChecked() is False
    # now create the .lnk and reload via a fresh window -> checked
    lnk.touch()
    w2 = MainWindow(FakeController(), store=store)
    assert w2.autostart_boot_checkbox.isChecked() is True


def test_start_silent_logs_on_algo_trading_disabled_no_modal(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui import main_window as mw
    from manager.app.controller import AlgoTradingDisabledError, AccountSpec
    c = FakeController()
    c.start = lambda master, slaves, **kw: (_ for _ in ()).throw(
        AlgoTradingDisabledError(["s1 (C:/t/terminal64.exe)"]))
    shown = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda parent, title, text: shown.append((title, text)) or 0)
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe")]
    w._start_silent()
    assert not shown, "auto-start path must not show a modal"
    assert "auto-start failed" in w.log_view.toPlainText().lower()


def test_do_start_manual_shows_modal_on_algo_trading_disabled(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.gui import main_window as mw
    from manager.app.controller import AlgoTradingDisabledError
    c = FakeController()
    c.start = lambda master, slaves, **kw: (_ for _ in ()).throw(
        AlgoTradingDisabledError(["s1 (C:/t/terminal64.exe)"]))
    shown = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda parent, title, text: shown.append((title, text)) or 0)
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w.start_button.click()  # manual path -> _on_start -> _do_start(show_modal=True)
    assert shown, "manual Start must still show the Algo Trading modal"
    assert "Algo Trading" in shown[0][0]
```

Append to `manager/tests/test_settings_store.py`:

```python


def test_save_then_load_config_round_trip_with_autostart(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    cfg = {"master": {"terminal_path": "C:/t/terminal64.exe"},
           "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
           "autostart": {"on_boot": True, "auto_copy": False}}
    s.save_config(cfg)
    assert s.load_config() == cfg
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py::test_autostart_checkboxes_default_off manager/tests/test_settings_store.py::test_save_then_load_config_round_trip_with_autostart -v
```
Expected: FAIL — `MainWindow` has no `autostart_boot_checkbox` attribute (AttributeError), and `_start_silent` does not exist.

- [ ] **Step 3: Add imports (`sys`, `QCheckBox`, `autostart`) to `main_window.py`**

In `manager/gui/main_window.py`, replace the import block (lines 1-15):
```python
from __future__ import annotations

import dataclasses
import subprocess
import webbrowser

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
    QProgressBar, QMessageBox,
)

from manager.app.controller import AccountSpec, StatusUpdate, AlgoTradingDisabledError
```
with:
```python
from __future__ import annotations

import dataclasses
import subprocess
import sys
import webbrowser

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QCheckBox, QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel,
    QGroupBox, QProgressBar, QMessageBox,
)

from manager.app.controller import AccountSpec, StatusUpdate, AlgoTradingDisabledError
from manager.platform import autostart
```

- [ ] **Step 4: Add the Auto-start group box + Cancel button in `_build_ui`**

In `manager/gui/main_window.py`, replace the slave box + Start/Stop block (lines 108-130):
```python
        # Slave list
        slave_box = QGroupBox("Slaves")
        sl = QVBoxLayout()
        self.slave_list = QListWidget()
        self.add_slave_button = QPushButton("Add Slave…")
        self.remove_slave_button = QPushButton("Remove Slave")
        self.edit_slave_button = QPushButton("Edit Slave…")
        self.edit_slave_button.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        row.addWidget(self.edit_slave_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        slave_box.setLayout(sl)

        # Start/Stop
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        controls = QHBoxLayout()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
```
with:
```python
        # Slave list
        slave_box = QGroupBox("Slaves")
        sl = QVBoxLayout()
        self.slave_list = QListWidget()
        self.add_slave_button = QPushButton("Add Slave…")
        self.remove_slave_button = QPushButton("Remove Slave")
        self.edit_slave_button = QPushButton("Edit Slave…")
        self.edit_slave_button.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        row.addWidget(self.edit_slave_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        slave_box.setLayout(sl)

        # Auto-start (boot + auto-copy)
        self.autostart_box = QGroupBox("Auto-start")
        as_layout = QVBoxLayout()
        self.autostart_boot_checkbox = QCheckBox("Launch on Windows startup")
        self.autostart_copy_checkbox = QCheckBox(
            "Auto-start copying on launch (15 s countdown)")
        as_layout.addWidget(self.autostart_boot_checkbox)
        as_layout.addWidget(self.autostart_copy_checkbox)
        self.autostart_box.setLayout(as_layout)

        # Start/Stop + countdown Cancel
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.autostart_cancel_button = QPushButton("Cancel")
        self.autostart_cancel_button.setVisible(False)
        controls = QHBoxLayout()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.autostart_cancel_button)
```

Then, in the same `_build_ui`, replace the root.addWidget block (lines 152-160):
```python
        root.addWidget(master_box)
        root.addWidget(slave_box)
        root.addLayout(controls)
        root.addLayout(updates_row)
        root.addWidget(self.update_progress)
        root.addWidget(QLabel("Status"))
        root.addWidget(self.status_view)
        root.addWidget(QLabel("Log"))
        root.addWidget(self.log_view)
```
with:
```python
        root.addWidget(master_box)
        root.addWidget(slave_box)
        root.addWidget(self.autostart_box)
        root.addLayout(controls)
        root.addLayout(updates_row)
        root.addWidget(self.update_progress)
        root.addWidget(QLabel("Status"))
        root.addWidget(self.status_view)
        root.addWidget(QLabel("Log"))
        root.addWidget(self.log_view)
```

- [ ] **Step 5: Wire the new controls**

In `manager/gui/main_window.py`, replace the wire-buttons block (lines 162-174):
```python
        # wire buttons
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.add_slave_button.clicked.connect(self._on_add_slave)
        self.remove_slave_button.clicked.connect(self._on_remove_slave)
        self.edit_slave_button.clicked.connect(self._on_edit_slave)
        self.slave_list.itemDoubleClicked.connect(
            lambda _item: self._on_edit_slave())
        self.slave_list.itemSelectionChanged.connect(self._update_edit_enabled)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.install_metatrader_button.clicked.connect(self._on_install_metatrader)
        self.check_update_button.clicked.connect(self.check_for_updates_now)
        self.update_restart_button.clicked.connect(self._on_update_restart)
```
with:
```python
        # wire buttons
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.add_slave_button.clicked.connect(self._on_add_slave)
        self.remove_slave_button.clicked.connect(self._on_remove_slave)
        self.edit_slave_button.clicked.connect(self._on_edit_slave)
        self.slave_list.itemDoubleClicked.connect(
            lambda _item: self._on_edit_slave())
        self.slave_list.itemSelectionChanged.connect(self._update_edit_enabled)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.install_metatrader_button.clicked.connect(self._on_install_metatrader)
        self.check_update_button.clicked.connect(self.check_for_updates_now)
        self.update_restart_button.clicked.connect(self._on_update_restart)
        self.autostart_boot_checkbox.toggled.connect(
            self._on_autostart_boot_toggled)
        self.autostart_copy_checkbox.toggled.connect(
            self._on_autostart_copy_toggled)
        self.autostart_cancel_button.clicked.connect(self._cancel_autostart_copy)
```

- [ ] **Step 6: Add countdown state + launch hook in `__init__`**

In `manager/gui/main_window.py`, replace the `__init__` body (lines 57-78):
```python
    def __init__(self, controller, store=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — Local Manager")
        self._controller = controller
        self._store = store
        self._slaves: list[AccountSpec] = []
        self._build_ui()
        self._populate_terminals()
        self._load_config()
        app = QApplication.instance()
        if app is not None and self._store is not None:
            app.aboutToQuit.connect(self._save_config)

        self._update_worker = None
        self._predownload_worker = None
        self._cached_wheel = None
        self._latest_version = None
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(3600 * 1000)
        self._update_timer.timeout.connect(self.check_for_updates_now)
        self._update_timer.start()
        QTimer.singleShot(10_000, self.check_for_updates_now)
```
with:
```python
    def __init__(self, controller, store=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — Local Manager")
        self._controller = controller
        self._store = store
        self._slaves: list[AccountSpec] = []
        self._countdown_timer: QTimer | None = None
        self._countdown_remaining = 0
        self._build_ui()
        self._populate_terminals()
        self._load_config()
        app = QApplication.instance()
        if app is not None and self._store is not None:
            app.aboutToQuit.connect(self._save_config)

        self._update_worker = None
        self._predownload_worker = None
        self._cached_wheel = None
        self._latest_version = None
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(3600 * 1000)
        self._update_timer.timeout.connect(self.check_for_updates_now)
        self._update_timer.start()
        QTimer.singleShot(10_000, self.check_for_updates_now)
        self._maybe_begin_autostart_copy()
```

- [ ] **Step 7: Persist the toggles in `_config_dict`**

In `manager/gui/main_window.py`, replace `_config_dict` (lines 185-189):
```python
    def _config_dict(self) -> dict:
        return {
            "master": {"terminal_path": self.master_terminal.currentText().strip()},
            "slaves": [dataclasses.asdict(s) for s in self._slaves],
        }
```
with:
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

- [ ] **Step 8: Restore the toggles in `_load_config`**

In `manager/gui/main_window.py`, replace the end of `_load_config` (lines 199-223):
```python
    def _load_config(self) -> None:
        if self._store is None:
            return
        try:
            cfg = self._store.load_config()
        except Exception as exc:
            self.append_log(f"config load failed: {exc}")
            return
        master = cfg.get("master") if isinstance(cfg, dict) else None
        if isinstance(master, dict):
            mpath = str(master.get("terminal_path", "")).strip()
            if mpath:
                self.master_terminal.setEditText(mpath)
        for s in (cfg.get("slaves") if isinstance(cfg, dict) else None) or []:
            if not isinstance(s, dict):
                continue
            fields = AccountSpec.__dataclass_fields__
            kwargs = {k: s[k] for k in fields if k in s}
            try:
                spec = AccountSpec(**kwargs)
            except TypeError:
                continue
            self._slaves.append(spec)
            label = (spec.terminal_path or spec.id).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            self.slave_list.addItem(f"{spec.id}: {label}")
```
with:
```python
    def _load_config(self) -> None:
        if self._store is None:
            return
        try:
            cfg = self._store.load_config()
        except Exception as exc:
            self.append_log(f"config load failed: {exc}")
            return
        master = cfg.get("master") if isinstance(cfg, dict) else None
        if isinstance(master, dict):
            mpath = str(master.get("terminal_path", "")).strip()
            if mpath:
                self.master_terminal.setEditText(mpath)
        for s in (cfg.get("slaves") if isinstance(cfg, dict) else None) or []:
            if not isinstance(s, dict):
                continue
            fields = AccountSpec.__dataclass_fields__
            kwargs = {k: s[k] for k in fields if k in s}
            try:
                spec = AccountSpec(**kwargs)
            except TypeError:
                continue
            self._slaves.append(spec)
            label = (spec.terminal_path or spec.id).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            self.slave_list.addItem(f"{spec.id}: {label}")
        # autostart toggles: auto_copy from stored value, on_boot synced to the
        # .lnk's existence (reality is the source of truth). blockSignals so
        # syncing does not re-trigger the toggle handler (which would touch the
        # OS or save).
        as_cfg = (cfg.get("autostart") if isinstance(cfg, dict) else None) or {}
        self.autostart_copy_checkbox.blockSignals(True)
        self.autostart_copy_checkbox.setChecked(bool(as_cfg.get("auto_copy", False)))
        self.autostart_copy_checkbox.blockSignals(False)
        self.autostart_boot_checkbox.blockSignals(True)
        self.autostart_boot_checkbox.setChecked(autostart.is_autostart_enabled())
        self.autostart_boot_checkbox.blockSignals(False)
```

- [ ] **Step 9: Refactor `_on_start` into the shared `_do_start` + `_start_silent`**

In `manager/gui/main_window.py`, replace `_on_start` (lines 321-335):
```python
    def _on_start(self):
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
            self.append_log(f"start blocked: {exc}")
            QMessageBox.warning(self, "Algo Trading disabled", str(exc))
        except Exception as exc:
            self.append_log(f"start failed: {exc}")
```
with:
```python
    def _on_start(self):
        self._do_start(show_modal=True)

    def _do_start(self, show_modal: bool, label: str = "start") -> None:
        """Shared Start body. show_modal=True (manual click) shows the Algo
        Trading modal on failure; show_modal=False (auto-start) logs only.
        `label` prefixes the failure log line so auto-start attempts are
        distinguishable in the log ("auto-start failed: …" vs "start failed: …")."""
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
            self.append_log(f"{label} failed: {exc}")
            if show_modal:
                QMessageBox.warning(self, "Algo Trading disabled", str(exc))
        except Exception as exc:
            self.append_log(f"{label} failed: {exc}")

    def _start_silent(self) -> None:
        """Auto-start path: same Start body, never shows a modal; logs as
        'auto-start failed' so the attempt is distinguishable in the log."""
        self._do_start(show_modal=False, label="auto-start")
```

- [ ] **Step 10: Add the toggle handlers**

Append these two methods to `manager/gui/main_window.py`, immediately after the `_start_silent` method added in Step 9 (i.e. after the `_start_silent` body, before the `# ---- handlers ----` section's `_on_stop`):

```python
    def _on_autostart_boot_toggled(self, checked: bool) -> None:
        try:
            if checked:
                autostart.enable_autostart(sys.executable, "-m manager")
            else:
                autostart.disable_autostart()
        except Exception as exc:
            self.append_log(f"autostart enable failed: {exc}")
            self.autostart_boot_checkbox.blockSignals(True)
            self.autostart_boot_checkbox.setChecked(False)
            self.autostart_boot_checkbox.blockSignals(False)
        self._save_config()

    def _on_autostart_copy_toggled(self, _checked: bool) -> None:
        # No OS side effect; the countdown only fires on launch, not mid-session.
        self._save_config()
```

- [ ] **Step 11: Add a stub `_maybe_begin_autostart_copy` + `_cancel_autostart_copy` (countdown lands in Task 3)**

To keep Task 2 green independently of the countdown, add minimal stubs now (Task 3 fills `_maybe_begin_autostart_copy` and adds `_autostart_tick`). Append after the toggle handlers from Step 10:

```python
    def _maybe_begin_autostart_copy(self) -> None:
        # Countdown implemented in Task 3; no-op here so launch is safe.
        return

    def _cancel_autostart_copy(self) -> None:
        # Wired in Task 3.
        return
```

- [ ] **Step 12: Run the full suite to verify green**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q
```
Expected: all green. The new tests pass (checkboxes exist, config round-trip, boot sync, silent-no-modal, manual-modal, store round-trip). The pre-existing `test_start_blocked_by_algo_trading_shows_message_box` still passes because the `AlgoTradingDisabledError` message text contains "Algo Trading" (the log prefix changed from "start blocked:" to "start failed:", but the assertion checks for `"Algo Trading"` in the log, which comes from the exception text).

- [ ] **Step 13: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py manager/tests/test_settings_store.py
git commit -m "feat(gui): autostart checkboxes, config persistence, shared _do_start" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 15 s auto-copy countdown on launch

**Files:**
- Modify: `manager/gui/main_window.py` (replace the `_maybe_begin_autostart_copy` + `_cancel_autostart_copy` stubs from Task 2 with the real countdown; add `_autostart_tick`)
- Modify: `manager/tests/test_main_window.py` (append countdown tests)

**Interfaces:**
- Consumes: `_start_silent()` from Task 2; `self.autostart_cancel_button`, `self.autostart_copy_checkbox`, `self._countdown_timer`, `self._countdown_remaining` from Task 2.
- Produces: the countdown behavior — on launch, if the auto-copy toggle is on and a master + slaves are configured, a 15 s `QTimer` countdown with a visible Cancel button; at 0, `_start_silent()` fires; Cancel aborts.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_main_window.py`:

```python


def test_autostart_copy_countdown_skipped_when_toggle_off(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": False}})
    c = FakeController()
    w = MainWindow(c, store=store)
    assert w._countdown_timer is None
    assert c.started is False


def test_autostart_copy_countdown_skipped_when_config_incomplete(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    # auto_copy on, master set, but NO slaves -> skip
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [], "autostart": {"on_boot": False, "auto_copy": True}})
    c = FakeController()
    w = MainWindow(c, store=store)
    assert w._countdown_timer is None
    assert c.started is False
    assert "auto-start skipped" in w.log_view.toPlainText().lower()


def test_autostart_copy_countdown_fires_start_at_zero(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": True}})
    c = FakeController()
    w = MainWindow(c, store=store)
    # countdown began on construction
    assert w._countdown_timer is not None
    # drive it deterministically: stop the real timer, set 1 s left, tick once
    w._countdown_timer.stop()
    w._countdown_remaining = 1
    w._autostart_tick()
    assert c.started is True
    assert w._countdown_timer is None
    assert not w.autostart_cancel_button.isVisible()


def test_autostart_cancel_button_aborts(qapp, tmp_path, monkeypatch):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    from manager.platform import autostart
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "nope.lnk")
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"},
                       "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe"}],
                       "autostart": {"on_boot": False, "auto_copy": True}})
    c = FakeController()
    w = MainWindow(c, store=store)
    assert w._countdown_timer is not None
    w._cancel_autostart_copy()
    assert w._countdown_timer is None
    assert c.started is False
    assert "auto-start cancelled" in w.log_view.toPlainText().lower()
    # buttons re-enabled to the not-running state
    assert w.start_button.isEnabled()
    assert not w.stop_button.isEnabled()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py::test_autostart_copy_countdown_fires_start_at_zero -v
```
Expected: FAIL — `_maybe_begin_autostart_copy` is the Task 2 no-op stub, so `_countdown_timer` is `None` and the countdown never starts.

- [ ] **Step 3: Implement the countdown**

In `manager/gui/main_window.py`, replace the two Task 2 stubs:
```python
    def _maybe_begin_autostart_copy(self) -> None:
        # Countdown implemented in Task 3; no-op here so launch is safe.
        return

    def _cancel_autostart_copy(self) -> None:
        # Wired in Task 3.
        return
```
with:
```python
    def _maybe_begin_autostart_copy(self) -> None:
        """On launch, if the auto-copy toggle is on and a master + slaves are
        configured, start a 15 s countdown to auto-Start. Cancel via the
        dedicated Cancel button. No-op otherwise (toggle off or config
        incomplete)."""
        if not self.autostart_copy_checkbox.isChecked():
            return
        terminal_path = self.master_terminal.currentText().strip()
        if not terminal_path or not self._slaves:
            self.append_log("auto-start skipped: no master/slaves configured")
            return
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self._countdown_remaining = 15
        self.autostart_cancel_button.setText("Cancel (15 s)")
        self.autostart_cancel_button.setVisible(True)
        self.append_status(StatusUpdate(kind="info",
            message="Auto-start in 15 s — click Cancel to abort"))
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._autostart_tick)
        self._countdown_timer.start()

    def _autostart_tick(self) -> None:
        self._countdown_remaining -= 1
        if self._countdown_remaining > 0:
            self.autostart_cancel_button.setText(
                f"Cancel ({self._countdown_remaining} s)")
            return
        # countdown reached zero -> fire Start (silent: no modal on failure)
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
        self.autostart_cancel_button.setVisible(False)
        self._start_silent()
        self.set_running(self._controller.is_running())

    def _cancel_autostart_copy(self) -> None:
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
        self.autostart_cancel_button.setVisible(False)
        self.set_running(False)
        self.append_log("auto-start cancelled")
```

- [ ] **Step 4: Run the countdown tests to verify they pass**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py -k autostart -v
```
Expected: PASS (the 4 new countdown tests + the Task 2 autostart tests).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run:
```powershell
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "feat(gui): 15s cancellable auto-copy countdown on launch" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: README — document the auto-start feature

**Files:**
- Modify: `README.md` (Features bullet after the "Single-window close = stop" bullet; Usage step 6 area)

**Interfaces:** None (documentation only).

- [ ] **Step 1: Add the Features bullet**

In `README.md`, replace the "Single-window close = stop" bullet (the three-line block ending "no system tray, no background-running mode."):
```markdown
- **Single-window close = stop** — closing the window stops the engine (joins
  workers) and exits the app; no system tray, no background-running mode.
```
with:
```markdown
- **Single-window close = stop** — closing the window stops the engine (joins
  workers) and exits the app; no system tray, no background-running mode.
- **Auto-start on boot + auto-copy** — optionally launch the app at Windows
  login and auto-start copying 15 s after launch (one click to cancel). If the
  terminals aren't ready, it fails silently (logged, no modal).
```

- [ ] **Step 2: Add a Usage note**

In `README.md`, replace Usage step 6 (the three-line "Close" block ending "Minimize the window to keep it running in the background."):
```markdown
6. **Close**: closing the window stops the engine (joins workers) and exits
   the app — there is no tray. Minimize the window to keep it running in the
   background.
```
with:
```markdown
6. **Close**: closing the window stops the engine (joins workers) and exits
   the app — there is no tray. Minimize the window to keep it running in the
   background.
7. **Auto-start (optional)**: in the **Auto-start** group, tick **Launch on
   Windows startup** to start the app at login (a shortcut in `shell:startup`),
   and/or **Auto-start copying on launch** to begin copying 15 s after the app
   opens — a **Cancel** button appears during the countdown. Auto-start fails
   silently (a log line) if no master/slaves are configured or Algo Trading is
   off; it never shows a modal, so an unattended reboot is not blocked.
8. **Updates**: the app checks for updates hourly and pre-downloads the
   verified wheel when one is found, so clicking **Update & restart** finishes
   in seconds (no network in the restart path) and reliably relaunches the
   manager.
```
Then delete the now-duplicated old "Updates" step 7 (the four-line block starting "7. **Updates**: the app checks for updates hourly"). Replace:
```markdown
7. **Updates**: the app checks for updates hourly and pre-downloads the
   verified wheel when one is found, so clicking **Update & restart** finishes
   in seconds (no network in the restart path) and reliably relaunches the
   manager.
```
with nothing (remove the block — the new step 8 above replaces it).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: autostart-on-boot + 15s auto-copy countdown" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```