# manager/platform/autostart.py
"""Windows Startup-folder shortcut management for autostart-on-login.

Creates/removes a .lnk in shell:startup via PowerShell + WScript.Shell COM
(the same technique scripts/install.ps1 uses for the Start Menu shortcut), so
no pywin32 dependency is needed. Qt-free and unit-testable with a mocked
subprocess.run."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class AutostartError(RuntimeError):
    """Raised when the Windows Startup shortcut cannot be created/removed."""


def startup_lnk_path() -> Path:
    """Path to the autostart shortcut. On Windows, the Startup folder under
    %APPDATA%; on other OSes a ~/.config/autostart fallback so the module is
    importable in tests/dev without path explosions (only Windows is a
    supported target per the README)."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home())
        return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "CopyTradesMT5.lnk")
    return Path.home() / ".config" / "autostart" / "CopyTradesMT5.lnk"


def is_autostart_enabled() -> bool:
    """True iff the Startup .lnk currently exists (the source of truth for the
    boot checkbox state)."""
    return startup_lnk_path().exists()


def _ps_quote(s: str) -> str:
    """Single-quote a string for a PowerShell -Command argument. A literal
    single quote is escaped by doubling it."""
    return "'" + s.replace("'", "''") + "'"


def enable_autostart(target_exe: str, arguments: str = "-m manager",
                     working_dir: str | None = None) -> None:
    """Create the Windows Startup .lnk pointing at target_exe (with arguments)
    via PowerShell + WScript.Shell COM. Raises AutostartError on non-zero exit
    or if powershell is missing, so the GUI can log and revert the toggle."""
    lnk = startup_lnk_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$sc = $ws.CreateShortcut({_ps_quote(str(lnk))})",
        f"$sc.TargetPath = {_ps_quote(target_exe)}",
        f"$sc.Arguments = {_ps_quote(arguments)}",
    ]
    if working_dir is not None:
        lines.append(f"$sc.WorkingDirectory = {_ps_quote(working_dir)}")
    lines.append("$sc.Description = 'CopyTrades MT5 Local Manager'")
    lines.append("$sc.Save()")
    script = "; ".join(lines)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise AutostartError(f"failed to create Startup shortcut: {exc}") from exc


def disable_autostart() -> None:
    """Delete the Startup .lnk if present (idempotent; missing file is a
    no-op)."""
    lnk = startup_lnk_path()
    try:
        lnk.unlink()
    except FileNotFoundError:
        pass