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

import hashlib
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = "resname/copy-trades-MT5"
BASE = f"https://github.com/{REPO}/releases/latest/download"
INSTALL_PS1_URL = f"{BASE}/install.ps1"
VERSION_URL = f"{BASE}/version.txt"
WHEEL_URL = f"{BASE}/manager-latest.whl"
WHEEL_SHA_URL = f"{BASE}/manager-latest.whl.sha256"

# Local cache for the pre-downloaded, SHA256-verified wheel. Lives in the
# install tree (%LOCALAPPDATA%), separate from the settings file (%APPDATA%),
# so an update reinstalling the wheel never wipes the user's config.
UPDATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) \
             / "CopyTradesMT5" / "updates"

# Windows detached-spawn flags shared by apply_update_and_restart (spawning
# the helper). CREATE_NO_WINDOW (0x08000000) + CREATE_NEW_PROCESS_GROUP
# (0x00000200) decouple the child so it survives the manager quitting.
_DETACHED_FLAGS = 0x08000000 | 0x00000200


class UpdateDownloadError(Exception):
    """Raised when the update wheel cannot be downloaded or its SHA256
    checksum does not match the published .sha256."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

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


def download_update(dest_dir: Path | None = None) -> Path:
    """Download WHEEL_URL + WHEEL_SHA_URL into dest_dir (default UPDATE_DIR),
    verify SHA256, and return the verified wheel path. Raises
    UpdateDownloadError on a network failure or checksum mismatch (the
    mismatched files are removed)."""
    dest = dest_dir if dest_dir is not None else UPDATE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    wheel_path = dest / "manager-latest.whl"
    sha_path = dest / "manager-latest.whl.sha256"
    try:
        urllib.request.urlretrieve(WHEEL_URL, str(wheel_path))
        urllib.request.urlretrieve(WHEEL_SHA_URL, str(sha_path))
    except Exception as exc:
        raise UpdateDownloadError(f"download failed: {exc}") from exc
    expected = sha_path.read_text(encoding="utf-8").strip().split()[0].lower()
    actual = _sha256_file(wheel_path).lower()
    if actual != expected:
        for p in (wheel_path, sha_path):
            try:
                p.unlink()
            except OSError:
                pass
        raise UpdateDownloadError(
            f"checksum mismatch (expected {expected} got {actual})")
    return wheel_path


def cached_update() -> Path | None:
    """Return the path of the cached + SHA256-verified wheel, or None if no
    usable cache exists. A stale/mismatched pair is deleted so the next check
    can re-download."""
    wheel = UPDATE_DIR / "manager-latest.whl"
    sha = UPDATE_DIR / "manager-latest.whl.sha256"
    if not wheel.exists() or not sha.exists():
        return None
    expected = sha.read_text(encoding="utf-8").strip().split()[0].lower()
    if _sha256_file(wheel).lower() != expected:
        for p in (wheel, sha):
            try:
                p.unlink()
            except OSError:
                pass
        return None
    return wheel


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