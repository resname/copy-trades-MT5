# Updater Progress Feedback + Ready-Gated Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the manager's update flow visibly "in progress" at every stage: a determinate progress bar for the background predownload, the "Update & restart" button greyed out until the predownloaded wheel is SHA-verified ready, and a small tkinter progress window shown by the detached helper during the reinstall gap — with the helper always relaunching (even on failure) so the user is never left with no app running.

**Architecture:** Two independent indicators. In-app (Qt): a `QProgressBar` in the updates row driven by a `urlretrieve` reporthook emitted from a new `_DownloadWorker` QThread; a `_apply_update_button_state()` rule `enabled = cached_wheel ready AND not running`. In-helper (tkinter): a stdlib-only indeterminate window shown immediately on helper start with the work in a worker thread so the bar animates; on completion it relaunches unconditionally (success or failure). The two paths share only the cached wheel on disk and the wheel's METADATA (for the version label).

**Tech Stack:** Python 3, PySide6 (Qt), stdlib `tkinter`/`ttk`, stdlib `urllib.request` with `reporthook`, `threading` + `queue`, `pytest` with the `qapp` offscreen fixture and monkeypatching.

## Global Constraints

- The download progress callback is optional (`progress=None` default); existing callers of `download_update` (no `progress`) and existing test stubs (`lambda dest_dir=None: ...`) must keep working unchanged — `apply_update_and_restart` never passes `progress` (the click-path downloads without a bar).
- The "Update & restart" button is `enabled = (cached wheel ready) AND (not running)`. It is shown-but-disabled the moment an update is detected and enabled only on verified predownload success while idle.
- The helper's reinstall window uses **stdlib tkinter only** — no PySide6, no manager-package imports — so it survives the package being overwritten. The window is a thin layer; the core sequence (wait → reinstall → relaunch) stays a testable headless function the window wraps.
- The helper **always relaunches** the manager, success or failure (fixes the "no app running" failure mode). Returns `0` in both cases; the failure detail lives in the window text + `update.log`.
- Tests never pop a real tkinter window or hit the network. Every `update_helper.main()` test forces the headless path via `monkeypatch.setattr(update_helper, "_can_show_window", lambda: False)`. Every `MainWindow._on_update_checked(available=True)` test stubs both `_UpdateWorker` and `_DownloadWorker` with no-thread doubles.
- The detached-spawn mechanism, SHA256 verification, `--no-deps` reinstall, version-check cadence, and the "stop copying before updating" guard are unchanged.

---

## File Structure

- **Modify:** `manager/updater.py` — `download_update` gains an optional `progress=callable(done, total)` wired to a `urlretrieve` reporthook.
- **Modify:** `manager/gui/main_window.py` — add `update_progress` `QProgressBar`; split `_DownloadWorker(QThread)` out of `_UpdateWorker`; add `_apply_update_button_state()`, `_on_download_progress()`; rework `_on_update_checked`, `_on_predownload_done`, `set_running`; `_do_predownload` accepts a `progress_cb`.
- **Modify:** `manager/update_helper.py` — factor `_read_wheel_metadata(wheel) -> (name, version)` out of `_valid_wheel_name`; add `_can_show_window()`, `_run_update_with_window(wheel, parent_pid) -> int`; rework `main()` to use the window when possible and always relaunch.
- **Modify (tests):** `manager/tests/test_updater.py` — one new test (progress callback invoked).
- **Modify (tests):** `manager/tests/test_main_window_updates.py` — add `_NoThreadDownloadWorker` stub; flip/extend existing tests for the disabled-until-ready behavior; add progress-bar and failure tests.
- **Modify (tests):** `manager/tests/test_update_helper.py` — force headless in existing `main()` tests; flip the failure test to "relaunches previous version"; add a `_read_wheel_metadata` test.

---

## Task 1: `download_update` progress callback

Add an optional progress callback to the wheel download, wired to `urlretrieve`'s reporthook. This is the foundation Task 2's in-app bar builds on.

