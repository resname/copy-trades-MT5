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