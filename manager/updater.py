"""Update checking + applying for the local manager.

Headless (no Qt). Compares the installed ``manager._version.__version__`` to
the latest release's ``version.txt`` on GitHub Releases. ``apply_update_and_restart``
spawns a detached PowerShell that downloads ``install.ps1`` from GitHub Releases
and runs it with ``-Yes`` (so the newest installer logic always runs, non-interactively)
and then calls ``on_quit`` so the caller can stop the engine and exit. The detached
installer detects any running manager instance, stops it gracefully then force,
waits for exit, reinstalls the latest wheel, and relaunches.
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from dataclasses import dataclass

REPO = "resname/copy-trades-MT5"
BASE = f"https://github.com/{REPO}/releases/latest/download"
INSTALL_PS1_URL = f"{BASE}/install.ps1"
VERSION_URL = f"{BASE}/version.txt"
WHEEL_URL = f"{BASE}/manager-latest.whl"
WHEEL_SHA_URL = f"{BASE}/manager-latest.whl.sha256"

# Process-creation flags for the background installer. CREATE_NO_WINDOW
# (0x08000000) gives the child a console (so ``powershell -Command`` actually
# executes its body) but no visible window. CREATE_NEW_PROCESS_GROUP (0x00000200)
# decouples Ctrl-C so the installer survives the parent quitting.
#
# Do NOT use DETACHED_PROCESS (0x00000008): a console-less ``powershell.exe
# -Command`` exits without running the script body, so the installer never runs
# and the "Update & restart" button silently does nothing (the app quits via
# on_quit but nothing reinstalls/relaunches).
_BG_FLAGS = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


@dataclass
class UpdateInfo:
    available: bool
    current: str
    latest: str | None


def parse_version(s: str) -> tuple[int, ...]:
    """Numeric tuple compare so ``0.1.10 > 0.1.9`` (not lex). Drops non-numeric
    suffixes (``0.1.0.dev0`` -> ``(0, 1, 0)``)."""
    parts: list[int] = []
    for tok in str(s).strip().split("."):
        num = ""
        for ch in tok:
            if ch.isdigit():
                num += ch
            else:
                break
        # Drop tokens with no leading digit (e.g. ``dev0``) so a non-numeric
        # suffix like ``0.1.0.dev0`` collapses to ``(0, 1, 0)`` rather than
        # appending a spurious zero part.
        if num == "":
            continue
        parts.append(int(num))
    return tuple(parts)


def current_version() -> str:
    from manager._version import __version__
    return __version__


def _fetch_text(url: str, timeout: float) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return None


def latest_version(timeout: float = 5.0) -> str | None:
    return _fetch_text(VERSION_URL, timeout)


def check_for_update(timeout: float = 5.0) -> UpdateInfo:
    cur = current_version()
    latest = latest_version(timeout)
    available = False
    if latest is not None:
        try:
            available = parse_version(latest) > parse_version(cur)
        except Exception:
            available = False
    return UpdateInfo(available=available, current=cur, latest=latest)


def apply_update_and_restart(on_quit) -> None:
    """Spawn a detached installer running the latest ``install.ps1`` with
    ``-Yes`` (newest installer logic, non-interactive), then call ``on_quit()``
    so the caller stops the engine and exits. The detached installer detects
    any running manager instance, stops it gracefully then force, waits for
    exit, reinstalls the latest wheel, and relaunches."""
    cmd = ["powershell", "-NoProfile", "-Command",
           f"& ([scriptblock]::Create((irm '{INSTALL_PS1_URL}'))) -Yes"]
    kwargs: dict = {"close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = _BG_FLAGS
    subprocess.Popen(cmd, **kwargs)
    on_quit()