**Files:**
- Modify: `manager/updater.py` — `download_update` (line ~96-120)
- Test: `manager/tests/test_updater.py` (append one test)

**Interfaces:**
- Consumes: existing `download_update(dest_dir=None)`.
- Produces: `download_update(dest_dir: Path | None = None, progress=None) -> Path`, where `progress` is an optional `callable(bytes_done: int, bytes_total: int)` (total `-1` when unknown). Task 2's `_do_predownload` passes `progress_cb` here; `apply_update_and_restart` calls it with no `progress` (unchanged).

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_updater.py`:

```python
def test_download_update_reports_progress_via_callback(tmp_path, monkeypatch):
    import hashlib
    from manager import updater
    data = b"wheel-bytes"

    def fake_urlretrieve(url, filename=None, reporthook=None, *a, **k):
        if str(url).endswith(".whl"):
            Path(filename).write_bytes(data)
        else:
            Path(filename).write_text(
                hashlib.sha256(data).hexdigest() + "  manager-latest.whl",
                encoding="utf-8")
        if reporthook is not None:
            # urlretrieve calls reporthook(block_num, block_size, total_size)
            reporthook(0, 100, 300)
            reporthook(1, 100, 300)
            reporthook(2, 100, 300)
        return filename, {}

    monkeypatch.setattr(updater.urllib.request, "urlretrieve", fake_urlretrieve)
    calls = []
    updater.download_update(dest_dir=tmp_path,
                            progress=lambda done, total: calls.append((done, total)))
    # _hook computes downloaded = block_num * block_size; total stays 300
    assert calls == [(0, 300), (100, 300), (200, 300)]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_updater.py::test_download_update_reports_progress_via_callback -v`
Expected: FAIL — `TypeError: download_update() got an unexpected keyword argument 'progress'`.

- [ ] **Step 3: Write the minimal implementation**

In `manager/updater.py`, change `download_update` to accept `progress` and wire a reporthook. Replace the function signature and the two `urlretrieve` calls:

```python
def download_update(dest_dir: Path | None = None,
                    progress=None) -> Path:
    """Download WHEEL_URL + WHEEL_SHA_URL into dest_dir (default UPDATE_DIR),
    verify SHA256, and return the verified wheel path. Raises
    UpdateDownloadError on a network failure or checksum mismatch (the
    mismatched files are removed). If ``progress`` is given, it is called as
    progress(bytes_done, bytes_total) during the wheel download (total -1
    when the server does not provide Content-Length)."""
    dest = dest_dir if dest_dir is not None else UPDATE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    wheel_path = dest / "manager-latest.whl"
    sha_path = dest / "manager-latest.whl.sha256"

    def _hook(block_num, block_size, total_size):
        if progress is None:
            return
        progress(block_num * block_size,
                 total_size if total_size > 0 else -1)

    try:
        urllib.request.urlretrieve(WHEEL_URL, str(wheel_path), reporthook=_hook)
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
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_updater.py::test_download_update_reports_progress_via_callback -v`
Expected: PASS.

- [ ] **Step 5: Run the full updater suite for regressions**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_updater.py -v`
Expected: PASS — in particular `test_apply_update_and_restart_*` (which call `download_update()` with no `progress`) and `test_download_update_verifies_sha_and_returns_path` / `test_download_update_raises_on_checksum_mismatch` (whose `fake_urlretrieve(url, filename=None, *a, **k)` absorbs the new `reporthook=_hook` kwarg and ignores it) must stay green.

- [ ] **Step 6: Commit**

```bash
git add manager/updater.py manager/tests/test_updater.py
git commit -m "feat(updater): optional progress callback on download_update"
```

---

## Task 2: In-app download progress + ready-gated restart button

Add a determinate `QProgressBar` to the updates row, drive it from a new `_DownloadWorker` thread, and grey out "Update & restart" until the predownloaded wheel is verified ready (and the manager is idle).

