# Updater progress feedback + ready-gated restart button

## Problem

The updater's update flow is confusing:

1. **No progress while the background predownload runs.** When an update is
   detected, the manager silently predownloads the wheel in a background
   thread so the eventual restart is fast. The only feedback is the static
   label "Update available: v{latest}", then a one-shot "ready" label at the
   end. On a slow connection there is no sign anything is happening.
2. **The "Update & restart" button is enabled before the wheel is ready.**
   It enables the moment an update is detected — before the predownload
   finishes — so clicking it then triggers a slow download-at-click instead
   of the intended fast cached restart.
3. **No feedback during the reinstall after the app quits.** Clicking
   "Update & restart" spawns a detached helper, then the manager window
   closes immediately so pip can reinstall without locked files. The helper
   waits for the parent to exit, reinstalls the wheel, and relaunches. While
   the window is closed and the helper works, **nothing is visible** — it
   looks like the app just crashed. This is the core confusion.
4. **On reinstall failure the user is left with nothing.** Today, if the
   helper's `pip install` fails, it logs the error and exits *without*
   relaunching — but the manager already quit, so no app is running.

## Goal

Make the update visibly "in progress" at every stage, gate the restart
button on a verified-ready wheel, and never leave the user with no manager
running.

Decisions (approved):
- **Approach 1 — dual indicator:** a determinate `QProgressBar` in the main
  window for the background download, and a small **tkinter** indeterminate
  progress window shown by the detached helper during the reinstall gap.
- **Restart button is greyed out** until the predownloaded wheel is
  SHA-verified ready; it enables only when the wheel is ready *and* the
  manager is idle.
- **On reinstall failure, relaunch the previous version** so the user is
  never left with no app running.

## Architecture

Two independent indicators, each the lightest fit for its context:

- **In-app (Qt):** a `QProgressBar` in the updates row + a button-state
  helper, driven by a reporthook signal from a new `_DownloadWorker`
  QThread.
- **In-helper (tkinter):** a stdlib-only indeterminate window that survives
  the manager package being overwritten, shown immediately on helper start,
  running a real `mainloop()` while the work happens in a worker thread.

The two paths never share code (the manager is gone by the time the helper
runs); they share only the cached wheel on disk and the wheel's METADATA
(for the version label).

## Design

### Change 1 — In-app download progress + ready-gated button

`manager/gui/main_window.py`:

- Add `self.update_progress = QProgressBar()` to the updates row, hidden by
  default. Range 0..100 when a total size is known; `setRange(0, 0)`
  (indeterminate) when it isn't.
- Add `self._apply_update_button_state()`:
  ```python
  def _apply_update_button_state(self) -> None:
      self.update_restart_button.setEnabled(
          self._cached_wheel is not None
          and not self._controller.is_running())
  ```
  Called from `_on_update_checked`, `_on_predownload_done`, and
  `set_running`.
- On `_on_update_checked(available=True)`:
  - label `"Update available: v{latest} — downloading…"`
  - `update_restart_button.setVisible(True)` but **disabled** (greyed)
  - `update_progress` shown, value 0, range 0..100
  - start a `_DownloadWorker` (new) running `_do_predownload(progress_cb)`
    with `progress` → `_on_download_progress` and `done` →
    `_on_predownload_done`.
- `_on_download_progress(done, total)`:
  - `total < 0` → `update_progress.setRange(0, 0)` (indeterminate)
  - else `setRange(0,100)`, `pct = 0 if total==0 else min(100, done*100//total)`,
    `setValue(pct)`.
- `_on_predownload_done(wheel)`:
  - hide `update_progress`; clear `_predownload_worker`
  - `wheel is None` (download/verify failed) → `_cached_wheel = None`,
    label `"Update download failed — click Check to retry"`,
    button stays disabled
  - success → `_cached_wheel = wheel`,
    label `"Update ready: v{latest} — restart in seconds"`,
    `_apply_update_button_state()` (enables if idle).
- `set_running(running)`: also call `_apply_update_button_state()` so
  stopping a copy job re-enables a ready update button.

`manager/gui/main_window.py` — split the worker:

- Keep `_UpdateWorker(QThread)` for the no-progress version check
  (`check_for_update`). Add `_DownloadWorker(QThread)`:
  ```python
  class _DownloadWorker(QThread):
      done = Signal(object)
      progress = Signal(int, int)   # bytes_done, bytes_total (-1 if unknown)
      def __init__(self, fn, parent=None):
          super().__init__(parent)
          self._fn = fn              # fn(progress_cb) -> Path | None
      def run(self):
          self.done.emit(self._fn(self._emit))
      def _emit(self, done, total):
          self.progress.emit(done, total)
  ```
- `_do_predownload(progress_cb=None)`:
  ```python
  def _do_predownload(self, progress_cb=None):
      from manager import updater
      try:
          return updater.download_update(progress=progress_cb)
      except Exception:
          return None
  ```

`manager/updater.py` — reporthook:

