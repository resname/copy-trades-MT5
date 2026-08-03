# manager/settings/store.py
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _default_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "CopyTradesMT5" / "settings.json"


class SettingsStore:
    """Plain-JSON persistence of the manager's config. Knows nothing about
    crypto (password blobs are opaque base64 strings in the account dicts).
    Also owns the registry of terminal instances THIS manager provisioned,
    so discovery can merge them with origin.txt-discovered installs."""

    def __init__(self, path: str | os.PathLike | None = None):
        self._path = Path(path) if path is not None else _default_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict:
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
        data.setdefault("learned_servers", [])
        return data

    def save(self, data: dict) -> None:
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

    def list_provisioned_instances(self) -> list[str]:
        return list(self.load().get("provisioned_instances", []))

    def add_provisioned_instance(self, install_dir: str) -> None:
        data = self.load()
        insts = data.get("provisioned_instances", [])
        if install_dir not in insts:
            insts.append(install_dir)
        data["provisioned_instances"] = insts
        self.save(data)

    def remove_provisioned_instance(self, install_dir: str) -> None:
        data = self.load()
        insts = [d for d in data.get("provisioned_instances", [])
                 if d != install_dir]
        data["provisioned_instances"] = insts
        self.save(data)