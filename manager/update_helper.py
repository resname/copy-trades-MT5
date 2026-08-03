"""Detached update helper, launched by updater.apply_update_and_restart as a
fully-decoupled process: ``pythonw -m manager.update_helper <wheel> <parent_pid>``.
Waits for the parent manager to exit, reinstalls the cached wheel, relaunches
the manager, and exits. Import-light: stdlib + psutil only — no PySide6, no
manager package imports — so it keeps running while the old package is being
overwritten by pip.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    with zipfile.ZipFile(wheel) as z:
        meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        text = z.read(meta).decode("utf-8", "replace")
    name = re.search(r"^Name:\s*(.+)$", text, re.M).group(1).strip()
    ver = re.search(r"^Version:\s*(.+)$", text, re.M).group(1).strip()
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
    valid = _valid_wheel_copy(wheel)
    _log(f"installing wheel as {os.path.basename(valid)}")
    try:
        return subprocess.call([sys.executable, "-m", "pip", "install",
                                "--upgrade", "--force-reinstall", valid])
    finally:
        shutil.rmtree(os.path.dirname(valid), ignore_errors=True)


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