```python
def download_update(dest_dir: Path | None = None,
                    progress=None) -> Path:
    ...
    def _hook(block_num, block_size, total_size):
        if progress is None:
            return
        progress(block_num * block_size,
                 total_size if total_size > 0 else -1)
    try:
        urllib.request.urlretrieve(WHEEL_URL, str(wheel_path), reporthook=_hook)
        urllib.request.urlretrieve(WHEEL_SHA_URL, str(sha_path))  # tiny; no hook
    except Exception as exc:
        raise UpdateDownloadError(f"download failed: {exc}") from exc
    ...  # SHA verify unchanged
```

### Change 2 — Helper progress window + always-relaunch

`manager/update_helper.py`:

- Factor `_read_wheel_metadata(wheel) -> tuple[str, str]` (name, version)
  out of the existing `_valid_wheel_name` (which keeps using it).
- Add `_run_update_with_window(wheel, parent_pid) -> int`:
  - builds a tkinter `Tk()` root + `ttk.Label` `"Installing update v{version}…"`
    + `ttk.Progressbar(mode="indeterminate")` (started, animating)
  - a daemon worker thread runs `_wait_for_parent(parent_pid)` then
    `rc = _reinstall(wheel)`, then `root.after(0, _finish)`
  - `_finish`: `bar.stop()`; on `rc == 0` label
    `"Update installed — restarting…"`, else
    `"Update failed — see update.log; relaunching previous version…"`;
    `root.after(800, root.quit)` so the result is readable
  - main thread `root.mainloop()`; on return, `root.destroy()`; return `rc`
  - the whole thing is wrapped so any `tkinter` import / Tk error falls back
    to the headless path (below).
- Rewrite `main()`:
  ```python
  def main(argv=None) -> int:
      ...parse args (wheel, parent_pid)...
      if _can_show_window():
          rc = _run_update_with_window(wheel, parent_pid)
      else:
          _wait_for_parent(parent_pid)
          rc = _reinstall(wheel)
      if rc != 0:
          _log("pip install failed rc={rc}; relaunching previous version")
      _log("relaunching manager")
      _relaunch()
      return 0
  ```
  Both the windowed and headless paths **always relaunch** (success or
  failure) — fixing the "no app running" failure mode. Returns 0 in both
  cases because the relaunch handles the outcome (the window/log carries the
  failure detail).

- `_can_show_window()`: `try: import tkinter` → `True`, else `False`. The
  window creation is additionally guarded with try/except so a Tk runtime
  error degrades to headless.

### What stays the same

- The detached-spawn mechanism (`apply_update_and_restart`, `_DETACHED_FLAGS`,
  `pythonw -m manager.update_helper`).
- The wheel download + SHA256 verification logic in `download_update`
  (only gains an optional `progress` callback).
- `--no-deps` reinstall (pure-Python wheel; locked dependency DLLs untouched).
- The version-check logic and the hourly/10s-on-start check cadence.
- The "stop copying before updating" guard in `_on_update_restart`.

## Edge cases

- **Unknown download size:** GitHub Releases sends `Content-Length`, so the
  bar is normally determinate. If a proxy strips it, the bar flips to
  indeterminate rather than misreporting 0%.
- **Predownload fails:** button stays disabled, label offers "Check to
  retry"; the check button re-runs the whole detect+download.
- **Predownload succeeds while a copy is running:** button stays disabled
  until the user stops copying; `set_running(False)` then enables it.
- **Reinstall failure loop:** a broken published wheel makes the relaunched
  manager re-detect the same bad update; repeated manual restarts would
  re-fail. The failure window makes this visible so the user stops clicking
  restart. Relaunching the previous version is still better than leaving
  nothing running.
- **No display / no tkinter:** helper runs headless but still always
  relaunches.

## Tests (TDD)

1. **Button greyed until downloaded:** `_on_update_checked(available=True)`
   → `update_restart_button.isEnabled() is False`, `update_progress`
   visible; then `_on_predownload_done(wheel)` (idle) → enabled.
2. **Ready button stays disabled while running:** predownload done,
   controller running → disabled; `set_running(False)` → enabled.
3. **Download progress drives the bar:** `_on_download_progress(50, 200)`
   → bar value 25; `(-1, -1)` → indeterminate range.
4. **Download failure keeps button disabled:** `_on_predownload_done(None)`
   → disabled, label contains "failed".
5. **`download_update` invokes the progress callback:** monkeypatch
   `urlretrieve` with a fake that calls the reporthook; assert `progress` was
   called with increasing bytes and a positive total.
6. **Helper always relaunches on failure (headless path):** monkeypatch
   `_reinstall` → 1 and `_can_show_window` → False; `main()` calls
   `_relaunch()` and returns 0.
7. **`_read_wheel_metadata` returns name + version** (pure function; reused
   by `_valid_wheel_name` and the window label).
8. **Existing in-app tests updated:** `test_update_available_enables…`
   flips to assert the button is *disabled* immediately and enabled only
   after `_on_predownload_done`; the predownload worker is stubbed with a
   no-thread double to avoid real network in CI.