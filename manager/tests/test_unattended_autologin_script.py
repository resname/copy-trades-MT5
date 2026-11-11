import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "scripts"
          / "enable-unattended-autologin.ps1")


def test_script_exists():
    assert SCRIPT.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_script_parses_without_syntax_errors():
    quoted = "'" + str(SCRIPT).replace("'", "''") + "'"
    command = (
        "$e=$null;$t=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"{quoted},[ref]$t,[ref]$e);"
        "if($e.Count){$e|ForEach-Object{$_.Message};exit 1}"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_script_configures_autologin_and_lock_task():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "AutoAdminLogon" in text
    assert "ForceAutoLogon" in text
    assert "DefaultPassword" in text
    assert "DisableAutomaticRestartSignOn" in text
    assert "LockWorkStation" in text
    assert "CopyTradesMT5-Lock" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text


def test_forceautologon_is_opt_in_and_lock_is_console_only():
    text = SCRIPT.read_text(encoding="utf-8")
    # ForceAutoLogon re-logs in after every logoff and can kick RDP sessions:
    # it must be opt-in, defaulting to 0.
    assert "[switch]$ForceAutoLogon" in text
    assert "if ($ForceAutoLogon) { '1' } else { '0' }" in text
    # The lock task must skip remote (RDP) sessions and lock only the console.
    assert "GetSystemMetrics" in text
    assert "SM_REMOTESESSION" in text
    assert "lock-console-session.ps1" in text
    # The script configures the LOCAL account; its domain is the computer name,
    # not $env:USERDOMAIN (which can be a domain on domain-joined machines).
    assert "$domainName = $env:COMPUTERNAME" in text
