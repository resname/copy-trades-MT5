# Updater restart-fix + pre-download + config persistence — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the "Update & restart" relaunch bug, add auto pre-download of the verified wheel, persist the master+slaves config across restarts, and swap/relabel the Launch/Install buttons.

**Architecture:** Updates move out of `install.ps1` into an in-package Python path: `updater.download_update` pre-downloads + SHA256-verifies the wheel to a local cache; `apply_update_and_restart` spawns a fully-detached `manager.update_helper` that waits for the manager to exit, `pip install --force-reinstall`s the cached wheel, and `Popen`-relaunches the manager (logged to `update.log`). Config (master terminal + slaves) is saved to `settings.json` via new `SettingsStore.load_config`/`save_config` accessors and auto-restored on startup; saving also fires on `QApplication.aboutToQuit` so tray-Quit and update-quit both persist.

**Tech Stack:** Python 3.11+, PySide6 (Qt), psutil, pytest; Windows-only process-creation flags guarded by `sys.platform == "win32"`.

## Global Constraints

- Demo accounts only — never a real account (unchanged from PR #14).
- No credentials are stored, piped, or logged by the manager; the update log contains only step markers + exit codes, no credentials/trade data.
- The update wheel is SHA256-verified before install; a checksum mismatch aborts and leaves the existing install untouched.
- Tests: headless suite green AND the PySide6 venv GUI suite green (no skips) before merge — `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q` is the GUI gate (memory `gui-tests-need-pyside6-venv`). Headless `python -m pytest -q` runs the non-GUI tests.
- Windows-only; guard non-Windows paths with `sys.platform == "win32"`.
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

**Baseline (verified on this branch):** PySide6 venv = 193 passed, 0 skipped; headless = 167 passed, 5 skipped. GUI-skip modules: `test_main_window`, `test_main_window_updates`, `test_slave_editor`, `test_tray`, `test_main_entry`.

---

## File Structure

- `manager/updater.py` — add `UPDATE_DIR`, `UpdateDownloadError`, `download_update`, `cached_update`, helper-spawn constants; rewrite `apply_update_and_restart` to spawn the detached helper.
- `manager/update_helper.py` — **new**, import-light: wait for parent exit → pip reinstall cached wheel → `Popen`-relaunch the manager → log to `update.log`.
- `manager/gui/main_window.py` — swap Install/Launch order + relabel Launch; add config save/load/restore + `aboutToQuit` hook; auto pre-download on update-detected + pass cached wheel to restart.
- `manager/gui/slave_editor.py` — relabel the Launch button to "Open terminal for login" (+ docstring).
- `manager/settings/store.py` — add `load_config`/`save_config` accessors.
- `manager/__main__.py` — pass `store` into `MainWindow`.
- `manager/tests/test_updater.py`, `manager/tests/test_update_helper.py` (new), `manager/tests/test_main_window.py`, `manager/tests/test_slave_editor.py`, `manager/tests/test_settings_store.py` — tests.
- `README.md`, `docs/TESTING.md` — button label/order, config persistence, pre-download, updated test counts.

---

### Task 1: Swap + relabel the Launch/Install buttons

**Files:**
- Modify: `manager/gui/main_window.py:66-71` (term_row) + store the row for an order assertion.
- Modify: `manager/gui/slave_editor.py:17` (docstring) and `:38` (button label).
- Test: `manager/tests/test_main_window.py`, `manager/tests/test_slave_editor.py`.

**Interfaces:** none (pure UI).

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_main_window.py` (after `test_install_metatrader_button_opens_download_page`):

```python
def test_launch_button_labeled_for_login(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.launch_terminal_button.text() == "Open terminal for login"
    assert w.install_metatrader_button.text() == "Install MetaTrader"


def test_install_button_is_left_of_launch_button(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    row = w.term_row
    assert row.indexOf(w.install_metatrader_button) < row.indexOf(w.launch_terminal_button)
```

Add to `manager/tests/test_slave_editor.py`:

```python
def test_slave_editor_launch_button_labeled_for_login(qapp):
    from manager.gui.slave_editor import SlaveEditor
    class _C:
        def discover_instances(self): return []
    dlg = SlaveEditor(_C())
    assert dlg.launch_terminal_button.text() == "Open terminal for login"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py::test_launch_button_labeled_for_login manager/tests/test_main_window.py::test_install_button_is_left_of_launch_button manager/tests/test_slave_editor.py::test_slave_editor_launch_button_labeled_for_login -q`
Expected: FAIL (`term_row` attribute missing; labels still "Launch terminal").

- [ ] **Step 3: Implement**

In `manager/gui/main_window.py`, replace the term_row block (currently):

```python
        term_row = QHBoxLayout()
        self.launch_terminal_button = QPushButton("Launch terminal")
        self.install_metatrader_button = QPushButton("Install MetaTrader")
        term_row.addWidget(self.launch_terminal_button)
        term_row.addWidget(self.install_metatrader_button)
        mform.addRow("", term_row)
```

with:

```python
        term_row = QHBoxLayout()
        self.install_metatrader_button = QPushButton("Install MetaTrader")
        self.launch_terminal_button = QPushButton("Open terminal for login")
        term_row.addWidget(self.install_metatrader_button)
        term_row.addWidget(self.launch_terminal_button)
        self.term_row = term_row
        mform.addRow("", term_row)
```

(The two `.clicked.connect` lines later in `_build_ui` still reference the same attributes — leave them.)

In `manager/gui/slave_editor.py` line 17 docstring, change "a Launch-terminal button" to "an Open-terminal-for-login button". Line 38, change:

```python
        self.launch_terminal_button = QPushButton("Launch terminal")
```

to:

```python
        self.launch_terminal_button = QPushButton("Open terminal for login")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py manager/tests/test_slave_editor.py -q`
Expected: PASS (and the existing GUI suite still green — run the full venv suite).

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/gui/slave_editor.py manager/tests/test_main_window.py manager/tests/test_slave_editor.py
git commit -m "gui: Install left of Launch; relabel Launch to 'Open terminal for login'

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: SettingsStore config accessors

**Files:**
- Modify: `manager/settings/store.py` (add `load_config`, `save_config`).
- Test: `manager/tests/test_settings_store.py`.

**Interfaces:**
- Produces: `SettingsStore.load_config() -> dict` (returns `data.get("config", {})`); `SettingsStore.save_config(config: dict) -> None` (load-merge-`config`-save, preserving other top-level keys).

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_settings_store.py`:

```python
def test_load_config_empty_when_absent(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    assert s.load_config() == {}


def test_save_then_load_config_round_trip(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    cfg = {"master": {"terminal_path": "C:/t/terminal64.exe"},
           "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                       "symbol_map_csv": "", "step_amount": 100.0,
                       "step_size": 0.01, "max_lot": 10.0,
                       "max_trade_age_minutes": 10.0, "normalize_sltp": True}]}
    s.save_config(cfg)
    assert s.load_config() == cfg


def test_save_config_preserves_other_keys(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    s.save({"accounts": {"master": {"id": "master"}}, "provisioned_instances": ["C:/x"],
            "global": {}})
    s.save_config({"master": {"terminal_path": "C:/t/terminal64.exe"}, "slaves": []})
    data = s.load()
    assert data["accounts"] == {"master": {"id": "master"}}
    assert data["provisioned_instances"] == ["C:/x"]
    assert data["config"]["master"]["terminal_path"] == "C:/t/terminal64.exe"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_settings_store.py -q`
Expected: FAIL (`AttributeError: 'SettingsStore' object has no attribute 'load_config'`).

- [ ] **Step 3: Implement**

Add to `manager/settings/store.py` (after `save`):

```python
    def load_config(self) -> dict:
        """Return the GUI-restorable config dict (master + slaves), or {} if
        none is stored. A missing/corrupt config is recoverable by re-entering
        config, not by crashing."""
        return self.load().get("config", {})

    def save_config(self, config: dict) -> None:
        """Merge `config` into the store (preserving accounts/
        provisioned_instances/global) and atomically save."""
        data = self.load()
        data["config"] = config
        self.save(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_settings_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/settings/store.py manager/tests/test_settings_store.py
git commit -m "feat(store): load_config/save_config accessors for GUI config

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: MainWindow config save/load + restore on startup

**Files:**
- Modify: `manager/gui/main_window.py` (`__init__` signature, `_build_ui` unchanged, add `_config_dict`/`_save_config`/`_load_config`, call `_load_config` in `__init__`, hook `aboutToQuit`, call `_save_config` in `_on_start`/`_on_add_slave`/`_on_remove_slave`).
- Modify: `manager/__main__.py` (pass `store` into `MainWindow`).
- Test: `manager/tests/test_main_window.py`.

**Interfaces:**
- Consumes: `SettingsStore.load_config() -> dict`, `SettingsStore.save_config(dict) -> None` (Task 2).
- Produces: `MainWindow(controller, store=None, parent=None)`; persistence is a no-op when `store is None` (so existing FakeController tests are unaffected).

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_main_window.py` (the imports `AccountSpec` and `SettingsStore` as needed):

```python
def test_config_round_trip_restores_master_and_slaves(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import AccountSpec
    from manager.settings.store import SettingsStore
    store = SettingsStore(path=tmp_path / "settings.json")
    c = FakeController()
    w = MainWindow(c, store=store)
    w.master_terminal.setEditText("C:/m/terminal64.exe")
    w._slaves = [AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe")]
    w._save_config()

    c2 = FakeController()
    w2 = MainWindow(c2, store=store)
    assert w2.master_terminal.currentText() == "C:/m/terminal64.exe"
    assert len(w2._slaves) == 1
    assert w2._slaves[0].id == "s1"
    assert w2._slaves[0].terminal_path == "C:/s1/terminal64.exe"
    assert w2.slave_list.count() == 1


def test_load_config_skips_when_store_none(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())  # store=None
    assert w._slaves == []
    # construction did not raise
    assert w.windowTitle()


def test_save_config_noop_when_store_none(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w._save_config()  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py::test_config_round_trip_restores_master_and_slaves -q`
Expected: FAIL (no `store` kwarg / no `_save_config`).

- [ ] **Step 3: Implement**

In `manager/gui/main_window.py`:

Add at the top (after existing imports):

```python
import dataclasses
```

Change `__init__`:

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
```

(Add `QApplication` to the `PySide6.QtWidgets` import line if not already present — it is NOT currently imported in main_window.py; add it.)

Add the three methods (after `_populate_terminals`, before the "public API" section):

```python
    # ---- config persistence ----
    def _config_dict(self) -> dict:
        return {
            "master": {"terminal_path": self.master_terminal.currentText().strip()},
            "slaves": [dataclasses.asdict(s) for s in self._slaves],
        }

    def _save_config(self) -> None:
        if self._store is None:
            return
        try:
            self._store.save_config(self._config_dict())
        except Exception as exc:
            self.append_log(f"config save failed: {exc}")

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

Call `_save_config()` in `_on_start` (after `self.set_running(True)`):

```python
        try:
            self._controller.start(master, list(self._slaves))
            self.set_running(True)
            self._save_config()
        except Exception as exc:
            self.append_log(f"start failed: {exc}")
```

In `_on_add_slave` (after `self.slave_list.addItem(...)`): add `self._save_config()`. In `_on_remove_slave` (after `del self._slaves[row]`): add `self._save_config()`.

In `manager/__main__.py` `build_app_graph`, change:

```python
    window = MainWindow(controller)
```

to:

```python
    window = MainWindow(controller, store=store)
```

- [ ] **Step 4: Run tests to verify they pass**

Run the full GUI suite: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py -q`
Expected: PASS (new tests + all existing main_window tests green).
Then the headless suite (no GUI): `python -m pytest -q` — expect 167 passed, 5 skipped unchanged (Task 3 touches only GUI files).

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/__main__.py manager/tests/test_main_window.py
git commit -m "feat(gui): persist master+slaves config; auto-restore on startup

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Updater pre-download + cache (download_update, cached_update)

**Files:**
- Modify: `manager/updater.py` (add `import os`, `from pathlib import Path`, `UPDATE_DIR`, `UpdateDownloadError`, `_sha256_file`, `download_update`, `cached_update`, `_DETACHED_FLAGS`).
- Test: `manager/tests/test_updater.py`.

**Interfaces:**
- Produces: `UPDATE_DIR: Path`; `UpdateDownloadError(Exception)`; `download_update(dest_dir: Path | None = None) -> Path` (downloads `WHEEL_URL`+`WHEEL_SHA_URL` into `dest_dir`/`UPDATE_DIR`, verifies SHA256, raises `UpdateDownloadError` on network failure or mismatch, returns the verified wheel path); `cached_update() -> Path | None` (returns the verified cached wheel, else `None`, deleting a stale/mismatched pair); `_DETACHED_FLAGS: int` (Windows detached-spawn flags used by Task 6).

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_updater.py`:

```python
def test_download_update_verifies_sha_and_returns_path(tmp_path, monkeypatch):
    import hashlib
    from manager import updater
    data = b"wheel-bytes"

    def fake_urlretrieve(url, filename=None, *a, **k):
        if str(url).endswith(".whl"):
            Path(filename).write_bytes(data)
        else:
            Path(filename).write_text(
                hashlib.sha256(data).hexdigest() + "  manager-latest.whl",
                encoding="utf-8")
        return filename, {}

    monkeypatch.setattr(updater.urllib.request, "urlretrieve", fake_urlretrieve)
    p = updater.download_update(dest_dir=tmp_path)
    assert p.read_bytes() == data


def test_download_update_raises_on_checksum_mismatch(tmp_path, monkeypatch):
    from manager import updater

    def fake_urlretrieve(url, filename=None, *a, **k):
        if str(url).endswith(".whl"):
            Path(filename).write_bytes(b"not-the-real-wheel")
        else:
            Path(filename).write_text("0000000000000000000000000000000000000000000000000000000000000000  manager-latest.whl",
                                      encoding="utf-8")
        return filename, {}

    monkeypatch.setattr(updater.urllib.request, "urlretrieve", fake_urlretrieve)
    with pytest.raises(updater.UpdateDownloadError):
        updater.download_update(dest_dir=tmp_path)
    # mismatched files are removed
    assert not (tmp_path / "manager-latest.whl").exists()


def test_cached_update_returns_none_when_absent(tmp_path, monkeypatch):
    from manager import updater
    monkeypatch.setattr(updater, "UPDATE_DIR", tmp_path)
    assert updater.cached_update() is None


def test_cached_update_returns_path_when_verified(tmp_path, monkeypatch):
    import hashlib
    from manager import updater
    data = b"x" * 64
    (tmp_path / "manager-latest.whl").write_bytes(data)
    (tmp_path / "manager-latest.whl.sha256").write_text(
        hashlib.sha256(data).hexdigest() + "  manager-latest.whl", encoding="utf-8")
    monkeypatch.setattr(updater, "UPDATE_DIR", tmp_path)
    assert updater.cached_update() == tmp_path / "manager-latest.whl"


def test_cached_update_deletes_stale_pair(tmp_path, monkeypatch):
    from manager import updater
    (tmp_path / "manager-latest.whl").write_bytes(b"stale")
    (tmp_path / "manager-latest.whl.sha256").write_text(
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  manager-latest.whl",
        encoding="utf-8")
    monkeypatch.setattr(updater, "UPDATE_DIR", tmp_path)
    assert updater.cached_update() is None
    assert not (tmp_path / "manager-latest.whl").exists()
    assert not (tmp_path / "manager-latest.whl.sha256").exists()
```

(Add `from pathlib import Path` to the test file imports if not present.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_updater.py -q -k "download_update or cached_update"`
Expected: FAIL (`AttributeError: module 'manager.updater' has no attribute 'download_update'`).

- [ ] **Step 3: Implement**

In `manager/updater.py`, add to the imports at the top:

```python
import hashlib
import os
from pathlib import Path
```

After the existing URL constants (after `WHEEL_SHA_URL = ...`), add:

```python
# Local cache for the pre-downloaded, SHA256-verified wheel. Lives in the
# install tree (%LOCALAPPDATA%), separate from the settings file (%APPDATA%),
# so an update reinstalling the wheel never wipes the user's config.
UPDATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) \
             / "CopyTradesMT5" / "updates"

# Windows detached-spawn flags shared by apply_update_and_restart (spawning
# the helper). CREATE_NO_WINDOW (0x08000000) + CREATE_NEW_PROCESS_GROUP
# (0x00000200) decouple the child so it survives the manager quitting.
_DETACHED_FLAGS = 0x08000000 | 0x00000200


class UpdateDownloadError(Exception):
    """Raised when the update wheel cannot be downloaded or its SHA256
    checksum does not match the published .sha256."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```

After `latest_version` (before `check_for_update`), add:

```python
def download_update(dest_dir: Path | None = None) -> Path:
    """Download WHEEL_URL + WHEEL_SHA_URL into dest_dir (default UPDATE_DIR),
    verify SHA256, and return the verified wheel path. Raises
    UpdateDownloadError on a network failure or checksum mismatch (the
    mismatched files are removed)."""
    dest = dest_dir if dest_dir is not None else UPDATE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    wheel_path = dest / "manager-latest.whl"
    sha_path = dest / "manager-latest.whl.sha256"
    try:
        urllib.request.urlretrieve(WHEEL_URL, str(wheel_path))
        urllib.request.urlretrieve(WHEEL_SHA_URL, str(sha_path))
    except Exception as exc:
        raise UpdateDownloadError(f"download failed: {exc}") from exc
    expected = sha_path.read_text(encoding="utf-8").strip().split()[0].lower()
    actual = _sha256_file(wheel_path).lower()
    if actual != expected:
        for p in (wheel_path, sha_path):
            try:
                p.unlink()
            except OSError:
                pass
        raise UpdateDownloadError(
            f"checksum mismatch (expected {expected} got {actual})")
    return wheel_path


def cached_update() -> Path | None:
    """Return the path of the cached + SHA256-verified wheel, or None if no
    usable cache exists. A stale/mismatched pair is deleted so the next check
    can re-download."""
    wheel = UPDATE_DIR / "manager-latest.whl"
    sha = UPDATE_DIR / "manager-latest.whl.sha256"
    if not wheel.exists() or not sha.exists():
        return None
    expected = sha.read_text(encoding="utf-8").strip().split()[0].lower()
    if _sha256_file(wheel).lower() != expected:
        for p in (wheel, sha):
            try:
                p.unlink()
            except OSError:
                pass
        return None
    return wheel
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_updater.py -q`
Expected: PASS (new tests + the existing updater tests still green — `apply_update_and_restart` is unchanged in this task).

- [ ] **Step 5: Commit**

```bash
git add manager/updater.py manager/tests/test_updater.py
git commit -m "feat(updater): pre-download + SHA256-verify wheel to local cache

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Detached update helper (`manager/update_helper.py`)

**Files:**
- Create: `manager/update_helper.py`.
- Test: `manager/tests/test_update_helper.py`.

**Interfaces:**
- Consumes: a verified wheel path (string) + the parent manager PID (int), passed as argv.
- Produces: `manager.update_helper.main(argv: list[str] | None = None) -> int` — wait for parent exit → `pip install --force-reinstall <wheel>` → `Popen`-relaunch `python -m manager` detached → log to `UPDATE_DIR/update.log`. Exit 0 on success, non-zero on a failed step.

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_update_helper.py`:

```python
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from manager import update_helper


def test_main_waits_for_parent_then_reinstalls_then_relaunches(monkeypatch, tmp_path):
    seq = []
    monkeypatch.setattr(update_helper, "_pid_exists", lambda pid: pid != 12345)
    monkeypatch.setattr(update_helper, "_reinstall",
                        lambda wheel: seq.append(("install", wheel)) or 0)
    monkeypatch.setattr(update_helper, "_relaunch", lambda: seq.append(("relaunch",)))
    monkeypatch.setattr(update_helper, "_log", lambda m: None)
    rc = update_helper.main(["C:/cached/manager-latest.whl", "12345"])
    assert rc == 0
    assert seq == [("install", "C:/cached/manager-latest.whl"), ("relaunch",)]


def test_main_does_not_reinstall_until_parent_gone(monkeypatch):
    alive = {"v": True}
    seq = []
    def fake_pid(pid):
        return alive["v"]
    monkeypatch.setattr(update_helper, "_pid_exists", fake_pid)
    monkeypatch.setattr(update_helper, "_wait_for_parent",
                        lambda pid, timeout_s=60.0: alive.__setitem__("v", False))
    monkeypatch.setattr(update_helper, "_reinstall", lambda wheel: seq.append(("install", wheel)) or 0)
    monkeypatch.setattr(update_helper, "_relaunch", lambda: seq.append(("relaunch",)))
    monkeypatch.setattr(update_helper, "_log", lambda m: None)
    rc = update_helper.main(["C:/w.whl", "999"])
    assert rc == 0
    assert seq == [("install", "C:/w.whl"), ("relaunch",)]


def test_main_skips_relaunch_when_pip_fails(monkeypatch):
    monkeypatch.setattr(update_helper, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(update_helper, "_reinstall", lambda wheel: 1)
    relaunched = []
    monkeypatch.setattr(update_helper, "_relaunch", lambda: relaunched.append(1))
    monkeypatch.setattr(update_helper, "_log", lambda m: None)
    rc = update_helper.main(["C:/w.whl", "1"])
    assert rc == 1
    assert relaunched == []


def test_main_missing_args(monkeypatch):
    monkeypatch.setattr(update_helper, "_log", lambda m: None)
    rc = update_helper.main(["only-one"])
    assert rc != 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_update_helper.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'manager.update_helper'`).

- [ ] **Step 3: Implement**

Create `manager/update_helper.py`:

```python
"""Detached update helper, launched by updater.apply_update_and_restart as a
fully-decoupled process: ``pythonw -m manager.update_helper <wheel> <parent_pid>``.
Waits for the parent manager to exit, reinstalls the cached wheel, relaunches
the manager, and exits. Import-light: stdlib + psutil only — no PySide6, no
manager package imports — so it keeps running while the old package is being
overwritten by pip.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dep of the manager
    psutil = None

# Windows detached-relaunch flags. CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP
# decouple the new manager from the helper (it survives the helper exiting).
# Do NOT use DETACHED_PROCESS for the relaunch: the manager is a GUI app that
# must create its Qt window.
_DETACHED_FLAGS = 0x08000000 | 0x00000200

LOG = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) \
      / "CopyTradesMT5" / "updates" / "update.log"


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass


def _pid_exists(pid: int) -> bool:
    if psutil is not None:
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_parent(parent_pid: int, timeout_s: float = 60.0) -> None:
    """Block until parent_pid is gone. If it is still alive after timeout_s,
    force-kill it (a hung manager must not block the update forever) and wait
    briefly for the venv files to release."""
    deadline = time.monotonic() + timeout_s
    while _pid_exists(parent_pid):
        if time.monotonic() >= deadline:
            _log(f"parent {parent_pid} still alive after {timeout_s}s; force-killing")
            if psutil is not None:
                try:
                    psutil.Process(parent_pid).kill()
                except Exception:
                    pass
            else:
                try:
                    os.kill(parent_pid, 9)
                except OSError:
                    pass
            for _ in range(20):
                if not _pid_exists(parent_pid):
                    break
                time.sleep(0.25)
            return
        time.sleep(0.25)


def _reinstall(wheel: str) -> int:
    return subprocess.call([sys.executable, "-m", "pip", "install",
                            "--upgrade", "--force-reinstall", wheel])


def _relaunch() -> None:
    kwargs: dict = {"close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_FLAGS
    subprocess.Popen([sys.executable, "-m", "manager"], **kwargs)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) < 2:
        _log("update_helper: missing args (wheel, parent_pid)")
        return 2
    wheel = args[0]
    parent_pid = int(args[1])
    _log(f"update_helper start: wheel={wheel} parent={parent_pid}")
    _wait_for_parent(parent_pid)
    _log("parent gone; reinstalling")
    rc = _reinstall(wheel)
    if rc != 0:
        _log(f"pip install failed rc={rc}; not relaunching")
        return rc
    _log("pip install ok; relaunching manager")
    _relaunch()
    _log("relaunch spawned; helper exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_update_helper.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/update_helper.py manager/tests/test_update_helper.py
git commit -m "feat(updater): detached update_helper (wait→reinstall→relaunch)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Rewrite `apply_update_and_restart` to spawn the helper + use the cached wheel

**Files:**
- Modify: `manager/updater.py` (`apply_update_and_restart` rewrite; add `_helper_exe`; remove `_BG_FLAGS` and `INSTALL_PS1_URL` — now unused).
- Modify: `manager/tests/test_updater.py` (replace the two old powershell-based apply tests; delete `test_background_spawn_actually_runs_command_body`).

**Interfaces:**
- Consumes: `download_update()`, `cached_update()`, `UpdateDownloadError`, `_DETACHED_FLAGS` (Task 4); `manager.update_helper.main` (Task 5).
- Produces: `apply_update_and_restart(on_quit, cached_wheel: Path | None = None) -> None`.

- [ ] **Step 1: Write the failing tests**

In `manager/tests/test_updater.py`, **delete** `test_apply_update_and_restart_spawns_and_quits` and `test_background_spawn_actually_runs_command_body` (they tested the old powershell mechanism). Add:

```python
def test_apply_update_and_restart_spawns_helper_with_wheel_and_pid(monkeypatch):
    from manager import updater
    captured = []
    monkeypatch.setattr(updater, "cached_update", lambda: None)
    monkeypatch.setattr(updater, "download_update",
                        lambda dest_dir=None: Path("C:/cached/manager-latest.whl"))
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **k: captured.append((cmd, k)) or MagicMock())
    monkeypatch.setattr(updater, "_helper_exe", lambda: "C:/venv/pythonw.exe")
    quit_called = []
    updater.apply_update_and_restart(on_quit=lambda: quit_called.append(True))
    assert len(captured) == 1
    cmd, kwargs = captured[0]
    assert cmd[0] == "C:/venv/pythonw.exe"
    assert "-m" in cmd and "manager.update_helper" in cmd
    assert str(Path("C:/cached/manager-latest.whl")) in cmd
    assert str(updater.os.getpid()) in cmd
    assert quit_called == [True]


def test_apply_update_and_restart_uses_cached_wheel(monkeypatch):
    from manager import updater
    captured = []
    monkeypatch.setattr(updater, "cached_update",
                        lambda: Path("C:/pre/manager-latest.whl"))
    download_calls = []
    monkeypatch.setattr(updater, "download_update",
                        lambda dest_dir=None: download_calls.append(1) or Path("x"))
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **k: captured.append(cmd) or MagicMock())
    monkeypatch.setattr(updater, "_helper_exe", lambda: "pyw")
    updater.apply_update_and_restart(on_quit=lambda: None)
    assert download_calls == []  # cache hit -> no download
    assert str(Path("C:/pre/manager-latest.whl")) in captured[0]


def test_apply_update_and_restart_aborts_when_no_wheel_and_download_fails(monkeypatch):
    from manager import updater
    monkeypatch.setattr(updater, "cached_update", lambda: None)
    def boom(dest_dir=None):
        raise updater.UpdateDownloadError("network down")
    monkeypatch.setattr(updater, "download_update", boom)
    spawned = []
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **k: spawned.append(cmd) or MagicMock())
    quit_called = []
    updater.apply_update_and_restart(on_quit=lambda: quit_called.append(True))
    assert spawned == []          # did not spawn the helper
    assert quit_called == []       # did NOT quit the app
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_updater.py -q -k "apply_update_and_restart"`
Expected: FAIL (old `apply_update_and_restart` doesn't spawn the helper / doesn't accept `cached_wheel`).

- [ ] **Step 3: Implement**

In `manager/updater.py`, add `import os` is already present (Task 4 added it). Add `_helper_exe` and replace `apply_update_and_restart`:

```python
def _helper_exe() -> str:
    """The interpreter to run the detached helper windowless: prefer the venv's
    pythonw.exe (sibling of sys.executable), fall back to sys.executable."""
    exe = sys.executable
    sibling = Path(exe).parent / "pythonw.exe"
    return str(sibling) if sibling.exists() else exe


def apply_update_and_restart(on_quit, cached_wheel: Path | None = None) -> None:
    """Ensure a verified wheel is ready (cached_wheel, else cached_update(),
    else download now + verify). On failure, return WITHOUT calling on_quit
    so the app stays running. On success: spawn the detached update_helper with
    (wheel, parent_pid), then call on_quit() so the caller stops the engine and
    exits. The helper waits for this process to exit, reinstalls the wheel, and
    relaunches the manager."""
    wheel = cached_wheel
    if wheel is None:
        wheel = cached_update()
    if wheel is None:
        try:
            wheel = download_update()
        except UpdateDownloadError:
            return
    parent_pid = os.getpid()
    kwargs: dict = {"close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_FLAGS
    subprocess.Popen(
        [_helper_exe(), "-m", "manager.update_helper", str(wheel), str(parent_pid)],
        **kwargs)
    on_quit()
```

Remove the now-unused `_BG_FLAGS` line and `INSTALL_PS1_URL` constant (and the comment block above `_BG_FLAGS`). Leave `VERSION_URL`, `WHEEL_URL`, `WHEEL_SHA_URL`, `REPO`, `BASE`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_updater.py -q`
Expected: PASS (new apply tests; the download/cache tests from Task 4 still green; the two deleted tests gone).
Then run the GUI suite to confirm `MainWindow._on_update_restart` (which calls `apply_update_and_restart(on_quit=...)`) still works with the new signature (it passes only `on_quit`, `cached_wheel` defaults to None): `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py -q`.

- [ ] **Step 5: Commit**

```bash
git add manager/updater.py manager/tests/test_updater.py
git commit -m "fix(updater): spawn detached helper for reinstall+relaunch; abort on no wheel

Replaces the fragile Start-Process-in-a-no-console-powershell relaunch (which
installed but never reopened the manager) with a tested Popen relaunch from a
detached helper. Network download + SHA verify happen before on_quit, so a
download/checksum failure leaves the app running instead of closing it with no
relaunch.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Auto pre-download in the GUI + pass the cached wheel to "Update & restart"

**Files:**
- Modify: `manager/gui/main_window.py` (`_on_update_checked` starts a pre-download worker; add `_do_predownload`, `_on_predownload_done`, `self._cached_wheel`, `self._latest_version`; `_on_update_restart` passes `cached_wheel`).
- Test: `manager/tests/test_main_window.py`.

**Interfaces:**
- Consumes: `updater.download_update() -> Path` (raises on failure) (Task 4); `updater.apply_update_and_restart(on_quit, cached_wheel=None)` (Task 6).
- Produces: a pre-download worker that, on a detected update, downloads+verifies the wheel and updates the label to "ready — restart in seconds"; `_on_update_restart` passes the stashed wheel.

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_main_window.py`:

```python
def test_predownload_done_sets_cached_wheel_and_ready_label(qapp):
    from manager.gui.main_window import MainWindow
    from pathlib import Path
    w = MainWindow(FakeController())
    w._latest_version = "0.2.0"
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert w._cached_wheel == Path("C:/cached/manager-latest.whl")
    assert "ready" in w.update_label.text().lower()


def test_update_restart_passes_cached_wheel(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    from pathlib import Path
    captured = {}
    from manager import updater
    monkeypatch.setattr(updater, "apply_update_and_restart",
                        lambda on_quit, cached_wheel=None: captured.update(
                            {"on_quit": on_quit, "cached_wheel": cached_wheel}))
    w = MainWindow(FakeController())
    w._cached_wheel = Path("C:/cached/manager-latest.whl")
    w._on_update_restart()
    assert captured["cached_wheel"] == Path("C:/cached/manager-latest.whl")


def test_update_restart_refuses_while_running(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    c.started = True
    w = MainWindow(c)
    w._cached_wheel = None
    w._on_update_restart()  # is_running() True -> logs, does not call apply
    assert "stop" in w.log_view.toPlainText().lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py::test_predownload_done_sets_cached_wheel_and_ready_label manager/tests/test_main_window.py::test_update_restart_passes_cached_wheel -q`
Expected: FAIL (no `_latest_version`/`_cached_wheel`/`_on_predownload_done`).

- [ ] **Step 3: Implement**

In `manager/gui/main_window.py` `__init__`, after `self._update_worker = None` add:

```python
        self._predownload_worker = None
        self._cached_wheel = None
        self._latest_version = None
```

Rewrite `_on_update_checked`:

```python
    def _on_update_checked(self, info) -> None:
        self._update_worker = None
        if info.latest is None and not info.available:
            self.update_label.setText("Couldn't check for updates")
            self.update_restart_button.setVisible(False)
            return
        if info.available:
            self._latest_version = info.latest
            self.update_label.setText(f"Update available: v{info.latest}")
            self.update_restart_button.setVisible(True)
            self.update_restart_button.setEnabled(not self._controller.is_running())
            self._predownload_worker = _UpdateWorker(self._do_predownload, self)
            self._predownload_worker.done.connect(self._on_predownload_done)
            self._predownload_worker.start()
        else:
            self.update_label.setText(f"Up to date (v{info.current})")
            self.update_restart_button.setVisible(False)

    def _do_predownload(self):
        from manager import updater
        try:
            return updater.download_update()
        except Exception:
            return None

    def _on_predownload_done(self, wheel) -> None:
        self._predownload_worker = None
        if wheel is None or self._latest_version is None:
            return
        self._cached_wheel = wheel
        self.update_label.setText(
            f"Update available: v{self._latest_version} (ready — restart in seconds)")
```

Rewrite `_on_update_restart`:

```python
    def _on_update_restart(self) -> None:
        if self._controller.is_running():
            self.append_log("stop copying before updating")
            return
        from manager import updater
        updater.apply_update_and_restart(
            on_quit=self._do_update_quit, cached_wheel=self._cached_wheel)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window.py
git commit -m "feat(gui): auto pre-download update wheel; pass cached wheel to restart

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: README + docs/TESTING updates

**Files:**
- Modify: `README.md`.
- Modify: `docs/TESTING.md`.

**Interfaces:** none (docs).

- [ ] **Step 1: Edit `README.md`**

1. In the **Features** section, update the manual-login + buttons bullet (currently "Launch terminal / Install MetaTrader buttons — launch a selected terminal's `terminal64.exe` to log in/verify, or open the MT5 download page to install another terminal (use a custom install path per terminal).") to:
   > **Install MetaTrader / Open terminal for login buttons** — open the MT5 download page to install another terminal (custom install path per terminal), or open a selected terminal's `terminal64.exe` login window to log in/verify. Install is on the left; Open-for-login is on the right.
2. Add a Features bullet after the Restart-recovery bullet:
   > **Persistent config** — the master terminal + slaves (with per-slave symbol map / lot-sizing / normalization) are saved to `settings.json` and restored on the next launch, so a restart (or an update) does not lose your setup.
3. In the **Usage** walkthrough, after the "Tray" step, add a short "Updates" note: the app checks for updates hourly and pre-downloads the verified wheel when one is found, so clicking **Update & restart** finishes in seconds (no network in the restart path) and reliably relaunches the manager.
4. **Testing** count line: replace the existing `167 passed, 5 skipped (193 passed with PySide6)` with the actual numbers measured in Step 3.
5. In **Troubleshooting**, if there is a row about the updater/restart, update it to point at `%LOCALAPPDATA%\CopyTradesMT5\updates\update.log` for diagnosing a failed relaunch. (If no such row exists, add one: "Update & restart closes the app but it doesn't reopen | The detached helper's pip install or relaunch step failed | Open `%LOCALAPPDATA%\CopyTradesMT5\updates\update.log` for the step that failed; re-run `copytrades update` or the one-liner installer.")

- [ ] **Step 2: Edit `docs/TESTING.md`**

1. Add `test_update_helper.py` to the section-3 table with description "Detached update helper: wait for parent exit → reinstall → relaunch (mocked)".
2. Update the `test_updater.py` description to "Version compare + wheel pre-download/SHA-verify/cache + apply-and-restart (mocked network/popen)".
3. Update the headless + PySide6 count lines to the actual numbers measured in Step 3.

- [ ] **Step 3: Run both suites and record the actual counts**

Run: `python -m pytest -q` and record `N passed, M skipped` (headless).
Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q` and record `P passed, 0 skipped` (PySide6).
Write these actual numbers into the README (Step 1.4) and `docs/TESTING.md` (Step 2.3) in place of the placeholders above. Confirm both suites are green (no new failures vs the baseline; counts reflect the new tests added in Tasks 1-7).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/TESTING.md
git commit -m "docs: updater pre-download + restart-fix + config persistence; button relabel

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- GUI button swap + relabel (both files) → Task 1.
- Config persistence (store accessors + MainWindow save/load/restore + quit hook + save on Start/add/remove) → Tasks 2 + 3. (`aboutToQuit` covers tray Quit and update-quit uniformly.)
- Pre-download (`download_update` + `cached_update` + `UpdateDownloadError`) → Task 4.
- Auto pre-download on detection + cached wheel to restart → Task 7.
- Robust restart (detached helper, wait→reinstall→relaunch, log, force-kill after cap) → Task 5.
- `apply_update_and_restart` rewrite (ensure wheel, abort on failure, spawn helper, on_quit) → Task 6.
- `install.ps1` unchanged (fresh installs) → no task needed (explicitly out of scope).
- `__main__.py` `update` subcommand uses the new apply path automatically (no code change needed — it calls `apply_update_and_restart(on_quit=...)`, unchanged signature) → no task needed.
- README + docs/TESTING → Task 8.

**2. Placeholder scan:** No TBD/TODO/“add error handling”/“similar to Task N”. Each code step has the actual code. The only measured values (test counts in Task 8) are filled by running the suites, which is the concrete action — not a placeholder.

**3. Type consistency:**
- `download_update(dest_dir=None) -> Path`, `cached_update() -> Path | None`, `apply_update_and_restart(on_quit, cached_wheel=None) -> None` — consistent across Tasks 4/6/7.
- `update_helper.main(argv) -> int`, `_pid_exists`, `_wait_for_parent`, `_reinstall`, `_relaunch`, `_log` — consistent across Task 5 tests + impl.
- `MainWindow(controller, store=None, parent=None)` — consistent across Tasks 1/3/7 tests + impl.
- `_DETACHED_FLAGS` defined in Task 4 (updater.py) for the apply spawn; `update_helper` redefines its own `_DETACHED_FLAGS` locally (import-light, no manager import) — intentional, not a mismatch.
- `_helper_exe()` is defined in Task 6 and monkeypatched in Task 6 tests — consistent.

No issues found. Plan is complete.