**Files:**
- Modify: `manager/gui/main_window.py` — imports, `_UpdateWorker`/new `_DownloadWorker`, `_build_ui` (add bar), `_on_update_checked`, `_on_download_progress` (new), `_on_predownload_done`, `_apply_update_button_state` (new), `_do_predownload`, `set_running`
- Test: `manager/tests/test_main_window_updates.py` — add `_NoThreadDownloadWorker` stub; flip `test_update_available_enables_restart_only_when_idle`; add `test_update_available_disables_restart_until_downloaded`, `test_update_ready_enables_restart_when_idle`, `test_ready_update_stays_disabled_while_running_and_enables_on_stop`, `test_download_progress_updates_bar`, `test_predownload_failure_keeps_button_disabled`; update `test_update_available_disables_restart_while_running` to also stub `_DownloadWorker`

**Interfaces:**
- Consumes: Task 1's `download_update(progress=...)`.
- Produces: `MainWindow.update_progress` (`QProgressBar`), `MainWindow._apply_update_button_state()`, `MainWindow._on_download_progress(done, total)`, and the module-level `_DownloadWorker(QThread)` class (with `done = Signal(object)` and `progress = Signal(int, int)`).

- [ ] **Step 1: Write the failing tests**

In `manager/tests/test_main_window_updates.py`, add the import `from pathlib import Path` at the top (alongside the existing `from manager.updater import UpdateInfo`), and add the new no-thread download-worker stub next to `_NoThreadUpdateWorker`:

```python
class _NoThreadDownloadWorker:
    """Test double for manager.gui.main_window._DownloadWorker. The real
    worker runs updater.download_update(progress=cb) on a QThread (a real
    network download); tests only check label/button/bar state, so this
    no-op worker avoids leaking a running thread and network I/O. Its
    done/progress signals are never emitted, so _on_predownload_done is never
    called (button stays disabled = the 'still downloading' state)."""
    done = _StubSignal()
    progress = _StubSignal()
    def __init__(self, fn, parent=None):
        self._fn = fn
    def start(self):
        pass
```

Flip the existing `test_update_available_enables_restart_only_when_idle` — rename it and change the final assertion. Replace the whole function with:

```python
def test_update_available_disables_restart_until_downloaded(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert "0.1.2" in w.update_label.text()
    assert w.update_restart_button.isVisibleTo(w) is True
    # greyed out until the predownloaded wheel is verified ready
    assert w.update_restart_button.isEnabled() is False
    # the progress bar is shown while the download is in progress
    assert w.update_progress.isVisibleTo(w) is True
```

Update `test_update_available_disables_restart_while_running` to also stub `_DownloadWorker`:

```python
def test_update_available_disables_restart_while_running(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=True))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert w.update_restart_button.isEnabled() is False
```

Append the new tests:

```python
def test_update_ready_enables_restart_when_idle(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    assert w.update_restart_button.isEnabled() is False
    # simulate the predownload finishing with a verified wheel
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))
    assert w.update_restart_button.isEnabled() is True
    # the bar hides once the wheel is ready
    assert w.update_progress.isVisibleTo(w) is False


def test_ready_update_stays_disabled_while_running_and_enables_on_stop(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=True))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(Path("C:/cached/manager-latest.whl"))  # wheel ready
    assert w.update_restart_button.isEnabled() is False  # still copying
    w.set_running(False)  # stop copying
    assert w.update_restart_button.isEnabled() is True


def test_download_progress_updates_bar(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_download_progress(50, 200)   # 50 of 200 bytes -> 25%
    assert w.update_progress.maximum() == 100
    assert w.update_progress.value() == 25
    w._on_download_progress(0, -1)     # unknown total -> indeterminate
    assert w.update_progress.maximum() == 0  # setRange(0,0) makes max 0


def test_predownload_failure_keeps_button_disabled(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    monkeypatch.setattr("manager.gui.main_window._UpdateWorker", _NoThreadUpdateWorker)
    monkeypatch.setattr("manager.gui.main_window._DownloadWorker", _NoThreadDownloadWorker)
    w = MainWindow(FakeController(running=False))
    w._on_update_checked(UpdateInfo(available=True, current="0.1.1", latest="0.1.2"))
    w._on_predownload_done(None)  # download/verify failed
    assert w.update_restart_button.isEnabled() is False
    assert "failed" in w.update_label.text().lower()
    assert w.update_progress.isVisibleTo(w) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window_updates.py -v`
