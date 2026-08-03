# manager/terminal/manager.py
from __future__ import annotations

import os
from pathlib import Path

from manager.terminal.discovery import TerminalInstance, discover_terminals
from manager.settings.store import SettingsStore


class TerminalManagerError(Exception):
    """Raised when there are not enough terminal instances to assign one per
    account (defensive — every account carries an explicit terminal_path in
    the terminal-path-only workflow, so this is unreachable in normal use)."""


def _default_process_iter(attrs=None):
    import psutil
    return psutil.process_iter(attrs)


def _norm(p: str) -> str:
    return os.path.normpath(p).lower()


class TerminalManager:
    """Owns terminal-instance lifecycle: discover existing installs
    (origin.txt + default Program Files + the store's provisioned registry)
    and assign one instance per account. Provisioning (auto-install) was
    removed — the user installs and logs in to terminals manually. Kill a
    specific instance's terminal64.exe before a worker respawn so
    mt5.initialize does not hit the -10003 IPC-collision from a stale
    terminal. The provisioned-instance registry is still merged by
    discover_all so terminals installed under prior versions remain
    discoverable."""

    def __init__(self, store=None, discover_fn=None, process_iter_fn=None,
                 sleep_fn=None, time_fn=None):
        self._store = store if store is not None else SettingsStore()
        self._discover_fn = (discover_fn if discover_fn is not None
                             else discover_terminals)
        self._process_iter_fn = (process_iter_fn if process_iter_fn is not None
                                 else _default_process_iter)
        self._sleep = sleep_fn if sleep_fn is not None else __import__("time").sleep
        self._time = time_fn if time_fn is not None else __import__("time").monotonic

    def discover_all(self) -> list[TerminalInstance]:
        """Merge origin.txt-discovered installs with the store's provisioned
        registry. Dedup by exe_path (origin.txt wins ties on order)."""
        seen: dict[str, TerminalInstance] = {}
        for inst in self._discover_fn():
            seen.setdefault(_norm(inst.exe_path), inst)
        for install_dir in self._store.list_provisioned_instances():
            exe_path = str(Path(install_dir) / "terminal64.exe")
            key = _norm(exe_path)
            if key not in seen:
                seen[key] = TerminalInstance(install_dir=install_dir,
                                              exe_path=exe_path,
                                              source="provisioned")
        return list(seen.values())

    def assign(self, accounts: list[dict]) -> dict[str, TerminalInstance]:
        """Assign one terminal instance per account. Accounts carrying a
        ``terminal_path`` keep it (tagged ``override``); the rest auto-assign
        from the available pool. Raises if there are not enough instances."""
        available = self.discover_all()
        pool = list(available)
        assigned: dict[str, TerminalInstance] = {}
        for acct in accounts:
            acct_id = acct["id"]
            override = acct.get("terminal_path")
            if override:
                install_dir = str(Path(override).parent)
                assigned[acct_id] = TerminalInstance(
                    install_dir=install_dir, exe_path=override,
                    source="override")
                pool = [p for p in pool if _norm(p.exe_path) != _norm(override)]
                continue
            if not pool:
                raise TerminalManagerError(
                    f"not enough terminal instances to assign {acct_id} "
                    f"(need {len(accounts)}, have {len(assigned) + len(pool)})")
            assigned[acct_id] = pool.pop(0)
        return assigned

    def kill_terminal(self, exe_path: str) -> int:
        """Terminate every running process whose executable path equals
        exe_path (case-insensitive, normalized). Best-effort: per-process
        NoSuchProcess / AccessDenied are swallowed. terminate() then wait();
        on a wait timeout, fall back to kill(). Returns the count of processes
        we attempted to terminate (matched by exe path). Never kills by image
        name alone — multiple instances share the name terminal64.exe."""
        target = _norm(exe_path)
        count = 0
        for proc in self._process_iter_fn():
            try:
                pexe = proc.exe()
            except Exception:
                pexe = None
            if not pexe or _norm(pexe) != target:
                continue
            count += 1
            try:
                proc.terminate()
            except Exception:
                continue
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return count