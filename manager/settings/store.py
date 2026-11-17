# manager/settings/store.py
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path


def _default_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "CopyTradesMT5" / "settings.json"


class SettingsStore:
    """Plain-JSON persistence of the manager's config. Also owns the registry
    of terminal instances THIS manager provisioned (under prior versions), so
    discovery can merge them with origin.txt-discovered installs."""

    def __init__(self, path: str | os.PathLike | None = None):
        self._path = Path(path) if path is not None else _default_path()
        # One lock for every read-modify-write: the engine's tracking callback
        # and the GUI's config save can run on different threads, and the
        # atomic os.replace alone would let one clobber the other's keys.
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict:
        """Return the parsed settings dict, or {} if the file is missing or
        corrupt. Never raises on a bad file — a corrupt store is recoverable
        by re-entering config, not by crashing the app."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        data.setdefault("accounts", {})
        data.setdefault("provisioned_instances", [])
        data.setdefault("global", {})
        return data

    def save(self, data: dict) -> None:
        with self._lock:
            self._save_unlocked(data)

    def _save_unlocked(self, data: dict) -> None:
        """Atomic write: serialize to a temp file in the same directory, then
        os.replace onto the target path so a crash mid-write never leaves a
        truncated settings file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, str(self._path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load_config(self) -> dict:
        """Return the GUI-restorable config dict (master + slaves), or {} if
        none is stored. A missing/corrupt config is recoverable by re-entering
        config, not by crashing."""
        return self.load().get("config", {})

    def save_config(self, config: dict) -> None:
        """Merge `config` into the store (preserving accounts/
        provisioned_instances/global) and atomically save."""
        with self._lock:
            data = self._load_unlocked()
            data["config"] = config
            self._save_unlocked(data)

    def load_master_lot_tracking(self) -> tuple[str, dict[str, float]]:
        """Return (terminal_path, learned lots). Any missing or malformed
        shape degrades to ("", {}); individual invalid lot entries are
        dropped."""
        data = self.load()
        glob = data.get("global")
        if not isinstance(glob, dict):
            return "", {}
        raw = glob.get("master_lot_tracking")
        if not isinstance(raw, dict):
            return "", {}
        path = raw.get("terminal_path")
        path = path if isinstance(path, str) else ""
        lots_raw = raw.get("lots")
        lots: dict[str, float] = {}
        if isinstance(lots_raw, dict):
            for symbol, value in lots_raw.items():
                if not isinstance(symbol, str) or not symbol:
                    continue
                if isinstance(value, bool):
                    continue
                try:
                    lot = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(lot) and lot > 0.0:
                    lots[symbol] = lot
        return path, lots

    def save_master_lot_tracking(self, terminal_path: str,
                                 lots: Mapping[str, float],
                                 suppressed: Iterable[int] = ()) -> None:
        """Persist the learned per-symbol master lots and the tickets whose
        re-learning a reset suppresses, preserving every other key."""
        clean: dict[str, float] = {}
        for symbol, value in lots.items():
            if not isinstance(symbol, str) or not symbol:
                continue
            if isinstance(value, bool):
                continue
            try:
                lot = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(lot) and lot > 0.0:
                clean[symbol] = lot
        clean_suppressed = sorted(
            {t for t in suppressed
             if isinstance(t, int) and not isinstance(t, bool) and t > 0})
        with self._lock:
            data = self._load_unlocked()
            glob = data.get("global")
            if not isinstance(glob, dict):
                glob = {}
                data["global"] = glob
            glob["master_lot_tracking"] = {
                "terminal_path": terminal_path if isinstance(
                    terminal_path, str) else "",
                "lots": clean,
                "suppressed": clean_suppressed,
            }
            self._save_unlocked(data)

    def load_master_lot_suppressed(self) -> set[int]:
        """Return the tickets whose re-learning a reset suppressed. Missing or
        malformed shapes degrade to an empty set."""
        data = self.load()
        glob = data.get("global")
        if not isinstance(glob, dict):
            return set()
        raw = glob.get("master_lot_tracking")
        if not isinstance(raw, dict):
            return set()
        values = raw.get("suppressed")
        if not isinstance(values, list):
            return set()
        return {t for t in values
                if isinstance(t, int) and not isinstance(t, bool) and t > 0}

    def list_provisioned_instances(self) -> list[str]:
        return list(self.load().get("provisioned_instances", []))

    def add_provisioned_instance(self, install_dir: str) -> None:
        with self._lock:
            data = self._load_unlocked()
            insts = data.get("provisioned_instances", [])
            if install_dir not in insts:
                insts.append(install_dir)
            data["provisioned_instances"] = insts
            self._save_unlocked(data)

    def remove_provisioned_instance(self, install_dir: str) -> None:
        with self._lock:
            data = self._load_unlocked()
            insts = [d for d in data.get("provisioned_instances", [])
                     if d != install_dir]
            data["provisioned_instances"] = insts
            self._save_unlocked(data)