Expected: FAIL — `MainWindow` has no attribute `update_progress` / `_on_download_progress` / `_apply_update_button_state`, and `_DownloadWorker` is missing; the renamed `test_update_available_disables_restart_until_downloaded` asserts `isEnabled() is False` against today's `True`.

- [ ] **Step 3: Write the minimal implementation**

In `manager/gui/main_window.py`:

1. Add `QProgressBar` to the `PySide6.QtWidgets` import line (line ~8-11):

```python
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
    QProgressBar,
)
```

2. Add the `_DownloadWorker` class immediately after the existing `_UpdateWorker` class (after line ~28):

```python
class _DownloadWorker(QThread):
    """Runs updater.download_update(progress=cb) off the GUI thread; emits
    progress(bytes_done, bytes_total) during the download and done(wheel) on
    completion (wheel is a Path on success, None on failure)."""
    done = Signal(object)
    progress = Signal(int, int)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        self.done.emit(self._fn(self._emit))

    def _emit(self, done, total):
        self.progress.emit(done, total)
```

3. In `_build_ui`, add the progress bar to the Updates block. Replace the Updates block (line ~115-123):

```python
        # Updates
        self.update_label = QLabel("")
        self.check_update_button = QPushButton("Check for updates")
        self.update_restart_button = QPushButton("Update & restart")
        self.update_restart_button.setVisible(False)
        self.update_progress = QProgressBar()
        self.update_progress.setRange(0, 100)
        self.update_progress.setVisible(False)
        updates_row = QHBoxLayout()
        updates_row.addWidget(self.update_label, 1)
        updates_row.addWidget(self.check_update_button)
        updates_row.addWidget(self.update_restart_button)
```
and add, right after the `updates_row` is added to the layout (after `root.addLayout(updates_row)`):
```python
        root.addWidget(self.update_progress)
```

4. Replace the `_on_update_checked` method (line ~225-241) with:

```python
    def _on_update_checked(self, info) -> None:
        self._update_worker = None
        if info.latest is None and not info.available:
            self.update_label.setText("Couldn't check for updates")
            self.update_restart_button.setVisible(False)
            self.update_progress.setVisible(False)
            return
        if info.available:
            self._latest_version = info.latest
            self.update_label.setText(f"Update available: v{info.latest} — downloading…")
            self.update_restart_button.setVisible(True)
            self.update_restart_button.setEnabled(False)  # greyed until ready
            self.update_progress.setRange(0, 100)
            self.update_progress.setValue(0)
            self.update_progress.setVisible(True)
            self._predownload_worker = _DownloadWorker(self._do_predownload, self)
            self._predownload_worker.progress.connect(self._on_download_progress)
            self._predownload_worker.done.connect(self._on_predownload_done)
            self._predownload_worker.start()
        else:
            self.update_label.setText(f"Up to date (v{info.current})")
            self.update_restart_button.setVisible(False)
            self.update_progress.setVisible(False)
```

5. Add `_on_download_progress` and `_apply_update_button_state` (next to `_on_predownload_done`). Replace the `_on_predownload_done` method (line ~250-256) and add the two helpers:

```python
    def _on_download_progress(self, done: int, total: int) -> None:
        if total < 0:
            self.update_progress.setRange(0, 0)  # indeterminate
        else:
            self.update_progress.setRange(0, 100)
            pct = 0 if total == 0 else min(100, done * 100 // total)
            self.update_progress.setValue(pct)

    def _apply_update_button_state(self) -> None:
        """Enable Update & restart only when a verified wheel is cached AND
        the manager is idle. Called on download-done, on update-checked, and
        from set_running (so stopping a copy job re-enables a ready update)."""
        self.update_restart_button.setEnabled(
            self._cached_wheel is not None
            and not self._controller.is_running())

    def _on_predownload_done(self, wheel) -> None:
        self._predownload_worker = None
        self.update_progress.setVisible(False)
        if wheel is None or self._latest_version is None:
            self._cached_wheel = None
            self.update_label.setText("Update download failed — click Check to retry")
            self.update_restart_button.setEnabled(False)
            return
        self._cached_wheel = wheel
        self.update_label.setText(
            f"Update ready: v{self._latest_version} — restart in seconds")
        self._apply_update_button_state()
```

