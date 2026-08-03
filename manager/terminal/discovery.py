# manager/terminal/discovery.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_appdata() -> Path:
    return Path(os.environ.get("APPDATA") or str(Path.home()))


def _default_install_dir() -> str:
    return r"C:\Program Files\MetaTrader 5"


@dataclass(frozen=True)
class TerminalInstance:
    """One discovered MT5 terminal install.

    - ``install_dir``: the directory containing terminal64.exe (the value
      read from origin.txt, or the default Program Files path).
    - ``exe_path``: ``<install_dir>/terminal64.exe`` — what the worker's
      ``terminal_path`` config is set to and what kill_terminal matches on.
    - ``source``: ``"appdata"`` (found via an origin.txt hash) or
      ``"default"`` (the standard Program Files install). Provisioned
      portable instances are merged separately by TerminalManager and
      tagged ``"provisioned"`` there.
    """
    install_dir: str
    exe_path: str
    source: str


def _read_origin_install_dir(origin_path: Path) -> str | None:
    try:
        text = origin_path.read_text(encoding="utf-16")
    except (OSError, UnicodeDecodeError):
        return None
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    return first or None


def discover_terminals(appdata_dir: str | os.PathLike | None = None,
                      default_install_dir: str | None = None
                      ) -> list[TerminalInstance]:
    """Enumerate installed MT5 terminals visible under
    ``<appdata_dir>/MetaQuotes/Terminal/<hash>/origin.txt`` plus the default
    Program Files install. Returns deduped TerminalInstances (by exe_path)
    whose terminal64.exe actually exists. Read-only; never raises on a bad
    individual folder — it is skipped. Portable instances are NOT found
    here (they keep no AppData data folder)."""
    base = Path(appdata_dir) if appdata_dir is not None else _default_appdata()
    terminals_root = base / "MetaQuotes" / "Terminal"

    seen: dict[str, TerminalInstance] = {}  # exe_path -> instance (dedup)

    if terminals_root.is_dir():
        for entry in terminals_root.iterdir():
            if not entry.is_dir():
                continue
            origin = entry / "origin.txt"
            if not origin.is_file():
                continue
            install_dir = _read_origin_install_dir(origin)
            if not install_dir:
                continue
            exe_path = str(Path(install_dir) / "terminal64.exe")
            if not Path(exe_path).is_file():
                continue
            seen.setdefault(exe_path, TerminalInstance(
                install_dir=install_dir, exe_path=exe_path, source="appdata"))

    default = default_install_dir if default_install_dir is not None \
        else _default_install_dir()
    default_exe = str(Path(default) / "terminal64.exe")
    if Path(default_exe).is_file():
        seen.setdefault(default_exe, TerminalInstance(
            install_dir=default, exe_path=default_exe, source="default"))

    return list(seen.values())