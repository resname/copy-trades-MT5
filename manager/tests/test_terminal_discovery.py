# manager/tests/test_terminal_discovery.py
from pathlib import Path

from manager.terminal.discovery import TerminalInstance, discover_terminals


def _make_origin(appdata: Path, hash_id: str, install_dir: str) -> None:
    """Write a UTF-16 origin.txt whose first line is the install dir."""
    folder = appdata / "MetaQuotes" / "Terminal" / hash_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "origin.txt").write_text(install_dir + "\n", encoding="utf-16")


def _make_exe(install_dir: Path) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "terminal64.exe").write_bytes(b"")


def test_discover_reads_origin_txt_first_line(tmp_path):
    appdata = tmp_path / "AppData"
    install = tmp_path / "MT5A"
    _make_origin(appdata, "AAA111", str(install))
    _make_exe(install)
    found = discover_terminals(appdata_dir=appdata,
                               default_install_dir=None)
    assert found == [TerminalInstance(install_dir=str(install),
                                      exe_path=str(install / "terminal64.exe"),
                                      source="appdata")]


def test_discover_handles_utf16_with_bom(tmp_path):
    appdata = tmp_path / "AppData"
    install = tmp_path / "MT5B"
    folder = appdata / "MetaQuotes" / "Terminal" / "BBB222"
    folder.mkdir(parents=True, exist_ok=True)
    # Write with BOM (utf-16 prepends one)
    (folder / "origin.txt").write_text(str(install) + "\n", encoding="utf-16")
    _make_exe(install)
    found = discover_terminals(appdata_dir=appdata, default_install_dir=None)
    assert len(found) == 1
    assert found[0].install_dir == str(install)


def test_discover_skips_folders_without_origin_txt(tmp_path):
    appdata = tmp_path / "AppData"
    (appdata / "MetaQuotes" / "Terminal" / "noorigin").mkdir(parents=True)
    found = discover_terminals(appdata_dir=appdata, default_install_dir=None)
    assert found == []


def test_discover_skips_origin_pointing_at_missing_exe(tmp_path):
    appdata = tmp_path / "AppData"
    install = tmp_path / "MT5C"
    _make_origin(appdata, "CCC333", str(install))
    # no terminal64.exe created
    found = discover_terminals(appdata_dir=appdata, default_install_dir=None)
    assert found == []


def test_discover_skips_malformed_origin_txt(tmp_path):
    appdata = tmp_path / "AppData"
    folder = appdata / "MetaQuotes" / "Terminal" / "BAD1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "origin.txt").write_bytes(b"\xff\xfe\x00\x00garbage")
    found = discover_terminals(appdata_dir=appdata, default_install_dir=None)
    assert found == []


def test_discover_includes_default_program_files_install(tmp_path):
    appdata = tmp_path / "AppData"
    default_install = tmp_path / "ProgramFiles" / "MetaTrader 5"
    _make_exe(default_install)
    found = discover_terminals(appdata_dir=appdata,
                               default_install_dir=str(default_install))
    assert TerminalInstance(install_dir=str(default_install),
                            exe_path=str(default_install / "terminal64.exe"),
                            source="default") in found


def test_discover_dedups_by_exe_path(tmp_path):
    """If two AppData hashes point at the same install dir (same terminal
    logged in twice), it appears once."""
    appdata = tmp_path / "AppData"
    install = tmp_path / "MT5D"
    _make_origin(appdata, "DUP1", str(install))
    _make_origin(appdata, "DUP2", str(install))
    _make_exe(install)
    found = discover_terminals(appdata_dir=appdata, default_install_dir=None)
    assert len(found) == 1
    assert found[0].exe_path == str(install / "terminal64.exe")


def test_discover_does_not_find_portable_instances(tmp_path):
    """Portable instances keep their data in the install dir, not under
    AppData, so origin.txt discovery never sees them. (TerminalManager
    merges the provisioned-instance registry for those.) Documented here
    as a contract: a install dir with no AppData hash is not discovered."""
    appdata = tmp_path / "AppData"
    portable = tmp_path / "PortableInstance"
    _make_exe(portable)
    # no origin.txt under appdata for this install
    found = discover_terminals(appdata_dir=appdata, default_install_dir=None)
    assert found == []