6. Update `_do_predownload` (line ~243-248) to accept a progress callback:

```python
    def _do_predownload(self, progress_cb=None):
        from manager import updater
        try:
            return updater.download_update(progress=progress_cb)
        except Exception:
            return None
```

7. Update `set_running` (line ~213-215) to also refresh the update button:

```python
    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self._apply_update_button_state()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window_updates.py -v`
Expected: PASS — all flipped/new tests green, and the untouched `test_update_restart_calls_updater_and_quits` / `test_update_restart_refuses_while_running` / `test_up_to_date_hides_restart` / `test_check_failed_label` / `test_update_ui_exists` still pass.

- [ ] **Step 5: Run the broader GUI suite for regressions**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py manager/tests/test_main_window_updates.py manager/tests/test_tray.py -v`
Expected: PASS — `set_running` now calls `_apply_update_button_state()` (which reads `self._cached_wheel`, initialized to `None` in `__init__`, and `self._controller.is_running()`), so existing start/stop tests must stay green.

- [ ] **Step 6: Commit**

```bash
git add manager/gui/main_window.py manager/tests/test_main_window_updates.py
git commit -m "feat(gui): download progress bar + ready-gated update-restart button"
```

---

## Task 3: Helper progress window + always-relaunch

Show a small tkinter indeterminate progress window during the reinstall gap, factor wheel-metadata reading, and make the helper always relaunch (even on failure) so the user is never left with no app running.

**Files:**
- Modify: `manager/update_helper.py` — factor `_read_wheel_metadata`; add `_can_show_window`, `_run_update_with_window`; rework `main`
- Test: `manager/tests/test_update_helper.py` — force headless in existing `main()` tests; flip the failure test; add a `_read_wheel_metadata` test

**Interfaces:**
- Consumes: existing `_wait_for_parent`, `_reinstall`, `_relaunch`, `_valid_wheel_name`, `_make_wheel` (test helper).
- Produces: `_read_wheel_metadata(wheel) -> tuple[str, str]` (name, version); `_can_show_window() -> bool`; `_run_update_with_window(wheel, parent_pid) -> int` (returns pip rc; relaunch is NOT done inside — `main` does it unconditionally afterward); a reworked `main(argv) -> int` that always relaunches and returns `0`.

- [ ] **Step 1: Write/update the failing tests**

In `manager/tests/test_update_helper.py`, update the three existing `main()` tests to force the headless path, and flip the failure test.

`test_main_waits_for_parent_then_reinstalls_then_relaunches` — add the headless monkeypatch (insert after the `monkeypatch.setattr(update_helper, "_pid_exists", ...)` line):

```python
def test_main_waits_for_parent_then_reinstalls_then_relaunches(monkeypatch, tmp_path):
    seq = []
    monkeypatch.setattr(update_helper, "_can_show_window", lambda: False)
    monkeypatch.setattr(update_helper, "_pid_exists", lambda pid: pid != 12345)
    monkeypatch.setattr(update_helper, "_reinstall",
                        lambda wheel: seq.append(("install", wheel)) or 0)
    monkeypatch.setattr(update_helper, "_relaunch", lambda: seq.append(("relaunch",)))
    monkeypatch.setattr(update_helper, "_log", lambda m: None)
    rc = update_helper.main(["C:/cached/manager-latest.whl", "12345"])
    assert rc == 0
    assert seq == [("install", "C:/cached/manager-latest.whl"), ("relaunch",)]
