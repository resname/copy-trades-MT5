# manager/tests/test_terminal_provisioning.py
import os
from pathlib import Path

import pytest

from manager.terminal.provisioning import (
    SETUP_DOWNLOAD_URL, provision_command, instance_install_dir,
    provision_instance, download_setup, ProvisioningError,
)


def test_provision_command_uses_auto_and_path_flags():
    cmd = provision_command(r"C:\setup\mt5setup.exe", r"C:\inst\instance_0")
    assert cmd == [r"C:\setup\mt5setup.exe", "/auto",
                   r"/path:C:\inst\instance_0"]


def test_instance_install_dir_default_root(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\s\AppData\Local")
    p = instance_install_dir(3)
    assert p == r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_3"


def test_instance_install_dir_custom_root():
    p = instance_install_dir(7, root=r"D:\terminals")
    assert p == r"D:\terminals\terminals\instance_7" or \
           p == str(Path(r"D:\terminals") / "terminals" / "instance_7")


def test_provision_instance_runs_installer_and_waits_for_exe(tmp_path):
    install_root = tmp_path
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(list(cmd))
        # simulate the installer creating terminal64.exe
        install_dir = cmd[cmd.index([a for a in cmd if a.startswith("/path:")][0])][-1] \
            if False else cmd[2][len("/path:"):]
        Path(install_dir).mkdir(parents=True, exist_ok=True)
        (Path(install_dir) / "terminal64.exe").write_bytes(b"")
        class _R:
            returncode = 0
        return _R()

    def fake_waiter(install_dir, poll_interval, timeout):
        # exe already created by runner above
        return Path(install_dir, "terminal64.exe").is_file()

    out = provision_instance(0, setup_path=r"C:\mt5setup.exe",
                             install_root=str(install_root),
                             runner=fake_runner, waiter=fake_waiter)
    assert out == instance_install_dir(0, root=str(install_root))
    assert calls[0] == [r"C:\mt5setup.exe", "/auto",
                        "/path:" + out]
    assert (Path(out) / "terminal64.exe").is_file()


def test_provision_instance_nonzero_exit_raises(tmp_path):
    def fake_runner(cmd, **kwargs):
        class _R:
            returncode = 1
        return _R()

    def fake_waiter(install_dir, poll_interval, timeout):
        return True

    with pytest.raises(ProvisioningError):
        provision_instance(0, setup_path=r"C:\mt5setup.exe",
                          install_root=str(tmp_path),
                          runner=fake_runner, waiter=fake_waiter)


def test_provision_instance_waiter_timeout_raises(tmp_path):
    def fake_runner(cmd, **kwargs):
        class _R:
            returncode = 0
        return _R()

    def fake_waiter(install_dir, poll_interval, timeout):
        return False  # never appears

    with pytest.raises(ProvisioningError):
        provision_instance(0, setup_path=r"C:\mt5setup.exe",
                          install_root=str(tmp_path),
                          runner=fake_runner, waiter=fake_waiter,
                          timeout=0.01)


def test_download_setup_uses_injected_downloader(tmp_path):
    cache = tmp_path / "mt5setup.exe"
    fetched = []

    def fake_downloader(src_url, dest_path):
        fetched.append((src_url, dest_path))
        Path(dest_path).write_bytes(b"installer bytes")

    out = download_setup(cache_path=str(cache), downloader=fake_downloader)
    assert out == str(cache)
    assert fetched[0][0] == SETUP_DOWNLOAD_URL
    assert fetched[0][1] == str(cache)
    assert Path(out).read_bytes() == b"installer bytes"


def test_download_setup_default_cache_path(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\s\AppData\Local")
    fetched = []

    def fake_downloader(src_url, dest_path):
        fetched.append(dest_path)
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"x")

    out = download_setup(downloader=fake_downloader)
    assert out == r"C:\Users\s\AppData\Local\CopyTradesMT5\mt5setup.exe"