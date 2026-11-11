import base64
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


def test_is_autostart_enabled_true_when_scheduled_task_exists(monkeypatch):
    monkeypatch.setattr(autostart, "legacy_shortcut_exists", lambda: False)
    monkeypatch.setattr(autostart, "task_exists", lambda: True)
    assert autostart.is_autostart_enabled() is True


def test_is_autostart_enabled_true_for_legacy_shortcut(monkeypatch):
    monkeypatch.setattr(autostart, "legacy_shortcut_exists", lambda: True)
    monkeypatch.setattr(autostart, "task_exists", lambda: False)
    assert autostart.is_autostart_enabled() is True


def test_is_autostart_enabled_false_when_neither_exists(monkeypatch):
    monkeypatch.setattr(autostart, "legacy_shortcut_exists", lambda: False)
    monkeypatch.setattr(autostart, "task_exists", lambda: False)
    assert autostart.is_autostart_enabled() is False


def test_enable_autostart_registers_highest_privilege_logon_task(
        monkeypatch, tmp_path):
    lnk = tmp_path / "CopyTradesMT5.lnk"
    lnk.touch()
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    monkeypatch.setattr(autostart, "task_exists", lambda: True)
    captured = {}

    def fake_run_elevated(script, result_path):
        captured["script"] = script

    monkeypatch.setattr(autostart, "_run_elevated_script", fake_run_elevated)

    autostart.enable_autostart("C:/some path/pythonw.exe", "-m manager")

    script = captured["script"]
    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "-RunLevel Highest" in script
    assert "-LogonType Interactive" in script
    assert "'C:/some path/pythonw.exe'" in script
    assert "'-m manager'" in script
    assert "Register-ScheduledTask -TaskName 'CopyTradesMT5'" in script
    # the legacy Startup shortcut must be gone so only one instance starts
    assert not lnk.exists()


def test_enable_autostart_raises_autostart_error_on_failure(
        monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "CopyTradesMT5.lnk")

    def raising_run_elevated(script, result_path):
        raise AutostartError("UAC cancelled")

    monkeypatch.setattr(autostart, "_run_elevated_script", raising_run_elevated)
    with pytest.raises(AutostartError):
        autostart.enable_autostart("C:/py/pythonw.exe")


def test_run_elevated_script_uses_runas_when_not_elevated(
        monkeypatch, tmp_path):
    result = tmp_path / "result.txt"
    result.write_text("OK", encoding="ascii")
    monkeypatch.setattr(autostart, "is_elevated", lambda: False)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)

    autostart._run_elevated_script("Write-Output 'hi'", result)

    # outer powershell re-launches itself elevated via the RunAs verb
    outer = captured["cmd"][-1]
    assert "-Verb RunAs" in outer
    assert "-EncodedCommand" in outer


def test_run_elevated_script_runs_directly_when_elevated(
        monkeypatch, tmp_path):
    result = tmp_path / "result.txt"
    result.write_text("OK", encoding="ascii")
    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)

    autostart._run_elevated_script("Write-Output 'hi'", result)

    encoded = captured["cmd"][-1]
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    assert decoded == "Write-Output 'hi'"


def test_run_elevated_script_raises_when_uac_cancelled(
        monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "is_elevated", lambda: False)
    monkeypatch.setattr(
        autostart.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    with pytest.raises(AutostartError):
        autostart._run_elevated_script("Write-Output 'hi'",
                                       tmp_path / "result.txt")


def test_run_elevated_script_raises_on_error_result(monkeypatch, tmp_path):
    result = tmp_path / "result.txt"
    result.write_text("ERR:Zugriff verweigert", encoding="ascii")
    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    monkeypatch.setattr(
        autostart.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    with pytest.raises(AutostartError, match="Zugriff verweigert"):
        autostart._run_elevated_script("Write-Output 'hi'", result)


def test_disable_autostart_deletes_task_and_legacy_shortcut(
        monkeypatch, tmp_path):
    lnk = tmp_path / "CopyTradesMT5.lnk"
    lnk.touch()
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    state = {"exists": True}
    monkeypatch.setattr(autostart, "task_exists", lambda: state["exists"])
    calls = []

    def fake_schtasks(args):
        calls.append(args)
        state["exists"] = False
        return 0

    monkeypatch.setattr(autostart, "_run_schtasks", fake_schtasks)

    autostart.disable_autostart()

    assert calls and "/Delete" in calls[0]
    assert not lnk.exists()


def test_disable_autostart_is_idempotent(monkeypatch, tmp_path):
    lnk = tmp_path / "CopyTradesMT5.lnk"
    monkeypatch.setattr(autostart, "startup_lnk_path", lambda: lnk)
    monkeypatch.setattr(autostart, "task_exists", lambda: False)
    # nothing exists -> no raise
    autostart.disable_autostart()
    assert not lnk.exists()


def test_disable_autostart_raises_if_task_cannot_be_removed(
        monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "startup_lnk_path",
                        lambda: tmp_path / "CopyTradesMT5.lnk")
    monkeypatch.setattr(autostart, "task_exists", lambda: True)
    monkeypatch.setattr(autostart, "_run_schtasks", lambda args: 1)

    def raising_run_elevated(script, result_path):
        raise AutostartError("denied")

    monkeypatch.setattr(autostart, "_run_elevated_script", raising_run_elevated)
    with pytest.raises(AutostartError):
        autostart.disable_autostart()