```

`test_main_does_not_reinstall_until_parent_gone` — add the headless monkeypatch (insert after the `alive = {"v": True}` line):

```python
def test_main_does_not_reinstall_until_parent_gone(monkeypatch):
    alive = {"v": True}
    monkeypatch.setattr(update_helper, "_can_show_window", lambda: False)
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
```

Replace `test_main_skips_relaunch_when_pip_fails` entirely (the policy flips — the helper now relaunches the previous version on failure):

```python
def test_main_relaunches_previous_version_when_pip_fails(monkeypatch):
    """Regression: when pip reinstall fails, the manager has already quit. The
    helper must relaunch the (previous) manager anyway so the user is never
    left with no app running, and return 0 (handled). The failure detail lives
    in update.log and (when a display is available) the progress window."""
    monkeypatch.setattr(update_helper, "_can_show_window", lambda: False)
    monkeypatch.setattr(update_helper, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(update_helper, "_reinstall", lambda wheel: 1)
    relaunched = []
    monkeypatch.setattr(update_helper, "_relaunch", lambda: relaunched.append(1))
    monkeypatch.setattr(update_helper, "_log", lambda m: None)
    rc = update_helper.main(["C:/w.whl", "1"])
    assert rc == 0
    assert relaunched == [1]
```

Add the metadata test (append at the end of the file):

```python
def test_read_wheel_metadata_returns_name_and_version(tmp_path):
    """_read_wheel_metadata is shared by _valid_wheel_name (PEP 427 filename)
    and the progress window's 'Installing v{version}…' label."""
    w = _make_wheel(tmp_path / "manager-latest.whl",
                    name="copy-trades-mt5-manager", version="0.1.42")
    name, version = update_helper._read_wheel_metadata(str(w))
    assert name == "copy-trades-mt5-manager"
    assert version == "0.1.42"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_update_helper.py -v`
Expected: FAIL — `update_helper` has no attribute `_can_show_window` / `_read_wheel_metadata`; and `test_main_relaunches_previous_version_when_pip_fails` asserts `rc == 0` / `relaunched == [1]` against today's `rc == 1` / no relaunch.

- [ ] **Step 3: Write the minimal implementation**

In `manager/update_helper.py`:

1. Add `import threading` and `import queue` to the imports (near the top, alongside `import os`, `import re`, etc.):

```python
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
```

2. Factor `_read_wheel_metadata` out of `_valid_wheel_name`. Replace the `_valid_wheel_name` function (line ~80-97) with:

```python
def _read_wheel_metadata(wheel: str) -> tuple[str, str]:
    """Read (Name, Version) from the wheel's dist-info/METADATA. Shared by
    _valid_wheel_name (PEP 427 filename) and the progress window label."""
    with zipfile.ZipFile(wheel) as z:
        meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        text = z.read(meta).decode("utf-8", "replace")
    name = re.search(r"^Name:\s*(.+)$", text, re.M).group(1).strip()
    ver = re.search(r"^Version:\s*(.+)$", text, re.M).group(1).strip()
    return name, ver


def _valid_wheel_name(wheel: str) -> str:
    """Build a valid PEP 427 wheel filename from the wheel's own METADATA.

    The cached update wheel is stored under the stable download name
    ``manager-latest.whl``, which is NOT a valid wheel filename (only 2
    dash-parts; PEP 427 needs >=5). pip rejects it: ``ERROR: Invalid wheel
    filename (wrong number of parts): 'manager-latest'`` (rc=1), so the
    helper bailed without relaunching and no new window opened after an
    update. A valid name built from the wheel's Name+Version (and its
    py3-none-any tag) makes pip accept it.
    """
    name, ver = _read_wheel_metadata(wheel)
    dist = re.sub(r"[^A-Za-z0-9.]+", "_", name)
    return f"{dist}-{ver}-py3-none-any.whl"
```

3. Add `_can_show_window` and `_run_update_with_window` just before the `main` function:

```python
def _can_show_window() -> bool:
    """True only if tkinter is importable. The actual Tk root creation is
    additionally guarded inside _run_update_with_window so a missing display
    degrades to the headless path."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def _run_update_with_window(wheel: str, parent_pid: int) -> int:
    """Show an indeterminate progress window while waiting for the parent and
    reinstalling. The work runs in a thread so the bar keeps animating; the
    worker posts its result through a queue that the main thread polls via
    root.after (tkinter is not thread-safe — only the main thread touches Tk).
    Returns the pip rc; relaunch is handled by the caller (main) on BOTH
    success and failure. Falls back to headless on any tkinter/Tk error."""
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()           # raises TclError if there is no display
    except Exception:
        _wait_for_parent(parent_pid)
        return _reinstall(wheel)
    try:
        try:
            _, version = _read_wheel_metadata(wheel)
        except Exception:
            version = ""
        root.title("CopyTrades MT5 — Updating")
        root.resizable(False, False)
        label = tk.Label(root, text=f"Installing update v{version}…",
                         padx=24, pady=18)
        label.pack()
        bar = ttk.Progressbar(root, mode="indeterminate", length=260)
        bar.pack(padx=24, pady=(0, 18))
        bar.start(12)

        done_q: queue.Queue = queue.Queue()

        def _work():
            try:
                _wait_for_parent(parent_pid)
                rc = _reinstall(wheel)
            except Exception:
                rc = 1
            done_q.put(rc)

        def _poll():
            try:
                rc = done_q.get_nowait()
            except queue.Empty:
                root.after(100, _poll)
                return
            bar.stop()
            if rc == 0:
                label.config(text="Update installed — restarting…")
            else:
                label.config(text="Update failed — see update.log\n"
                                  "Relaunching previous version…")
            root.after(800, root.quit)

        threading.Thread(target=_work, daemon=True).start()
        root.after(100, _poll)
        root.mainloop()
        try:
            return done_q.get_nowait()
        except queue.Empty:
            return 0
    finally:
        try:
            root.destroy()
        except Exception:
            pass
```

4. Rework `main` (line ~142-159) to use the window when possible and always relaunch:

```python
def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) < 2:
        _log("update_helper: missing args (wheel, parent_pid)")
        return 2
    wheel = args[0]
    parent_pid = int(args[1])
    _log(f"update_helper start: wheel={wheel} parent={parent_pid}")
    if _can_show_window():
        rc = _run_update_with_window(wheel, parent_pid)
    else:
        _wait_for_parent(parent_pid)
        _log("parent gone; reinstalling")
        rc = _reinstall(wheel)
    if rc != 0:
        _log(f"pip install failed rc={rc}; relaunching previous version")
    else:
        _log("pip install ok; relaunching manager")
    _relaunch()
    _log("relaunch spawned; helper exit")
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_update_helper.py -v`
Expected: PASS — `test_main_waits_for_parent_then_reinstalls_then_relaunches` and `test_main_does_not_reinstall_until_parent_gone` (headless path) green; `test_main_relaunches_previous_version_when_pip_fails` green; `test_read_wheel_metadata_returns_name_and_version` green; and the untouched `_reinstall`/`_valid_wheel_name`/`test_main_missing_args` tests green.

- [ ] **Step 5: Run the full manager suite for regressions**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests -v`
Expected: PASS (the pre-existing `test_mt5_worker.py` BrokenPipeError warning, if present, is in a file this plan does not touch).

- [ ] **Step 6: Commit**

```bash
git add manager/update_helper.py manager/tests/test_update_helper.py
git commit -m "feat(updater): helper progress window + always relaunch on failure"
```

---

## Final Verification

After all three tasks:

- [ ] Run the whole manager suite: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests -v` — all green.
- [ ] Grep sanity check: `update_progress`, `_DownloadWorker`, `_apply_update_button_state`, `_on_download_progress`, `progress=` (in `download_update`), `_read_wheel_metadata`, `_can_show_window`, `_run_update_with_window` each appear exactly where intended.
- [ ] Confirm no existing test was silently deleted — the renamed `test_update_available_disables_restart_until_downloaded` and flipped `test_main_relaunches_previous_version_when_pip_fails` are present and assert the new behavior.