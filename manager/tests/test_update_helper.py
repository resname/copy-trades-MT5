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