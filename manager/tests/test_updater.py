# manager/tests/test_updater.py
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from manager import updater
from manager.updater import parse_version, check_for_update


def test_parse_version_numeric_not_lex():
    assert parse_version("0.1.10") > parse_version("0.1.9")
    assert parse_version("0.1.5") == parse_version("0.1.5")


def test_parse_version_drops_suffix():
    assert parse_version("0.1.0.dev0") == (0, 1, 0)
    assert parse_version("0.1.42") == (0, 1, 42)


def test_current_version_reads_module():
    from manager._version import __version__
    assert updater.current_version() == __version__


def test_check_for_update_available(monkeypatch):
    monkeypatch.setattr(updater, "_fetch_text", lambda url, t: "0.1.99")
    info = check_for_update()
    assert info.available is True
    assert info.latest == "0.1.99"
    assert info.current == updater.current_version()


def test_check_for_update_same_version(monkeypatch):
    monkeypatch.setattr(updater, "_fetch_text",
                        lambda url, t: updater.current_version())
    info = check_for_update()
    assert info.available is False


def test_check_for_update_network_failure(monkeypatch):
    monkeypatch.setattr(updater, "_fetch_text", lambda url, t: None)
    info = check_for_update()
    assert info.available is False
    assert info.latest is None


def test_apply_update_and_restart_spawns_helper_with_wheel_and_pid(monkeypatch):
    from manager import updater
    captured = []
    monkeypatch.setattr(updater, "cached_update", lambda: None)
    monkeypatch.setattr(updater, "download_update",
                        lambda dest_dir=None: Path("C:/cached/manager-latest.whl"))
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **k: captured.append((cmd, k)) or MagicMock())
    monkeypatch.setattr(updater, "_helper_exe", lambda: "C:/venv/pythonw.exe")
    quit_called = []
    updater.apply_update_and_restart(on_quit=lambda: quit_called.append(True))
    assert len(captured) == 1
    cmd, kwargs = captured[0]
    assert cmd[0] == "C:/venv/pythonw.exe"
    assert "-m" in cmd and "manager.update_helper" in cmd
    assert str(Path("C:/cached/manager-latest.whl")) in cmd
    assert str(updater.os.getpid()) in cmd
    assert quit_called == [True]


def test_apply_update_and_restart_uses_cached_wheel(monkeypatch):
    from manager import updater
    captured = []
    monkeypatch.setattr(updater, "cached_update",
                        lambda: Path("C:/pre/manager-latest.whl"))
    download_calls = []
    monkeypatch.setattr(updater, "download_update",
                        lambda dest_dir=None: download_calls.append(1) or Path("x"))
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **k: captured.append(cmd) or MagicMock())
    monkeypatch.setattr(updater, "_helper_exe", lambda: "pyw")
    updater.apply_update_and_restart(on_quit=lambda: None)
    assert download_calls == []  # cache hit -> no download
    assert str(Path("C:/pre/manager-latest.whl")) in captured[0]


def test_apply_update_and_restart_aborts_when_no_wheel_and_download_fails(monkeypatch):
    from manager import updater
    monkeypatch.setattr(updater, "cached_update", lambda: None)
    def boom(dest_dir=None):
        raise updater.UpdateDownloadError("network down")
    monkeypatch.setattr(updater, "download_update", boom)
    spawned = []
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **k: spawned.append(cmd) or MagicMock())
    quit_called = []
    updater.apply_update_and_restart(on_quit=lambda: quit_called.append(True))
    assert spawned == []          # did not spawn the helper
    assert quit_called == []       # did NOT quit the app


def test_download_update_verifies_sha_and_returns_path(tmp_path, monkeypatch):
    import hashlib
    from manager import updater
    data = b"wheel-bytes"

    def fake_urlretrieve(url, filename=None, *a, **k):
        if str(url).endswith(".whl"):
            Path(filename).write_bytes(data)
        else:
            Path(filename).write_text(
                hashlib.sha256(data).hexdigest() + "  manager-latest.whl",
                encoding="utf-8")
        return filename, {}

    monkeypatch.setattr(updater.urllib.request, "urlretrieve", fake_urlretrieve)
    p = updater.download_update(dest_dir=tmp_path)
    assert p.read_bytes() == data


def test_download_update_raises_on_checksum_mismatch(tmp_path, monkeypatch):
    from manager import updater

    def fake_urlretrieve(url, filename=None, *a, **k):
        if str(url).endswith(".whl"):
            Path(filename).write_bytes(b"not-the-real-wheel")
        else:
            Path(filename).write_text("0000000000000000000000000000000000000000000000000000000000000000  manager-latest.whl",
                                      encoding="utf-8")
        return filename, {}

    monkeypatch.setattr(updater.urllib.request, "urlretrieve", fake_urlretrieve)
    with pytest.raises(updater.UpdateDownloadError):
        updater.download_update(dest_dir=tmp_path)
    # mismatched files are removed
    assert not (tmp_path / "manager-latest.whl").exists()


def test_cached_update_returns_none_when_absent(tmp_path, monkeypatch):
    from manager import updater
    monkeypatch.setattr(updater, "UPDATE_DIR", tmp_path)
    assert updater.cached_update() is None


def test_cached_update_returns_path_when_verified(tmp_path, monkeypatch):
    import hashlib
    from manager import updater
    data = b"x" * 64
    (tmp_path / "manager-latest.whl").write_bytes(data)
    (tmp_path / "manager-latest.whl.sha256").write_text(
        hashlib.sha256(data).hexdigest() + "  manager-latest.whl", encoding="utf-8")
    monkeypatch.setattr(updater, "UPDATE_DIR", tmp_path)
    assert updater.cached_update() == tmp_path / "manager-latest.whl"


def test_cached_update_deletes_stale_pair(tmp_path, monkeypatch):
    from manager import updater
    (tmp_path / "manager-latest.whl").write_bytes(b"stale")
    (tmp_path / "manager-latest.whl.sha256").write_text(
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  manager-latest.whl",
        encoding="utf-8")
    monkeypatch.setattr(updater, "UPDATE_DIR", tmp_path)
    assert updater.cached_update() is None
    assert not (tmp_path / "manager-latest.whl").exists()
    assert not (tmp_path / "manager-latest.whl.sha256").exists()