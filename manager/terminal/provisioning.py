# manager/terminal/provisioning.py
from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path


SETUP_DOWNLOAD_URL = "https://www.metatrader5.com/en/download"
"""Official MT5 download page. mt5setup.exe is a web installer (it downloads
most components from MetaQuotes' CDN at install time), so provisioning needs
internet reachability. Installing to a user-writable path under
%LOCALAPPDATA% avoids UAC elevation."""


class ProvisioningError(Exception):
    """Raised when a terminal instance could not be installed: the installer
    exited non-zero, the terminal64.exe never appeared within the timeout, or
    the setup bootstrapper could not be downloaded."""


def _default_root() -> str:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return str(Path(local) / "CopyTradesMT5")


def instance_install_dir(index: int, root: str | None = None) -> str:
    """The per-instance install dir: ``<root>/terminals/instance_<index>``.
    Default root is %LOCALAPPDATA%/CopyTradesMT5 (user-writable, no UAC)."""
    base = root if root is not None else _default_root()
    return str(Path(base) / "terminals" / f"instance_{index}")


def provision_command(setup_path: str, install_dir: str) -> list[str]:
    """The argv for an unattended custom-path install:
    ``mt5setup.exe /auto /path:"<install_dir>"``. ``/auto`` suppresses the
    settings UI; ``/path:`` overrides the install dir. Pure function so the
    exact flags are testable without running anything."""
    return [setup_path, "/auto", f"/path:{install_dir}"]


def _default_runner(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def _default_waiter(install_dir: str, poll_interval: float, timeout: float) -> bool:
    """Poll for terminal64.exe to appear. The installer's completion signal
    is undocumented (it spawns child processes), so we both wait on the
    process exit (in provision_instance) and poll the filesystem here."""
    exe = Path(install_dir) / "terminal64.exe"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if exe.is_file():
            return True
        time.sleep(poll_interval)
    return exe.is_file()


def provision_instance(index: int, setup_path: str,
                       install_root: str | None = None,
                       runner=None, waiter=None,
                       poll_interval: float = 1.0, timeout: float = 300.0) -> str:
    """Install one MT5 instance to ``instance_install_dir(index, install_root)``
    using ``mt5setup.exe /auto /path:<dir>``. Returns the install dir on
    success. Raises ProvisioningError on installer non-zero exit or on the
    terminal64.exe never appearing within ``timeout`` seconds. ``runner`` is
    subprocess.run by default; ``waiter`` is a (install_dir, poll_interval,
    timeout) -> bool poller. Both injectable so tests never run a real
    installer."""
    install_dir = instance_install_dir(index, root=install_root)
    cmd = provision_command(setup_path, install_dir)
    run = runner if runner is not None else _default_runner
    result = run(cmd)
    if getattr(result, "returncode", 0) != 0:
        raise ProvisioningError(
            f"mt5setup.exe exited {getattr(result, 'returncode', '?')} for "
            f"{install_dir}")
    wait = waiter if waiter is not None else _default_waiter
    if not wait(install_dir, poll_interval, timeout):
        raise ProvisioningError(
            f"terminal64.exe did not appear at {install_dir} within {timeout}s")
    return install_dir


def _default_downloader(src_url: str, dest_path: str) -> None:
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(src_url, dest_path)


def download_setup(cache_path: str | None = None, downloader=None) -> str:
    """Fetch the mt5setup.exe bootstrapper to a cache path. Default cache is
    %LOCALAPPDATA%/CopyTradesMT5/mt5setup.exe. ``downloader(src_url, dest_path)``
    is injectable so tests never hit the network. Returns the cache path."""
    dest = cache_path or str(Path(_default_root()) / "mt5setup.exe")
    dl = downloader if downloader is not None else _default_downloader
    try:
        dl(SETUP_DOWNLOAD_URL, dest)
    except Exception as exc:
        raise ProvisioningError(f"could not download mt5setup.exe: {exc}") from exc
    return dest