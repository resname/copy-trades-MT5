import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT = SCRIPTS / "enable-unattended-autologin.ps1"
FIX_SCRIPT = SCRIPTS / "fix-unattended-autologin.ps1"


def _assert_ps_parses(path: Path) -> None:
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


def _extract_lock_helper(text: str) -> str:
    match = re.search(r"\$lockHelperCode = @'\r?\n(.*?)\r?\n'@", text,
                      re.DOTALL)
    assert match, "lock helper block not found"
    return match.group(1)


def test_script_exists():
    assert SCRIPT.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_script_parses_without_syntax_errors():
    _assert_ps_parses(SCRIPT)


def test_script_configures_autologin_and_lock_task():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "AutoAdminLogon" in text
    assert "ForceAutoLogon" in text
    assert "DefaultPassword" in text
    assert "DisableAutomaticRestartSignOn" in text
    assert "LockWorkStation" in text
    assert "CopyTradesMT5-Lock" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "no-console-lock" in text


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


def test_fix_script_exists():
    assert FIX_SCRIPT.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_fix_script_parses_without_syntax_errors():
    _assert_ps_parses(FIX_SCRIPT)


def test_fix_script_fixes_forceautologon_and_console_lock():
    text = FIX_SCRIPT.read_text(encoding="utf-8")
    assert "ForceAutoLogon" in text
    assert "SM_REMOTESESSION" in text
    assert "lock-console-session.ps1" in text
    assert "Register-ScheduledTask" in text
    assert "DefaultDomainName" in text
    assert "RemoveLockTask" in text
    assert "no-console-lock" in text
    # The repair must never change the account password.
    assert "Set-LocalUser" not in text
    for line in text.splitlines():
        if "DefaultPassword" in line:
            assert "Set-ItemProperty" not in line


@pytest.mark.skipif(os.name != "nt", reason="Windows-only")
def test_fix_script_generates_console_only_helper(tmp_path):
    text = FIX_SCRIPT.read_text(encoding="utf-8")
    helper = _extract_lock_helper(text)
    assert "GetSystemMetrics" in helper
    assert "LockWorkStation" in helper
    path = tmp_path / "lock-console-session.ps1"
    path.write_text(helper, encoding="utf-8")
    _assert_ps_parses(path)
