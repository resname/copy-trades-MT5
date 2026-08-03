# manager/tests/test_updater.py
import subprocess
from unittest.mock import MagicMock

import pytest

from manager import updater
from manager.updater import UpdateInfo, parse_version, check_for_update, apply_update_and_restart


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


def test_apply_update_and_restart_spawns_and_quits(monkeypatch):
    calls = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    quit_called = []
    apply_update_and_restart(on_quit=lambda: quit_called.append(True))
    assert len(calls) == 1
    _args, kwargs = calls[0]
    cmd = kwargs.get("args") or (_args[0] if _args else None)
    assert cmd is not None
    assert any("install.ps1" in str(part) for part in cmd)
    assert quit_called == [True]