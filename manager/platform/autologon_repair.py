"""Qt-free self-healing for the Windows unattended-boot setup.

The v0.1.34 setup wrote ``ForceAutoLogon=1`` and a ``CopyTradesMT5-Lock`` task
that locked RDP sessions. ``ForceAutoLogon=1`` re-logs in after every logoff
and can repeatedly kick RDP sessions on single-session Windows clients. This
module detects that legacy state and repairs it:

- ``ForceAutoLogon`` -> ``0``
- Winlogon domain corrected to the computer name for the local account
- the lock task replaced with a console-only helper
  (``GetSystemMetrics(SM_REMOTESESSION)``), or removed when the
  ``no-console-lock`` marker exists

The repair never asks for or changes the Windows password. On startup it runs
silently when the process is already elevated (the normal case for a rig
started by the elevated logon task); otherwise the GUI offers a one-click
``Repair now (UAC)`` action.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from manager.platform import autostart
from manager.platform.autostart import _ps_quote, _run_elevated_script

TASK_NAME = "CopyTradesMT5-Lock"
HELPER_NAME = "lock-console-session.ps1"
WINLOGON_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
SETUP_URL = ("https://github.com/resname/copy-trades-MT5/releases/latest/"
             "download/enable-unattended-autologin.ps1")

_HELPER_CODE = r"""Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class Desk { [DllImport("user32.dll")] public static extern int GetSystemMetrics(int nIndex); }'
$SM_REMOTESESSION = 0x1000
if ([Desk]::GetSystemMetrics($SM_REMOTESESSION) -eq 0) {
  rundll32.exe user32.dll,LockWorkStation
}"""


class AutologonRepairError(RuntimeError):
    """Raised when the repair or the in-app setup cannot be executed."""


@dataclass(frozen=True)
class RepairResult:
    changed: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    needs_elevation: bool = False
    error: str | None = None


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))


def _current_user() -> str:
    return os.environ.get("USERNAME", "")


def _computer_name() -> str:
    return os.environ.get("COMPUTERNAME", "")


def _no_lock_marker() -> Path:
    return _local_appdata() / "CopyTradesMT5" / "no-console-lock"


def _read_winlogon() -> dict[str, str]:
    """Read the Winlogon values we care about. Returns {} when the registry is
    unavailable (non-Windows/dev), so callers treat the machine as clean."""
    if os.name != "nt":
        return {}
    import winreg

    names = ("AutoAdminLogon", "DefaultUserName", "DefaultDomainName",
             "DefaultPassword", "ForceAutoLogon")
    values: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINLOGON_KEY) as key:
            for name in names:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                values[name] = str(value)
    except OSError:
        return {}
    return values


def _lock_task_xml() -> str | None:
    """XML of the CopyTradesMT5-Lock task, or None when it is absent."""
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
            capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _reasons() -> tuple[str, ...]:
    values = _read_winlogon()
    if not values:
        return ()

    reasons: list[str] = []
    auto = values.get("AutoAdminLogon") == "1"
    if values.get("ForceAutoLogon") == "1":
        reasons.append("ForceAutoLogon=1 can kick RDP sessions")

    if auto:
        user = _current_user()
        computer = _computer_name()
        if values.get("DefaultUserName") == user and computer:
            domain = values.get("DefaultDomainName", "")
            if domain.lower() != computer.lower():
                reasons.append(f"DefaultDomainName={domain!r} != {computer!r}")
        xml = _lock_task_xml()
        if xml is not None:
            if HELPER_NAME not in xml:
                reasons.append("CopyTradesMT5-Lock locks RDP sessions")
        elif not _no_lock_marker().exists():
            reasons.append(
                "CopyTradesMT5-Lock missing while auto-login is enabled")

    if autostart.legacy_shortcut_exists() and not autostart.task_exists():
        reasons.append("legacy Startup shortcut (not elevated)")
    return tuple(reasons)


def assess() -> tuple[str, ...]:
    """Return the legacy-state reasons; empty means nothing to repair."""
    return _reasons()


def setup_configured() -> bool:
    """True when auto-login has a stored password (nothing left to set up)."""
    values = _read_winlogon()
    return (values.get("AutoAdminLogon") == "1"
            and "DefaultPassword" in values)


def _build_repair_script(result_path: Path) -> str:
    """PowerShell that applies the repair idempotently and reports its outcome
    as 'OK' or 'ERR:<message>' in ``result_path``."""
    result = _ps_quote(str(result_path))
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  $winlogon = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'",
        "  Set-ItemProperty -LiteralPath $winlogon -Name ForceAutoLogon "
        "-Value '0' -Type String",
        "  $p = Get-ItemProperty -LiteralPath $winlogon",
        "  if ($p.AutoAdminLogon -eq '1') {",
        "    if ($p.DefaultUserName -eq $env:USERNAME -and "
        "$p.DefaultDomainName -ne $env:COMPUTERNAME) {",
        "      Set-ItemProperty -LiteralPath $winlogon -Name DefaultDomainName "
        "-Value $env:COMPUTERNAME -Type String",
        "    }",
        "    $marker = Join-Path $env:LOCALAPPDATA "
        "'CopyTradesMT5\\no-console-lock'",
        "    if (Test-Path -LiteralPath $marker) {",
        f"      Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
        "-Confirm:$false -ErrorAction SilentlyContinue",
        "    } else {",
        "      $dir = Join-Path $env:LOCALAPPDATA 'CopyTradesMT5\\bin'",
        "      New-Item -ItemType Directory -Force -Path $dir | Out-Null",
        f"      $helper = Join-Path $dir '{HELPER_NAME}'",
        "      $helperCode = @'",
        _HELPER_CODE,
        "'@",
        "      Set-Content -LiteralPath $helper -Value $helperCode "
        "-Encoding ASCII",
        "      $u = \"$env:COMPUTERNAME\\$env:USERNAME\"",
        "      $action = New-ScheduledTaskAction -Execute "
        "\"$env:SystemRoot\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" "
        "-Argument \"-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass "
        "-File `\"$helper`\"\"",
        "      $trigger = New-ScheduledTaskTrigger -AtLogOn -User $u",
        "      try { $trigger.Delay = 'PT5S' } catch {}",
        "      $principal = New-ScheduledTaskPrincipal -UserId $u "
        "-LogonType Interactive -RunLevel Limited",
        "      $settings = New-ScheduledTaskSettingsSet "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-MultipleInstances IgnoreNew",
        "      $task = New-ScheduledTask -Action $action -Trigger $trigger "
        "-Principal $principal -Settings $settings",
        f"      Register-ScheduledTask -TaskName '{TASK_NAME}' "
        "-InputObject $task -Force | Out-Null",
        "    }",
        "  }",
        f"  Set-Content -LiteralPath {result} -Value 'OK' -Encoding ASCII",
        "} catch {",
        f"  Set-Content -LiteralPath {result} -Value "
        "('ERR:' + $_.Exception.Message) -Encoding ASCII",
        "  exit 1",
        "}",
    ]
    return "\n".join(lines)


def _result_file() -> Path:
    fd, name = tempfile.mkstemp(prefix="copytrades-repair-", suffix=".txt")
    os.close(fd)
    return Path(name)


def _apply(reasons: tuple[str, ...]) -> tuple[str, ...]:
    result = _result_file()
    try:
        _run_elevated_script(_build_repair_script(result), result)
    finally:
        try:
            result.unlink()
        except OSError:
            pass

    # The legacy Startup shortcut migration is the app's own task, not part of
    # the autologin config. Only do it here when already elevated; otherwise the
    # existing Auto-start migration banner handles it with its own UAC prompt.
    if (autostart.is_elevated() and autostart.legacy_shortcut_exists()
            and not autostart.task_exists()):
        autostart.enable_autostart(sys.executable, "-m manager")
    return reasons


def repair_on_startup() -> RepairResult:
    """Called once at app startup (including the update relaunch).

    Silent repair when elevated; otherwise the GUI shows the repair banner.
    """
    reasons = assess()
    if not reasons:
        return RepairResult()
    if not autostart.is_elevated():
        return RepairResult(reasons=reasons, needs_elevation=True)
    try:
        return RepairResult(changed=_apply(reasons), reasons=reasons)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the GUI
        return RepairResult(reasons=reasons, error=str(exc))


def repair_now() -> RepairResult:
    """GUI button: repair now, elevating via UAC when necessary."""
    reasons = assess()
    if not reasons:
        return RepairResult()
    try:
        return RepairResult(changed=_apply(reasons), reasons=reasons)
    except Exception as exc:  # noqa: BLE001
        return RepairResult(reasons=reasons, error=str(exc))


def download_setup_script(dest_dir: Path | None = None) -> Path:
    """Download the latest release's enable-unattended-autologin.ps1."""
    dest = Path(dest_dir) if dest_dir is not None \
        else _local_appdata() / "CopyTradesMT5" / "updates"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "enable-unattended-autologin.ps1"
    try:
        urllib.request.urlretrieve(SETUP_URL, str(path))
    except Exception as exc:  # noqa: BLE001
        raise AutologonRepairError(f"download failed: {exc}") from exc
    return path


def launch_setup_script(path: Path) -> None:
    """Run the setup script elevated. It prompts for the Windows password in
    its own secure console window; this process never sees it."""
    quoted = _ps_quote(str(path))
    outer = (
        "$ErrorActionPreference = 'Stop'; "
        "Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait "
        "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"
        f"{quoted}")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", outer],
            capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        raise AutologonRepairError(
            f"failed to launch the setup script: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise AutologonRepairError(
            "setup cancelled or failed" + (f": {detail}" if detail else ""))
