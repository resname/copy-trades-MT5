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
