"""Detached update helper, launched by updater.apply_update_and_restart as a
fully-decoupled process: ``pythonw -m manager.update_helper <wheel> <parent_pid>``.
Waits for the parent manager to exit, reinstalls the cached wheel, relaunches
the manager, and exits. Import-light: stdlib + psutil only — no PySide6, no
manager package imports — so it keeps running while the old package is being
overwritten by pip.
"""
from __future__ import annotations

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


def _valid_wheel_copy(wheel: str) -> str:
    """Copy the cached wheel to a temp path with a valid PEP 427 filename so
    pip accepts it (see _valid_wheel_name). Returns the copy's path."""
    dst_dir = tempfile.mkdtemp(prefix="ctm5_update_")
    dst = os.path.join(dst_dir, _valid_wheel_name(wheel))
    shutil.copyfile(wheel, dst)
    return dst


def _reinstall(wheel: str) -> int:
    # pip rejects the stable cache name (manager-latest.whl) as an invalid
    # wheel filename; install from a valid-named copy instead, and clean up
    # the temp copy afterward so repeated updates don't accumulate in temp.
    # --no-deps: the manager wheel is pure Python (py3-none-any); only it needs
    # reinstalling. Without --no-deps, --force-reinstall reinstalls every dep
    # (PySide6, shiboken6, psutil, ...) and uninstalling shiboken6 hits its
    # locked msvcp140.dll -> WinError 5 -> pip rc=1 -> helper bails without
    # relaunching, so the new manager window never opens. The deps are already
    # present in the venv and unchanged between releases, so skip them.
    valid = _valid_wheel_copy(wheel)
    _log(f"installing wheel as {os.path.basename(valid)}")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps",
             "--upgrade", "--force-reinstall", valid],
            capture_output=True, text=True)
        if proc.returncode != 0:
            _log(f"pip install failed rc={proc.returncode}")
            _log(f"pip stdout:\n{proc.stdout}")
            _log(f"pip stderr:\n{proc.stderr}")
        return proc.returncode
    finally:
        shutil.rmtree(os.path.dirname(valid), ignore_errors=True)


def _relaunch() -> None:
    kwargs: dict = {"close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_FLAGS
    subprocess.Popen([sys.executable, "-m", "manager"], **kwargs)


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


if __name__ == "__main__":
    raise SystemExit(main())