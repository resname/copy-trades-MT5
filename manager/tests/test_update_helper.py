import os
import re
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from manager import update_helper


def _make_wheel(path, name="copy-trades-mt5-manager", version="0.1.11"):
    """Build a minimal .whl zip with a dist-info/METADATA so the helper can read
    Name+Version. Written under the intentionally-invalid stable cache name
    (manager-latest.whl) to reproduce the real cached-wheel shape."""
    dist = re.sub(r"[^A-Za-z0-9.]+", "_", name) + "-" + version
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{dist}.dist-info/METADATA",
                   f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n")
        z.writestr(f"{dist}.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    return path


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


def test_valid_wheel_name_from_metadata(tmp_path):
    """The cached wheel is stored as 'manager-latest.whl' (the stable download
    name), which is NOT a valid PEP 427 wheel filename. _valid_wheel_name must
    build a valid name from the wheel's own Name+Version metadata."""
    w = _make_wheel(tmp_path / "manager-latest.whl")
    assert update_helper._valid_wheel_name(str(w)) == \
        "copy_trades_mt5_manager-0.1.11-py3-none-any.whl"


def test_reinstall_passes_valid_wheel_filename_to_pip(monkeypatch, tmp_path):
    """Regression: pip rejects 'manager-latest.whl' as an invalid wheel filename
    ('wrong number of parts', rc=1), so the helper bailed without relaunching
    and the new manager window never opened after an update. _reinstall must
    install from a valid-named copy so pip accepts it."""
    w = _make_wheel(tmp_path / "manager-latest.whl")
    captured = {}

    def fake_call(cmd, **k):
        captured["cmd"] = cmd
        # capture the copy's bytes now -- _reinstall removes the temp dir in
        # its `finally` right after this returns
        captured["wheel_bytes"] = Path(cmd[-1]).read_bytes()
        return 0

    monkeypatch.setattr(update_helper.subprocess, "call", fake_call)
    rc = update_helper._reinstall(str(w))
    assert rc == 0
    wheel_arg = captured["cmd"][-1]
    # pip must receive a valid PEP 427 wheel filename, not 'manager-latest.whl'
    assert os.path.basename(wheel_arg) == \
        "copy_trades_mt5_manager-0.1.11-py3-none-any.whl"
    # the copy pip was handed is byte-identical to the cached wheel
    assert captured["wheel_bytes"] == w.read_bytes()


def test_reinstall_cleans_up_temp_copy(monkeypatch, tmp_path):
    """The valid-named copy lives in a temp dir; remove it after pip runs so
    repeated updates don't accumulate copies in temp."""
    w = _make_wheel(tmp_path / "manager-latest.whl")
    seen = {}
    monkeypatch.setattr(update_helper.subprocess, "call",
                        lambda cmd, **k: seen.__setitem__(
                            "d", os.path.dirname(cmd[-1])) or 0)
    update_helper._reinstall(str(w))
    assert not os.path.exists(seen["d"]), "temp copy dir should be removed after install"