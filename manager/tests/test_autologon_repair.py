import os
import subprocess
from pathlib import Path

import pytest

from manager.platform import autologon_repair as repair
from manager.platform import autostart


def _values(**overrides):
    values = {"AutoAdminLogon": "1", "DefaultUserName": "u",
              "DefaultDomainName": "PC", "DefaultPassword": "x"}
    values.update(overrides)
    return values


@pytest.fixture
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "_read_winlogon", lambda: _values())
    monkeypatch.setattr(
        repair, "_lock_task_xml",
        lambda: "<Actions><Exec><Command>powershell.exe</Command>"
                "<Arguments>lock-console-session.ps1</Arguments></Exec></Actions>")
    monkeypatch.setattr(repair, "_no_lock_marker", lambda: tmp_path / "marker")
    monkeypatch.setattr(repair, "_current_user", lambda: "u")
    monkeypatch.setattr(repair, "_computer_name", lambda: "PC")
    monkeypatch.setattr(autostart, "legacy_shortcut_exists", lambda: False)
    monkeypatch.setattr(autostart, "task_exists", lambda: True)


def test_assess_clean(clean):
    assert repair.assess() == ()


def test_assess_flags_forceautologon(clean, monkeypatch):
    monkeypatch.setattr(repair, "_read_winlogon",
                        lambda: _values(ForceAutoLogon="1"))
    reasons = repair.assess()
    assert any("ForceAutoLogon" in reason for reason in reasons)


def test_assess_flags_old_lock_task(clean, monkeypatch):
    monkeypatch.setattr(
        repair, "_lock_task_xml",
        lambda: "<Actions><Exec><Command>rundll32.exe</Command>"
                "<Arguments>user32.dll,LockWorkStation</Arguments></Exec></Actions>")
    reasons = repair.assess()
    assert any("RDP" in reason for reason in reasons)


def test_assess_flags_missing_lock_task(clean, monkeypatch):
    monkeypatch.setattr(repair, "_lock_task_xml", lambda: None)
    reasons = repair.assess()
    assert any("missing" in reason for reason in reasons)


def test_assess_marker_skips_missing_lock_task(clean, monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.touch()
    monkeypatch.setattr(repair, "_no_lock_marker", lambda: marker)
    monkeypatch.setattr(repair, "_lock_task_xml", lambda: None)
    assert repair.assess() == ()


def test_assess_flags_domain_mismatch(clean, monkeypatch):
    monkeypatch.setattr(repair, "_read_winlogon",
                        lambda: _values(DefaultDomainName="OTHER"))
    reasons = repair.assess()
    assert any("DefaultDomainName" in reason for reason in reasons)


def test_assess_ignores_lock_task_when_auto_login_is_off(clean, monkeypatch):
    monkeypatch.setattr(repair, "_read_winlogon",
                        lambda: _values(AutoAdminLogon="0"))
    monkeypatch.setattr(
        repair, "_lock_task_xml",
        lambda: "<Actions><Exec><Command>rundll32.exe</Command></Exec></Actions>")
    assert repair.assess() == ()


def test_assess_flags_legacy_shortcut(clean, monkeypatch):
    monkeypatch.setattr(autostart, "legacy_shortcut_exists", lambda: True)
    monkeypatch.setattr(autostart, "task_exists", lambda: False)
    reasons = repair.assess()
    assert any("legacy" in reason for reason in reasons)


def test_setup_configured(clean, monkeypatch):
    assert repair.setup_configured() is True
    monkeypatch.setattr(repair, "_read_winlogon",
                        lambda: _values(AutoAdminLogon="0"))
    assert repair.setup_configured() is False
    monkeypatch.setattr(repair, "_read_winlogon",
                        lambda: {"AutoAdminLogon": "1"})
    assert repair.setup_configured() is False


def test_repair_on_startup_is_noop_when_clean(clean, monkeypatch):
    monkeypatch.setattr(repair, "assess", lambda: ())
    result = repair.repair_on_startup()
    assert result.changed == ()
    assert result.needs_elevation is False
    assert result.error is None


def test_repair_on_startup_needs_elevation(clean, monkeypatch):
    monkeypatch.setattr(repair, "assess", lambda: ("ForceAutoLogon=1",))
    monkeypatch.setattr(autostart, "is_elevated", lambda: False)
    called = []
    monkeypatch.setattr(repair, "_run_elevated_script",
                        lambda *a, **k: called.append(1))
    result = repair.repair_on_startup()
    assert result.needs_elevation is True
    assert result.changed == ()
    assert called == []


def test_repair_on_startup_applies_when_elevated(clean, monkeypatch):
    monkeypatch.setattr(repair, "assess", lambda: ("ForceAutoLogon=1",))
    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    captured = {}

    def fake_run(script, result_path):
        captured["script"] = script

    monkeypatch.setattr(repair, "_run_elevated_script", fake_run)
    result = repair.repair_on_startup()
    assert result.needs_elevation is False
    assert result.changed == ("ForceAutoLogon=1",)
    assert "ForceAutoLogon" in captured["script"]
    assert "SM_REMOTESESSION" in captured["script"]


def test_repair_now_uses_the_elevated_path(clean, monkeypatch):
    monkeypatch.setattr(repair, "assess", lambda: ("ForceAutoLogon=1",))
    monkeypatch.setattr(autostart, "is_elevated", lambda: False)
    captured = {}

    def fake_run(script, result_path):
        captured["script"] = script

    monkeypatch.setattr(repair, "_run_elevated_script", fake_run)
    result = repair.repair_now()
    assert result.changed == ("ForceAutoLogon=1",)
    assert "ForceAutoLogon" in captured["script"]


def test_repair_script_never_touches_the_password(tmp_path):
    script = repair._build_repair_script(tmp_path / "result.txt")
    assert "DefaultPassword" not in script
    assert "Set-LocalUser" not in script


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_build_repair_script_parses(tmp_path):
    script = repair._build_repair_script(tmp_path / "result.txt")
    path = tmp_path / "repair.ps1"
    path.write_text(script, encoding="utf-8")
    quoted = "'" + str(path).replace("'", "''") + "'"
    command = (
        "$e=$null;$t=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"{quoted},[ref]$t,[ref]$e);"
        "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_download_setup_script(monkeypatch, tmp_path):
    def fake_urlretrieve(url, filename):
        Path(filename).write_text("# setup", encoding="utf-8")
        return filename, None

    monkeypatch.setattr(repair.urllib.request, "urlretrieve", fake_urlretrieve)
    path = repair.download_setup_script(tmp_path)
    assert path.name == "enable-unattended-autologin.ps1"
    assert path.read_text(encoding="utf-8") == "# setup"


def test_launch_setup_script_uses_runas(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(repair.subprocess, "run", fake_run)
    repair.launch_setup_script(Path("C:/tmp/enable-unattended-autologin.ps1"))
    outer = captured["cmd"][-1]
    assert "-Verb RunAs" in outer
    assert "enable-unattended-autologin.ps1" in outer


def test_launch_setup_script_raises_on_cancel(monkeypatch):
    monkeypatch.setattr(
        repair.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    with pytest.raises(repair.AutologonRepairError):
        repair.launch_setup_script(Path("C:/tmp/enable-unattended-autologin.ps1"))
