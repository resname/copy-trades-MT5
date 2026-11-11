# manager/platform/autostart.py
"""Windows autostart via an elevated Scheduled Task.

The app must start at logon **with administrative rights** so it can launch
the MT5 terminals in the same elevated context. A Startup-folder .lnk starts
non-elevated (and Windows never elevates it silently), so the supported
mechanism is a Scheduled Task with ``RunLevel Highest``. Registering such a
task requires elevation; the first enable therefore shows one UAC consent
prompt, after which every logon starts the app elevated without prompting.

Legacy installs used ``<Startup>/CopyTradesMT5.lnk``. That shortcut is treated
as "enabled" for backward compatibility and is removed once the scheduled task
exists, so only one manager instance starts.
"""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import tempfile
from pathlib import Path

TASK_NAME = "CopyTradesMT5"


class AutostartError(RuntimeError):
    """Raised when the scheduled task cannot be created/removed."""


def startup_lnk_path() -> Path:
    """Legacy autostart shortcut path (pre-scheduled-task installs). On
    Windows, the Startup folder under %APPDATA%; on other OSes a
    ~/.config/autostart fallback so the module is importable in tests/dev
    without path explosions (only Windows is a supported target)."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home())
        return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "CopyTradesMT5.lnk")
    return Path.home() / ".config" / "autostart" / "CopyTradesMT5.lnk"


def legacy_shortcut_exists() -> bool:
    """True iff the old Startup-folder .lnk is present."""
    return startup_lnk_path().exists()


def _run_schtasks(args: list[str]) -> int:
    """Run schtasks and return its exit code (1 when the tool is missing)."""
    try:
        proc = subprocess.run(["schtasks", *args], capture_output=True,
                              text=True)
    except (FileNotFoundError, OSError):
        return 1
    return proc.returncode


def task_exists() -> bool:
    """True iff the elevated logon task is registered."""
    if os.name != "nt":
        return False
    return _run_schtasks(["/Query", "/TN", TASK_NAME]) == 0


def is_autostart_enabled() -> bool:
    """The boot checkbox's source of truth: the scheduled task (new) or the
    legacy Startup shortcut (old installs)."""
    return legacy_shortcut_exists() or task_exists()


def is_elevated() -> bool:
    """True iff this process already runs with an elevated token."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ps_quote(s: str) -> str:
    """Single-quote a string for a PowerShell -Command argument. A literal
    single quote is escaped by doubling it."""
    return "'" + s.replace("'", "''") + "'"


def _build_task_registration_script(target_exe: str,
                                    arguments: str = "-m manager",
                                    working_dir: str | None = None,
                                    result_path: Path | None = None) -> str:
    """PowerShell that registers the elevated AtLogOn task and reports its
    outcome in ``result_path`` ('OK' or 'ERR:<message>'). Built as a plain
    string so tests can assert on it without touching the OS."""
    result = _ps_quote(str(result_path))
    action = (f"  $action = New-ScheduledTaskAction -Execute "
              f"{_ps_quote(target_exe)} -Argument {_ps_quote(arguments)}")
    if working_dir is not None:
        action += f" -WorkingDirectory {_ps_quote(working_dir)}"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "try {",
        '  $user = "$env:USERDOMAIN\\$env:USERNAME"',
        action,
        "  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
        "  $principal = New-ScheduledTaskPrincipal -UserId $user "
        "-LogonType Interactive -RunLevel Highest",
        "  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-MultipleInstances IgnoreNew",
        "  $task = New-ScheduledTask -Action $action -Trigger $trigger "
        "-Principal $principal -Settings $settings",
        "  Register-ScheduledTask -TaskName 'CopyTradesMT5' "
        "-InputObject $task -Force | Out-Null",
        f"  Set-Content -LiteralPath {result} -Value 'OK' -Encoding ASCII",
        "} catch {",
        f"  Set-Content -LiteralPath {result} -Value "
        "('ERR:' + $_.Exception.Message) -Encoding ASCII",
        "  exit 1",
        "}",
    ]
    return "\n".join(lines)


def _build_task_delete_script(result_path: Path) -> str:
    result = _ps_quote(str(result_path))
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "  Unregister-ScheduledTask -TaskName 'CopyTradesMT5' "
        "-Confirm:$false",
        f"  Set-Content -LiteralPath {result} -Value 'OK' -Encoding ASCII",
        "} catch {",
        f"  Set-Content -LiteralPath {result} -Value "
        "('ERR:' + $_.Exception.Message) -Encoding ASCII",
        "  exit 1",
        "}",
    ])


def _run_elevated_script(script: str, result_path: Path) -> None:
    """Run ``script`` in PowerShell. If this process is not elevated, re-launch
    PowerShell via the UAC RunAs verb (one consent prompt) and wait for it.
    The script writes its outcome to ``result_path``; non-zero exit (e.g. the
    user cancelled UAC) or an ERR result raises AutostartError."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    if is_elevated():
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-EncodedCommand", encoded]
    else:
        outer = ("$ErrorActionPreference = 'Stop'; "
                 "Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait "
                 "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',"
                 f"'-EncodedCommand','{encoded}'")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-Command", outer]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        raise AutostartError(f"failed to run powershell: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise AutostartError(
            "elevation cancelled or task setup failed"
            + (f": {detail}" if detail else ""))
    try:
        text = result_path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        text = ""
    if text.startswith("ERR:"):
        raise AutostartError(text[4:].strip())
    if text != "OK":
        raise AutostartError("scheduled-task setup produced no result")


def _remove_legacy_shortcut() -> None:
    try:
        startup_lnk_path().unlink()
    except FileNotFoundError:
        pass


def _result_file() -> Path:
    fd, name = tempfile.mkstemp(prefix="copytrades-autostart-",
                                suffix=".txt")
    os.close(fd)
    return Path(name)


def enable_autostart(target_exe: str, arguments: str = "-m manager",
                     working_dir: str | None = None) -> None:
    """Register the elevated AtLogOn task (one UAC prompt when not already
    elevated) and remove the legacy Startup shortcut. Raises AutostartError so
    the GUI can log and revert the toggle."""
    result = _result_file()
    try:
        script = _build_task_registration_script(
            target_exe, arguments, working_dir, result)
        _run_elevated_script(script, result)
        if not task_exists():
            raise AutostartError(
                "scheduled task did not appear after registration")
        # Only one instance may start at logon: drop the old non-elevated .lnk.
        _remove_legacy_shortcut()
    finally:
        try:
            result.unlink()
        except OSError:
            pass


def disable_autostart() -> None:
    """Remove the scheduled task and the legacy Startup .lnk (idempotent).
    A direct schtasks delete is tried first; if that is denied (a Highest task
    may need elevation), the delete is retried through UAC."""
    if task_exists():
        if _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"]) != 0:
            result = _result_file()
            try:
                _run_elevated_script(_build_task_delete_script(result), result)
            finally:
                try:
                    result.unlink()
                except OSError:
                    pass
            if task_exists():
                raise AutostartError("scheduled task could not be removed")
    _remove_legacy_shortcut()
