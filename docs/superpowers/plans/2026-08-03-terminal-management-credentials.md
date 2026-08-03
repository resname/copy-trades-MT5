# Terminal Management & Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add terminal-instance lifecycle (discover installed MT5 terminals, provision the shortfall via `mt5setup.exe /auto /path`, assign one instance per account, kill a specific instance's `terminal64.exe`), DPAPI-encrypted credential storage, the JSON settings store, and the deferred supervisor hardening (startup readiness gate + restart backoff) — everything the GUI (Plan 4) needs to wire Start/Stop and per-account login.

**Architecture:** Three new manager-side packages. `manager/settings/` holds DPAPI crypto (`credentials.py`) and a plain-JSON settings store (`store.py`) that also persists the registry of terminals *this manager* provisioned. `manager/terminal/` holds `discovery.py` (enumerate existing installs via `origin.txt` + the default Program Files path), `provisioning.py` (silent-install new instances with `mt5setup.exe /auto /path`), and `manager.py` (the `TerminalManager` that discovers + provisions + assigns + exposes the `kill_terminal(exe_path)` callable wired into the `Supervisor`). The supervisor itself gains a startup readiness gate (spawn slaves, wait for each slave's `SymbolInfoMsg` + first `StatusMsg` before spawning the master) and per-worker exponential restart backoff — the two MUSTs deferred from Plan 2.

**Tech Stack:** Python 3.11+, pywin32 (`win32crypt` — DPAPI), psutil (process enumeration / kill by executable path), MetaTrader5 (unchanged, already behind the worker adapter), pytest. All Windows-only.

## Global Constraints

Copied verbatim from the spec + the standing security constraints + Plan 2's deferred MUSTs. Every task's requirements implicitly include this section.

- **Demo accounts only — never capture or log in with a real account.** (Standing security constraint. Tests use fake/demo credentials; the manual smoke test is demo-only.)
- **Credentials are passed to workers through the pipe, never on the command line, so they do not appear in the process list.** (Unchanged from Plan 2. This plan adds *at-rest* DPAPI encryption; the in-flight-via-pipe rule stays.)
- **DPAPI-encrypted credentials at rest** — pywin32 `win32crypt.CryptProtectData` / `CryptUnprotectData`, per-user OS-managed key (`Flags=0`). Cross-user/machine decryption fails by design → the app catches that and re-prompts.
- **Capture artifacts (pcaps, Frida logs) can be large and may contain credentials — they are gitignored, never committed.** (Unchanged.)
- **One terminal install per concurrent account** — each in its own directory; two terminals cannot run from the same folder. Provisioned instances go to `%LOCALAPPDATA%\CopyTradesMT5\terminals\instance_<n>` (user-writable, no UAC).
- **`login` must be an `int`, not a string.** The store keeps login as int in the account dict.
- **Keep terminal windows visible** (minimized acceptable). The manager does not force-hide terminal windows.
- **`MetaTrader5` and `win32crypt` are imported lazily** inside the functions that use them, so the unit-test suite runs without MetaTrader5/pywin32 installed (matches the Plan 2 adapter seam pattern). All crypto/process/subprocess call sites take an injectable dependency (a `crypto` module, a `process_iter_fn`, a `runner`, a `downloader`) so tests never hit a real terminal, the network, or DPAPI.
- **Engine purity preserved** (Plan 2 invariant): the engine never touches the terminal, the filesystem, the clock, or the process list. The supervisor remains the only seam. All new I/O lives in `settings/` and `terminal/`, never in `engine/`.
- **Slave normalizes (EA-faithful)** (unchanged): the manager sends RAW master SL/TP + master open price + side; the slave worker normalizes. This plan does not alter copy logic.
- **Windows-only.** Paths use `pathlib.Path` and forward slashes in literals; `os.environ["APPDATA"]` / `os.environ["LOCALAPPDATA"]` are read at call time (never imported at module load) and are overridable via constructor args for tests.

---

## File structure

New files (all under `manager/`):

```
manager/
  settings/
    __init__.py            # empty package marker
    credentials.py         # DPAPI encrypt/decrypt + base64 password wrappers + CredentialDecryptError
    store.py               # SettingsStore: JSON load/save + provisioned-instance registry
  terminal/
    __init__.py            # empty package marker
    discovery.py           # discover_terminals(): origin.txt + default Program Files
    provisioning.py        # provision_instance(): mt5setup.exe /auto /path + download_setup()
    manager.py             # TerminalManager: discover + provision + assign + kill_terminal()
  tests/
    test_credentials.py
    test_settings_store.py
    test_terminal_discovery.py
    test_terminal_provisioning.py
    test_terminal_manager.py
    test_supervisor_readiness.py   # readiness gate + restart backoff (extends test_supervisor.py)
```

Modified files:

- `manager/supervisor.py` — add readiness flags to `WorkerHandle`, set them in `_dispatch_slave`, add `slave_ready` / `wait_for_slaves_ready`; add restart-backoff fields + logic in `_restart`, reset on message.

Responsibilities:
- `credentials.py` — the ONLY module that touches `win32crypt`. Pure functions; no state.
- `store.py` — the ONLY module that reads/writes the settings JSON. Pure JSON; never imports crypto (password blobs are opaque base64 strings already in the account dict). Owns the provisioned-instance registry.
- `discovery.py` — read-only enumeration of existing terminal installs (the AppData `origin.txt` scan + the default path). No mutations, no network.
- `provisioning.py` — the ONLY module that runs `mt5setup.exe` and downloads the installer. No discovery, no assignment.
- `manager.py` (`TerminalManager`) — orchestrator: merges discovery results with the provisioned-instance registry from the store, provisions the shortfall, assigns one instance per account, and provides `kill_terminal(exe_path)` (psutil). This is the seam Plan 4's GUI calls.
- `supervisor.py` — gains the readiness gate + backoff (deferred Plan 2 MUSTs). The `kill_terminal` constructor hook (already present from Plan 2) is fed by `TerminalManager.kill_terminal` in Plan 4.

---

## Task 1: DPAPI credential encryption (`settings/credentials.py`)

**Files:**
- Create: `manager/settings/__init__.py` (empty)
- Create: `manager/settings/credentials.py`
- Test: `manager/tests/test_credentials.py`

**Interfaces:**
- Produces: `CredentialDecryptError(Exception)`; `encrypt(plaintext: str, crypto=None) -> bytes`; `decrypt(blob: bytes, crypto=None) -> str`; `encrypt_password(plaintext: str, crypto=None) -> str` (base64 of `encrypt`, JSON-safe); `decrypt_password(blob: str, crypto=None) -> str` (inverse). `crypto` is a module exposing `CryptProtectData` / `CryptUnprotectData` (lazy-imported `win32crypt` when `None`).

- [ ] **Step 1: Write the failing test**

```python
# manager/tests/test_credentials.py
import base64
import pytest

from manager.settings import credentials


class FakeCrypto:
    """Mimics win32crypt: CryptProtectData(data, desc, None, None, None, 0) -> bytes;
    CryptUnprotectData(blob, None, None, None, 0) -> (desc, bytes)."""
    def __init__(self):
        self.calls = []

    def CryptProtectData(self, data, desc, *rest):
        self.calls.append(("protect", data, desc))
        return b"ENC:" + data

    def CryptUnprotectData(self, blob, *rest):
        self.calls.append(("unprotect", blob))
        if not blob.startswith(b"ENC:"):
            raise ValueError("bad blob")
        return ("CopyTradesMT5", blob[len(b"ENC:"):])


def test_encrypt_decrypt_round_trip():
    crypto = FakeCrypto()
    blob = credentials.encrypt("s3cret", crypto=crypto)
    assert blob == b"ENC:s3cret"
    assert credentials.decrypt(blob, crypto=crypto) == "s3cret"


def test_encrypt_password_is_base64_string():
    crypto = FakeCrypto()
    blob_str = credentials.encrypt_password("s3cret", crypto=crypto)
    assert isinstance(blob_str, str)
    # base64-decodes back to the raw DPAPI blob
    assert base64.b64decode(blob_str) == b"ENC:s3cret"
    assert credentials.decrypt_password(blob_str, crypto=crypto) == "s3cret"


def test_decrypt_garbage_raises_credential_decrypt_error():
    crypto = FakeCrypto()
    with pytest.raises(credentials.CredentialDecryptError):
        credentials.decrypt(b"not-a-real-blob", crypto=crypto)


def test_decrypt_non_bytes_raises_credential_decrypt_error():
    with pytest.raises(credentials.CredentialDecryptError):
        credentials.decrypt("string-not-bytes", crypto=FakeCrypto())


def test_decrypt_password_bad_base64_raises_credential_decrypt_error():
    with pytest.raises(credentials.CredentialDecryptError):
        credentials.decrypt_password("!!!not base64!!!", crypto=FakeCrypto())


def test_encrypt_rejects_none():
    with pytest.raises(ValueError):
        credentials.encrypt(None, crypto=FakeCrypto())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_credentials.py -v`
Expected: FAIL with `ImportError: cannot import name 'credentials'` (module not created yet).

- [ ] **Step 3: Write minimal implementation**

```python
# manager/settings/__init__.py
  (empty file)
```

```python
# manager/settings/credentials.py
from __future__ import annotations

import base64


class CredentialDecryptError(Exception):
    """Raised when a stored credential blob cannot be decrypted — wrong user,
    wrong machine, corrupted, or tampered. The GUI catches this and prompts
    the user to re-enter the credential. DPAPI blobs are bound to the OS user
    that encrypted them; an admin password reset (vs. a user self-change) also
    invalidates the master keys and makes prior blobs unrecoverable."""


def _load_crypto():
    """Lazy-import win32crypt so the test suite runs without pywin32."""
    import win32crypt  # pywin32
    return win32crypt


def encrypt(plaintext: str, crypto=None) -> bytes:
    """DPAPI-encrypt a UTF-8 plaintext string under the current user's
    OS-managed key (Flags=0). Returns the opaque blob bytes. ``crypto`` is
    the win32crypt module, injected in tests; lazy-imported when None."""
    if plaintext is None:
        raise ValueError("plaintext must be a str")
    if not isinstance(plaintext, str):
        raise ValueError("plaintext must be a str")
    mod = crypto if crypto is not None else _load_crypto()
    return mod.CryptProtectData(plaintext.encode("utf-8"), "CopyTradesMT5",
                                None, None, None, 0)


def decrypt(blob, crypto=None) -> str:
    """Inverse of encrypt. Raises CredentialDecryptError on any failure
    (pywintypes.error from a cross-user/machine blob, corrupted blob, wrong
    type, etc.). The broad `except` is intentional: pywintypes.error is not
    importable without pywin32, and any decrypt failure means re-prompt."""
    if not isinstance(blob, (bytes, bytearray)):
        raise CredentialDecryptError("blob must be bytes")
    mod = crypto if crypto is not None else _load_crypto()
    try:
        _desc, data = mod.CryptUnprotectData(bytes(blob), None, None, None, 0)
    except CredentialDecryptError:
        raise
    except Exception as exc:
        raise CredentialDecryptError(str(exc)) from exc
    return data.decode("utf-8")


def encrypt_password(plaintext: str, crypto=None) -> str:
    """JSON-safe credential: base64 of the DPAPI blob. Store this string in
    the account dict under ``password_blob``; it survives JSON round-trips
    and never exposes the plaintext or the raw blob in logs."""
    return base64.b64encode(encrypt(plaintext, crypto=crypto)).decode("ascii")


def decrypt_password(blob: str, crypto=None) -> str:
    """Inverse of encrypt_password."""
    if not isinstance(blob, str):
        raise CredentialDecryptError("password blob must be a str")
    try:
        raw = base64.b64decode(blob, validate=True)
    except Exception as exc:
        raise CredentialDecryptError(str(exc)) from exc
    return decrypt(raw, crypto=crypto)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_credentials.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS (no regressions; the new 6 tests add to the existing 120).

- [ ] **Step 6: Commit**

```bash
git add manager/settings/__init__.py manager/settings/credentials.py manager/tests/test_credentials.py
git commit -m "feat(settings): DPAPI credential encrypt/decrypt (pywin32, lazy)"
```

---

## Task 2: JSON settings store + provisioned-instance registry (`settings/store.py`)

**Files:**
- Create: `manager/settings/store.py`
- Test: `manager/tests/test_settings_store.py`

**Interfaces:**
- Consumes: nothing (pure JSON + `pathlib`; deliberately does NOT import `credentials` — password blobs are opaque base64 strings already present in the account dict, so the store stays crypto-free and independently testable).
- Produces: `SettingsStore(path=None)`. Methods: `load() -> dict` (returns `{}` for missing/corrupt file); `save(data: dict) -> None` (atomic write: temp file + `os.replace`); `list_provisioned_instances() -> list[str]` (install dirs this manager created); `add_provisioned_instance(install_dir: str) -> None` (idempotent, persists immediately); `remove_provisioned_instance(install_dir: str) -> None`. The on-disk shape is `{"accounts": {account_id: {...}}, "provisioned_instances": [install_dir, ...], "global": {...}}`. Default path: `%APPDATA%\CopyTradesMT5\settings.json`.

- [ ] **Step 1: Write the failing test**

```python
# manager/tests/test_settings_store.py
import json
from pathlib import Path

from manager.settings.store import SettingsStore


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(path=tmp_path / "settings.json")


def test_load_missing_file_returns_empty_dict():
    store = _store(Path("/no/such/dir"))
    assert store.load() == {}


def test_save_then_load_round_trip(tmp_path):
    store = _store(tmp_path)
    data = {"accounts": {"master": {"login": 5001, "server": "Demo-Server"}},
            "provisioned_instances": [], "global": {"heartbeat_seconds": 5}}
    store.save(data)
    assert store.load() == data


def test_password_blob_survives_round_trip(tmp_path):
    """The store is crypto-agnostic: an opaque base64 password blob stored
    in an account dict is returned byte-for-byte after a save/load cycle."""
    store = _store(tmp_path)
    acct = {"login": 5001, "server": "Demo-Server",
            "password_blob": "ZW5jcnlwdGVk"}
    store.save({"accounts": {"s1": acct}, "provisioned_instances": [],
                "global": {}})
    loaded = store.load()
    assert loaded["accounts"]["s1"]["password_blob"] == "ZW5jcnlwdGVk"
    assert loaded["accounts"]["s1"]["login"] == 5001


def test_load_corrupt_json_returns_empty_dict(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not valid json", encoding="utf-8")
    store = SettingsStore(path=p)
    assert store.load() == {}


def test_provisioned_instance_registry_add_list_remove(tmp_path):
    store = _store(tmp_path)
    assert store.list_provisioned_instances() == []
    store.add_provisioned_instance(r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0")
    store.add_provisioned_instance(r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1")
    assert store.list_provisioned_instances() == [
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0",
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1"]
    # persisted across a new store instance
    assert SettingsStore(path=tmp_path / "settings.json").list_provisioned_instances() == [
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0",
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1"]
    store.remove_provisioned_instance(r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_0")
    assert store.list_provisioned_instances() == [
        r"C:\Users\s\AppData\Local\CopyTradesMT5\terminals\instance_1"]


def test_add_provisioned_instance_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.add_provisioned_instance("C:/inst_0")
    store.add_provisioned_instance("C:/inst_0")
    assert store.list_provisioned_instances() == ["C:/inst_0"]


def test_save_preserves_existing_provisioned_instances(tmp_path):
    """add_provisioned_instance must not clobber accounts/global data."""
    store = _store(tmp_path)
    store.save({"accounts": {"master": {"login": 1}}, "provisioned_instances": [],
                "global": {"x": 1}})
    store.add_provisioned_instance("C:/inst_0")
    loaded = store.load()
    assert loaded["accounts"] == {"master": {"login": 1}}
    assert loaded["global"] == {"x": 1}
    assert loaded["provisioned_instances"] == ["C:/inst_0"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_settings_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.settings.store'`.

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_settings_store.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/settings/store.py manager/tests/test_settings_store.py
git commit -m "feat(settings): JSON settings store + provisioned-instance registry"
```

---

## Task 3: Terminal discovery via `origin.txt` (`terminal/discovery.py`)

**Files:**
- Create: `manager/terminal/__init__.py` (empty)
- Create: `manager/terminal/discovery.py`
- Test: `manager/tests/test_terminal_discovery.py`

**Interfaces:**
- Consumes: nothing but the filesystem (read-only). Paths are injectable for tests.
- Produces: `TerminalInstance` dataclass `(install_dir: str, exe_path: str, source: str)` where `source ∈ {"appdata", "default"}`; `discover_terminals(appdata_dir=None, default_install_dir=None) -> list[TerminalInstance]`. Enumerates `<appdata_dir>/MetaQuotes/Terminal/<hash>/origin.txt` (UTF-16, first line = install dir), maps each to `<install_dir>/terminal64.exe`, dedups by `exe_path`, then appends the default `C:\Program Files\MetaTrader 5\` install if its `terminal64.exe` exists. Portable instances are NOT found here (they keep their data in the install dir, not under AppData) — `TerminalManager` (Task 5) merges the provisioned-instance registry.

**Why UTF-16:** MetaQuotes states "all text files are of Unicode format"; `origin.txt` is UTF-16 LE. We read with `encoding="utf-16"` and tolerate a leading BOM.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_terminal_discovery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.terminal.discovery'`.

- [ ] **Step 3: Write minimal implementation**

```python
# manager/terminal/__init__.py
  (empty file)
```

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_terminal_discovery.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/terminal/__init__.py manager/terminal/discovery.py manager/tests/test_terminal_discovery.py
git commit -m "feat(terminal): discover MT5 instances via origin.txt + default path"
```

---

## Task 4: Terminal provisioning via `mt5setup.exe /auto /path` (`terminal/provisioning.py`)

**Files:**
- Create: `manager/terminal/provisioning.py`
- Test: `manager/tests/test_terminal_provisioning.py`

**Interfaces:**
- Consumes: nothing at import time. `subprocess` and `urllib` are injected via the `runner` / `downloader` params so tests never run a real installer or hit the network.
- Produces:
  - `SETUP_DOWNLOAD_URL = "https://www.metatrader5.com/en/download"` (the bootstrapper page; the real `mt5setup.exe` URL is resolved by the downloader — kept as a constant for documentation + the default cache path).
  - `instance_install_dir(index: int, root: str | None = None) -> str` — `<root>/terminals/instance_<index>`, root default `%LOCALAPPDATA%\CopyTradesMT5`.
  - `provision_command(setup_path: str, install_dir: str) -> list[str]` — the argv `[setup_path, "/auto", "/path:" + install_dir]` (pure; tested directly).
  - `provision_instance(index: int, setup_path: str, install_root: str | None = None, runner=None, waiter=None, poll_interval: float = 1.0, timeout: float = 300.0) -> str` — runs the installer (default `runner = subprocess.run`), then waits for `<install_dir>/terminal64.exe` to appear (default `waiter` polls the filesystem). Returns the install dir. Raises `ProvisioningError` on installer non-zero exit or timeout.
  - `download_setup(cache_path: str | None = None, downloader=None) -> str` — fetches `mt5setup.exe` to a cache path (default `%LOCALAPPDATA%\CopyTradesMT5\mt5setup.exe`); `downloader(src_url, dest_path)` is injected (default uses `urllib.request.urlretrieve`). Returns the cache path.

**Facts baked in (from research):** `mt5setup.exe /auto /path:"<dir>"` is the official silent+custom-path mode; it is a web installer (needs internet to MetaQuotes' CDN); the completion signal is undocumented, so we both wait on the process exit AND poll for `terminal64.exe`. Installing under `%LOCALAPPDATA%` avoids UAC.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_terminal_provisioning.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.terminal.provisioning'`.

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_terminal_provisioning.py -v`
Expected: PASS (8 tests). If `test_provision_instance_runs_installer_and_waits_for_exe` is fiddly, simplify the runner's exe-creation to read the path from `cmd[2][len("/path:"):]` — the assert already documents that. (The `if False else` branch in the test is intentional: it documents that the inline extraction falls back to `cmd[2][len("/path:"):]`; both forms resolve to the same value.)

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/terminal/provisioning.py manager/tests/test_terminal_provisioning.py
git commit -m "feat(terminal): provision MT5 instances via mt5setup.exe /auto /path"
```

---

## Task 5: Terminal manager — discover + provision + assign + kill (`terminal/manager.py`)

**Files:**
- Create: `manager/terminal/manager.py`
- Test: `manager/tests/test_terminal_manager.py`

**Interfaces:**
- Consumes: `manager.terminal.discovery.discover_terminals` (Task 3), `manager.terminal.provisioning.provision_instance` + `download_setup` (Task 4), `manager.settings.store.SettingsStore` (Task 2). All injectable through the constructor for tests.
- Produces: `TerminalManager`. Constructor `TerminalManager(store=None, discover_fn=None, provision_fn=None, download_fn=None, process_iter_fn=None, sleep_fn=None, time_fn=None)`. Methods:
  - `discover_all() -> list[TerminalInstance]` — merges `discover_terminals()` (appdata + default) with the store's provisioned-instance registry (each tagged `source="provisioned"`, `exe_path=<install_dir>/terminal64.exe`). Dedups by exe_path.
  - `required_count(num_slaves: int) -> int` — `1 + num_slaves` (master + slaves).
  - `provision_shortfall(num_slaves: int, setup_path: str | None = None) -> list[str]` — discover_all, compute shortfall = required - available, install that many via `provision_instance`, register each in the store, return the list of new install dirs.
  - `assign(accounts: list[dict]) -> dict[str, TerminalInstance]` — given accounts each carrying an optional `terminal_path` (user override), assign one instance per account. Accounts with a matching override use it; the rest get auto-assigned from the available pool (discovered + provisioned) round-robin/first-free. Returns `{account_id: TerminalInstance}`. Raises `TerminalManagerError` if there are not enough instances (caller should provision_shortfall first).
  - `kill_terminal(exe_path: str) -> int` — terminate every running process whose `exe()` equals `exe_path` (case-insensitive, normalized). psutil by default; `process_iter_fn` injectable. Returns the count terminated. Catches `NoSuchProcess`/`AccessDenied` per process (best-effort). Never kills by image name alone.

**`TerminalInstance` reuse:** import from `discovery` (Task 3). Provisioned instances are constructed as `TerminalInstance(install_dir=..., exe_path=<install_dir>/terminal64.exe, source="provisioned")`.

- [ ] **Step 1: Write the failing test**

```python
# manager/tests/test_terminal_manager.py
from pathlib import Path

import pytest

from manager.terminal.discovery import TerminalInstance
from manager.terminal.manager import TerminalManager, TerminalManagerError
from manager.settings.store import SettingsStore


class FakeStore:
    """Minimal store double: tracks provisioned-instance registry."""
    def __init__(self):
        self._insts = []
    def list_provisioned_instances(self):
        return list(self._insts)
    def add_provisioned_instance(self, d):
        if d not in self._insts:
            self._insts.append(d)
    def remove_provisioned_instance(self, d):
        self._insts = [x for x in self._insts if x != d]


class FakeProc:
    def __init__(self, exe, alive=True):
        self._exe = exe
        self._alive = alive
        self.terminated = False
        self.pid = hash(exe) & 0xFFFF
    def exe(self):
        return self._exe if self._alive else None
    def terminate(self):
        self._alive = False
        self.terminated = True
    def wait(self, timeout=None):
        return 0
    def kill(self):
        self._alive = False
        self.terminated = True


def _mgr(**kw):
    store = kw.pop("store", FakeStore())
    return TerminalManager(store=store,
                            discover_fn=lambda **k: [],
                            process_iter_fn=lambda attrs=None: [],
                            sleep_fn=lambda s: None, time_fn=lambda: 0.0,
                            **kw)


def test_required_count_is_one_plus_slaves():
    m = _mgr()
    assert m.required_count(0) == 1
    assert m.required_count(3) == 4


def test_discover_all_merges_appdata_default_and_provisioned():
    store = FakeStore()
    store.add_provisioned_instance(r"C:\prov\instance_0")
    discovered = [
        TerminalInstance(r"C:\Appdata\MT5A", r"C:\Appdata\MT5A\terminal64.exe", "appdata"),
        TerminalInstance(r"C:\Program Files\MetaTrader 5", r"C:\Program Files\MetaTrader 5\terminal64.exe", "default"),
    ]
    m = TerminalManager(store=store, discover_fn=lambda **k: discovered,
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    # stub exe existence: provisioning of provisioned exe_path
    all_insts = m.discover_all()
    by_exe = {i.exe_path: i for i in all_insts}
    assert by_exe[r"C:\Appdata\MT5A\terminal64.exe"].source == "appdata"
    assert by_exe[r"C:\Program Files\MetaTrader 5\terminal64.exe"].source == "default"
    # provisioned instance is included with source="provisioned"
    assert r"C:\prov\instance_0\terminal64.exe" in by_exe
    assert by_exe[r"C:\prov\instance_0\terminal64.exe"].source == "provisioned"


def test_discover_all_dedups_by_exe_path():
    """If the store's provisioned registry overlaps an appdata-discovered
    install, it appears once."""
    store = FakeStore()
    store.add_provisioned_instance(r"C:\overlap")
    discovered = [TerminalInstance(r"C:\overlap", r"C:\overlap\terminal64.exe", "appdata")]
    m = TerminalManager(store=store, discover_fn=lambda **k: discovered,
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    insts = m.discover_all()
    assert len(insts) == 1


def test_provision_shortfall_installs_and_registers():
    store = FakeStore()
    m = TerminalManager(store=store,
                        discover_fn=lambda **k: [],  # nothing installed yet
                        provision_fn=lambda index, setup_path, install_root=None,
                                       **k: fr"C:\prov\instance_{index}",
                        download_fn=lambda cache_path=None: r"C:\cache\mt5setup.exe",
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    new_dirs = m.provision_shortfall(num_slaves=2,
                                     setup_path=r"C:\cache\mt5setup.exe")
    # required = 3, available = 0 -> 3 new
    assert new_dirs == [r"C:\prov\instance_0", r"C:\prov\instance_1",
                       r"C:\prov\instance_2"]
    assert store.list_provisioned_instances() == new_dirs


def test_provision_shortfall_only_installs_the_gap():
    store = FakeStore()
    store.add_provisioned_instance(r"C:\existing\instance_0")
    m = TerminalManager(store=store,
                        discover_fn=lambda **k: [
                            TerminalInstance(r"C:\existing\instance_0",
                                             r"C:\existing\instance_0\terminal64.exe",
                                             "provisioned")],
                        provision_fn=lambda index, setup_path, install_root=None, **k:
                            fr"C:\prov\instance_{index}",
                        download_fn=lambda cache_path=None: r"C:\cache\mt5setup.exe",
                        process_iter_fn=lambda attrs=None: [],
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    new_dirs = m.provision_shortfall(num_slaves=1)  # required 2, available 1
    assert len(new_dirs) == 1


def test_assign_one_instance_per_account_auto():
    discovered = [
        TerminalInstance(r"C:\i0", r"C:\i0\terminal64.exe", "appdata"),
        TerminalInstance(r"C:\i1", r"C:\i1\terminal64.exe", "appdata"),
    ]
    m = _mgr(discover_fn=lambda **k: discovered)
    accounts = [{"id": "master"}, {"id": "s1"}]
    assigned = m.assign(accounts)
    assert set(assigned.keys()) == {"master", "s1"}
    assert assigned["master"].exe_path == r"C:\i0\terminal64.exe"
    assert assigned["s1"].exe_path == r"C:\i1\terminal64.exe"


def test_assign_respects_user_terminal_path_override():
    discovered = [TerminalInstance(r"C:\i0", r"C:\i0\terminal64.exe", "appdata")]
    m = _mgr(discover_fn=lambda **k: discovered)
    accounts = [{"id": "master", "terminal_path": r"C:\override\terminal64.exe"}]
    assigned = m.assign(accounts)
    assert assigned["master"].exe_path == r"C:\override\terminal64.exe"
    assert assigned["master"].source == "override"


def test_assign_raises_when_not_enough_instances():
    discovered = [TerminalInstance(r"C:\i0", r"C:\i0\terminal64.exe", "appdata")]
    m = _mgr(discover_fn=lambda **k: discovered)
    with pytest.raises(TerminalManagerError):
        m.assign([{"id": "master"}, {"id": "s1"}])  # need 2, have 1


def test_kill_terminal_matches_by_exe_path_case_insensitive():
    procs = [
        FakeProc(r"C:\Inst\terminal64.exe"),
        FakeProc(r"c:\inst\terminal64.exe"),  # same, case differs -> same instance
        FakeProc(r"C:\Other\terminal64.exe"),  # different instance
    ]
    m = TerminalManager(store=FakeStore(), discover_fn=lambda **k: [],
                        process_iter_fn=lambda attrs=None: procs,
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    n = m.kill_terminal(r"c:\INST\terminal64.exe")
    assert n == 2  # the two matching, case-insensitively
    assert procs[0].terminated and procs[1].terminated
    assert not procs[2].terminated


def test_kill_terminal_terminates_then_kills_on_timeout():
    class SlowProc:
        def __init__(self, exe):
            self._exe = exe
            self.killed = False
            self.pid = 1
        def exe(self): return self._exe
        def terminate(self): pass
        def wait(self, timeout=None):
            raise TimeoutError  # psutil raises psutil.TimeoutExpired; tests use this
        def kill(self):
            self.killed = True
    procs = [SlowProc(r"C:\Inst\terminal64.exe")]
    m = TerminalManager(store=FakeStore(), discover_fn=lambda **k: [],
                        process_iter_fn=lambda attrs=None: procs,
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    m.kill_terminal(r"C:\Inst\terminal64.exe")
    assert procs[0].killed


def test_kill_terminal_handles_missing_and_access_denied():
    class GoneProc:
        def __init__(self, exe): self._exe = exe; self.pid = 2
        def exe(self): return self._exe
        def terminate(self): raise FileNotFoundError  # NoSuchProcess analogue
        def wait(self, timeout=None): return 0
        def kill(self): pass
    class DeniedProc:
        def __init__(self, exe): self._exe = exe; self.pid = 3
        def exe(self): return self._exe
        def terminate(self): raise PermissionError  # AccessDenied analogue
        def wait(self, timeout=None): return 0
        def kill(self): raise PermissionError
    procs = [GoneProc(r"C:\Inst\terminal64.exe"),
             DeniedProc(r"C:\Inst\terminal64.exe")]
    m = TerminalManager(store=FakeStore(), discover_fn=lambda **k: [],
                        process_iter_fn=lambda attrs=None: procs,
                        sleep_fn=lambda s: None, time_fn=lambda: 0.0)
    # best-effort: counts attempts, does not raise
    n = m.kill_terminal(r"C:\Inst\terminal64.exe")
    assert n == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_terminal_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.terminal.manager'`.

- [ ] **Step 3: Write minimal implementation**

```python
# manager/terminal/manager.py
from __future__ import annotations

import os
from pathlib import Path

from manager.terminal.discovery import TerminalInstance, discover_terminals
from manager.terminal import provisioning
from manager.settings.store import SettingsStore


class TerminalManagerError(Exception):
    """Raised when there are not enough terminal instances to assign one per
    account (caller should provision_shortfall first)."""


def _default_process_iter(attrs=None):
    import psutil
    return psutil.process_iter(attrs)


def _norm(p: str) -> str:
    return os.path.normpath(p).lower()


class TerminalManager:
    """Owns terminal-instance lifecycle: discover existing installs (origin.txt
    + default Program Files), provision the shortfall via mt5setup.exe /auto
    /path, assign one instance per account, and kill a specific instance's
    terminal64.exe before a worker respawn (so mt5.initialize does not hit the
    -10003 IPC-collision from a stale terminal). The provisioned-instance
    registry is persisted in the settings store so provisioned (portable)
    instances survive restarts — origin.txt discovery cannot see them."""

    def __init__(self, store=None, discover_fn=None, provision_fn=None,
                 download_fn=None, process_iter_fn=None, sleep_fn=None,
                 time_fn=None):
        self._store = store if store is not None else SettingsStore()
        self._discover_fn = discover_fn if discover_fn is not None \
            else discover_terminals
        self._provision_fn = provision_fn if provision_fn is not None \
            else provisioning.provision_instance
        self._download_fn = download_fn if download_fn is not None \
            else provisioning.download_setup
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

    def required_count(self, num_slaves: int) -> int:
        return 1 + max(0, num_slaves)

    def provision_shortfall(self, num_slaves: int,
                            setup_path: str | None = None) -> list[str]:
        """Install as many new instances as needed to reach required_count.
        Downloads the setup bootstrapper if setup_path is None. Each new
        install dir is registered in the store. Returns the new install dirs."""
        if setup_path is None:
            setup_path = self._download_fn()
        available = len(self.discover_all())
        required = self.required_count(num_slaves)
        shortfall = max(0, required - available)
        new_dirs: list[str] = []
        # index instances by the current provisioned count to avoid collisions
        start_index = len(self._store.list_provisioned_instances())
        for i in range(shortfall):
            idx = start_index + i
            install_dir = self._provision_fn(idx, setup_path)
            self._store.add_provisioned_instance(install_dir)
            new_dirs.append(install_dir)
        return new_dirs

    def assign(self, accounts: list[dict]) -> dict[str, TerminalInstance]:
        """Assign one terminal instance per account. Accounts carrying a
        ``terminal_path`` (user override) keep it (tagged ``override``); the
        rest auto-assign from the available pool. Raises if there are not
        enough instances."""
        available = self.discover_all()
        pool = list(available)
        assigned: dict[str, TerminalInstance] = {}
        for acct in accounts:
            acct_id = acct["id"]
            override = acct.get("terminal_path")
            if override:
                # normalize to a TerminalInstance; user took responsibility
                install_dir = str(Path(override).parent)
                assigned[acct_id] = TerminalInstance(
                    install_dir=install_dir, exe_path=override,
                    source="override")
                # remove a matching pool entry so it is not double-assigned
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_terminal_manager.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/terminal/manager.py manager/tests/test_terminal_manager.py
git commit -m "feat(terminal): TerminalManager discover+provision+assign+kill_terminal"
```

---

## Task 6: Supervisor readiness gate + restart backoff (`supervisor.py`)

**Files:**
- Modify: `manager/supervisor.py`
- Test: `manager/tests/test_supervisor_readiness.py` (new file; the existing `test_supervisor.py` stays green and unchanged)

**Interfaces:**
- Consumes: the existing `Supervisor` (Plan 2). No new external deps. Pure-supervisor change; the engine stays untouched.
- Produces (new on `WorkerHandle`): `got_symbol_info: bool = False`, `got_status: bool = False`, `restart_count: int = 0`, `next_restart_at: float = 0.0`.
- Produces (new on `Supervisor`):
  - `slave_ready(slave_id) -> bool` — True iff that slave's handle has both `got_symbol_info` and `got_status`.
  - `wait_for_slaves_ready(timeout: float = 10.0, slave_ids: list[str] | None = None) -> bool` — ticks `self.tick()` until every spawned slave (or the given ids) is ready or the timeout elapses. Returns whether all are ready. Safe to call before `spawn_master`.
  - Restart backoff: `_restart(name)` now applies an exponential delay before respawning a *dead* worker: `delay = min(BASE_BACKOFF * 2**restart_count, MAX_BACKOFF)`; it sets `next_restart_at = now + delay`, increments `restart_count`, and if `now < next_restart_at` it skips the respawn this tick (the dead handle stays; the next tick retries). Any received message resets `restart_count = 0` and `next_restart_at = 0.0` (recovery). Constants: `BASE_BACKOFF = 1.0`, `MAX_BACKOFF = 30.0` (seconds), exposed as class attributes so tests can override via subclass or monkeypatch.

**Why the readiness gate (Plan 2 deferred MUST #1):** the master sends its first `SnapshotMsg` immediately on startup; a freshly-spawned slave does more init (recovery + symbol-info + status) and loses the race. If the master's first snapshot is ingested before the slave's `SymbolInfoMsg`, a `NEW` is skipped for no-info AND `ingest_snapshot` advances the shared `_prev` past it → the position is permanently never copied. The gate makes spawn-order deterministic: slaves first → wait for SI+Status → master.

- [ ] **Step 1: Write the failing test**

```python
# manager/tests/test_supervisor_readiness.py
import time

from manager.engine.models import SymbolInfo, BUY, Position
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.supervisor import Supervisor, WorkerHandle

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = int(time.time())


def _engine():
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                              step_amount=100.0, step_size=0.01, max_lot=10.0,
                              max_trade_age_minutes=999999, normalize_sltp=True))
    return eng


def _slave_cfg():
    return {"slave_id": "s1", "terminal_path": "C:/t/s.exe", "login": 2,
            "server": "Demo", "symbol_map_csv": "EURUSD=EURUSD",
            "normalize_sltp": True, "retry_count": 1, "retry_delay_ms": 0,
            "slave_status_interval_ms": 60000}


def _slave_state():
    return {"symbol_infos": {"EURUSD": SI},
            "account": {"login": 2, "balance": 1000.0, "equity": 1000.0,
                        "currency": "USD", "server": "Demo"},
            "ticks": {"EURUSD": (1.10000, 1.10010)}}


def _tick_until(sup, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sup.tick(timeout=0.02)
        if predicate():
            return True
    return predicate()


def test_slave_ready_requires_symbol_info_and_status():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), "pw", adapter_kind="fake",
                    fake_state=_slave_state())
    try:
        # not ready immediately after spawn
        assert not sup.slave_ready("s1")
        ok = _tick_until(sup, lambda: sup.slave_ready("s1"))
        assert ok, "slave never became ready (SI + Status)"
        assert sup.slave_ready("s1")
    finally:
        sup.shutdown()


def test_wait_for_slaves_ready_returns_true_when_ready():
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    sup.spawn_slave("s1", _slave_cfg(), "pw", adapter_kind="fake",
                    fake_state=_slave_state())
    try:
        assert sup.wait_for_slaves_ready(timeout=5.0)
        assert sup.slave_ready("s1")
    finally:
        sup.shutdown()


def test_wait_for_slaves_ready_times_out_when_status_missing():
    """A slave that reported SymbolInfo but never Status (e.g. crashed
    mid-init) must cause wait_for_slaves_ready to return False, not hang.
    Pure unit test: construct the handle directly so the outcome is
    deterministic rather than racing a real fake worker's init."""
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.0)
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        password="", adapter_kind="fake", fake_state=None,
        got_symbol_info=True, got_status=False, last_msg_ts=time.time())
    # _StubProc is alive and last_msg_ts is now, so _health_check never
    # restarts; tick is a fast no-op. The gate polls until the wall-clock
    # timeout elapses and returns False because got_status stays False.
    ok = sup.wait_for_slaves_ready(timeout=0.1)
    assert ok is False


def test_readiness_gate_prevents_permanent_skip_of_first_snapshot():
    """Plan 2 deferred MUST #1: with the readiness gate, the master is spawned
    AFTER slaves are ready, so the first NEW is not skipped for no-info."""
    eng = _engine()
    sup = Supervisor(eng, poll_timeout=0.02)
    master_state = {
        "positions": [Position(42, "EURUSD", BUY, 1.10000, 0.5, 1.095, 1.105,
                               NOW, 0.00001, "")],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    sup.spawn_slave("s1", _slave_cfg(), "pw", adapter_kind="fake",
                    fake_state=_slave_state())
    assert sup.wait_for_slaves_ready(timeout=5.0)
    sup.spawn_master({"terminal_path": "C:/t/m.exe", "login": 1,
                      "server": "Demo", "master_interval_ms": 20}, "pw",
                     adapter_kind="fake", fake_state=master_state)
    try:
        ok = _tick_until(
            sup,
            lambda: eng._slaves["s1"].table.get(42) is not None
            and eng._slaves["s1"].table.get(42).slave_ticket != 0)
        assert ok, "first OPEN was skipped (readiness gate did not prevent the race)"
    finally:
        sup.shutdown()


class _FakeNow:
    """Mutable clock for backoff tests."""
    def __init__(self, t=0.0):
        self.t = t
    def __call__(self):
        return self.t


class _StubProc:
    def __init__(self): self._alive = True
    def is_alive(self): return self._alive
    def terminate(self): self._alive = False
    def join(self, timeout=None): pass


def test_restart_backoff_delays_respawn_of_dead_worker():
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=1000.0, consecutive_failures=5,
                     poll_timeout=0.0, time_fn=clock)
    sup.MAX_BACKOFF = 30.0
    sup.BASE_BACKOFF = 1.0
    spawned = []

    def fake_spawn(name, role, config, password, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                         config=config, password=password,
                         adapter_kind=adapter_kind, fake_state=fake_state,
                         last_msg_ts=clock.t)
        spawned.append(h)
        return h
    sup._spawn = fake_spawn
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        password="", adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    # kill it
    sup._handles["s1"].proc.terminate()
    assert not sup._handles["s1"].proc.is_alive()
    sup._restart("s1")
    assert spawned, "first restart should spawn immediately"
    first = spawned[-1]
    # kill again -> should schedule backoff, NOT spawn this time
    first.proc.terminate()
    clock.t = 0.5  # < next_restart_at (1.0) -> skip
    count_before = len(spawned)
    sup._restart("s1")
    assert len(spawned) == count_before, "restart should be skipped within backoff window"
    # advance past backoff -> spawns
    clock.t = 1.5
    sup._restart("s1")
    assert len(spawned) == count_before + 1


def test_restart_backoff_resets_on_message():
    clock = _FakeNow(0.0)
    eng = _engine()
    sup = Supervisor(eng, stale_seconds=1000.0, consecutive_failures=5,
                     poll_timeout=0.0, time_fn=clock)
    sup.MAX_BACKOFF = 30.0
    sup.BASE_BACKOFF = 1.0
    spawned = []

    def fake_spawn(name, role, config, password, adapter_kind, fake_state):
        h = WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None,
                         config=config, password=password,
                         adapter_kind=adapter_kind, fake_state=fake_state,
                         last_msg_ts=clock.t)
        spawned.append(h)
        return h
    sup._spawn = fake_spawn
    sup._handles["s1"] = WorkerHandle(
        name="s1", role="slave", proc=_StubProc(), pipe=None, config={},
        password="", adapter_kind="fake", fake_state=None, last_msg_ts=0.0)
    # First death: immediate respawn; backoff (1.0s) scheduled for the NEXT death.
    sup._handles["s1"].proc.terminate()
    sup._restart("s1")
    assert len(spawned) == 1
    first = spawned[-1]
    assert first.restart_count == 1 and first.next_restart_at == 1.0
    # A message arrives on the new worker -> backoff resets to zero.
    first.restart_count = 0
    first.next_restart_at = 0.0
    # Second death: only 0.5s later, but backoff was reset so respawn is
    # immediate (no skip). Without the reset, 0.5 < 1.0 would have skipped.
    first.proc.terminate()
    clock.t = 0.5
    sup._restart("s1")
    assert len(spawned) == 2
    # And the next backoff window is the BASE (1.0s), not doubled (2.0s),
    # because restart_count was 0 going in.
    second = spawned[-1]
    assert second.restart_count == 1
    assert second.next_restart_at == 1.5  # 0.5 + base 1.0, not 0.5 + 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_supervisor_readiness.py -v`
Expected: FAIL — `Supervisor` has no `slave_ready` / `wait_for_slaves_ready` and `WorkerHandle` lacks the new fields.

- [ ] **Step 3: Write minimal implementation**

Modify `manager/supervisor.py`. The changes are additive (new fields default-initialized, new methods, two edits to existing methods). Apply them precisely:

**(a) `WorkerHandle` — add readiness + backoff fields** (after `fake_state: dict | None` and before `last_msg_ts`):

```python
@dataclass
class WorkerHandle:
    name: str
    role: str
    proc: multiprocessing.Process
    pipe: object
    config: dict
    password: str
    adapter_kind: str
    fake_state: dict | None
    got_symbol_info: bool = False
    got_status: bool = False
    restart_count: int = 0
    next_restart_at: float = 0.0
    last_msg_ts: float = 0.0
    fail_count: int = 0
```

**(b) `Supervisor` — class attributes for backoff + the readiness-gate methods.** Add near the top of the class body (after the docstring, before `__init__`):

```python
    BASE_BACKOFF = 1.0   # seconds; first respawn delay after a death
    MAX_BACKOFF = 30.0   # seconds; cap on exponential backoff
```

**(c) `__init__` — accept an optional `time_fn`-driven clock already stored as `self._time_fn`; no signature change needed.** (The existing `time_fn=time.time` param is reused.)

**(d) `_dispatch_slave` — set readiness flags + reset backoff on any message.** Replace the body of `_dispatch_slave` so SymbolInfoMsg sets `got_symbol_info`, StatusMsg sets `got_status`, and every dispatched message resets `restart_count`/`next_restart_at`:

```python
    def _dispatch_slave(self, slave_id, msg) -> None:
        h = self._handles.get(slave_id)
        if h is not None:
            h.got_symbol_info = h.got_symbol_info or isinstance(msg, SymbolInfoMsg)
            h.got_status = h.got_status or isinstance(msg, StatusMsg)
            h.restart_count = 0
            h.next_restart_at = 0.0
        if isinstance(msg, AckMsg):
            for cmd in self._engine.apply_ack(slave_id, msg):
                self._send(slave_id, cmd)
        elif isinstance(msg, StatusMsg):
            self._engine.apply_status(slave_id, msg)
        elif isinstance(msg, RecoveryMsg):
            self._engine.apply_recovery(slave_id, msg.records)
        elif isinstance(msg, SymbolInfoMsg):
            self._engine.apply_symbol_info(slave_id, msg.infos)
        elif isinstance(msg, ErrorMsg):
            self.errors.append(f"{slave_id}: {msg.message}")
```

**(e) `_read_master` — reset backoff on master message.** In `_read_master`, after `h.last_msg_ts = self._time_fn()` and `h.fail_count = 0`, add:

```python
            h.restart_count = 0
            h.next_restart_at = 0.0
```

**(f) New readiness-gate methods** (add after `spawn_slave`):

```python
    def slave_ready(self, slave_id) -> bool:
        """A slave is ready once it has reported BOTH SymbolInfoMsg and its
        first StatusMsg (balance). Until then, spawning the master risks the
        startup-race permanent-skip (Plan 2 deferred MUST #1)."""
        h = self._handles.get(slave_id)
        return h is not None and h.got_symbol_info and h.got_status

    def wait_for_slaves_ready(self, timeout: float = 10.0,
                              slave_ids: list[str] | None = None) -> bool:
        """Tick until every spawned slave (or the given ids) is ready or the
        timeout elapses. Call BEFORE spawn_master. Returns whether all are
        ready. Safe when no master is spawned yet (_read_master is a no-op
        when the master handle is absent)."""
        ids = slave_ids if slave_ids is not None \
            else [n for n, h in self._handles.items() if h.role == "slave"]
        if not ids:
            return True
        deadline = self._time_fn() + timeout
        while self._time_fn() < deadline:
            self.tick(timeout=0.02)
            if all(self.slave_ready(i) for i in ids):
                return True
        return all(self.slave_ready(i) for i in ids)
```

**(g) `_restart` — exponential backoff.** Replace `_restart` with:

```python
    def _restart(self, name) -> None:
        h = self._handles.get(name)
        if h is None:
            return
        now = self._time_fn()
        # Backoff: a *dead* worker is not respawned until next_restart_at.
        # A *stale-but-alive* worker (consecutive stale failures) is restarted
        # immediately — the process is hung, not dead, so there is no spawn
        # storm to dampen.
        if not h.proc.is_alive():
            if now < h.next_restart_at:
                return  # still in the backoff window; retry next tick
            self.errors.append(f"restarting {name}")
        else:
            self.errors.append(f"restarting {name} (stale)")
        if h.proc.is_alive():
            h.proc.terminate()
            h.proc.join(timeout=2.0)
        if h.pipe is not None:
            try:
                h.pipe.close()
            except Exception:
                pass
        if self._kill_terminal is not None:
            try:
                self._kill_terminal(h.config.get("terminal_path", ""))
            except Exception:
                pass
        if h.role == "slave":
            self._engine.reset_slave(name)  # recovery re-seeds on reconnect
        # Schedule the backoff for the NEXT death from the CURRENT count,
        # then carry that state onto the freshly-spawned handle (the new
        # handle defaults to restart_count=0/next_restart_at=0.0, so without
        # carrying it over a respawn would reset the backoff and a death storm
        # would never throttle).
        delay = min(self.BASE_BACKOFF * (2 ** h.restart_count), self.MAX_BACKOFF)
        new_count = h.restart_count + 1
        new_next = now + delay
        new_h = self._spawn(name, h.role, h.config, h.password,
                            h.adapter_kind, h.fake_state)
        new_h.restart_count = new_count
        new_h.next_restart_at = new_next
        self._handles[name] = new_h
        if self.on_restart:
            self.on_restart(name, h.role)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest manager/tests/test_supervisor_readiness.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Run the existing supervisor tests + full suite**

Run: `pytest manager/tests/test_supervisor.py manager/tests/test_supervisor_readiness.py -v`
Expected: PASS — the existing 6 supervisor tests stay green (the `WorkerHandle` construction in `test_consecutive_stale_failures_restart` and `test_message_resets_fail_count` uses kwargs that still work because the new fields have defaults; the existing `_restart`-driving tests use a *live* `_StubProc` (`is_alive()` returns True), so the dead-process backoff branch is not taken — stale restarts remain immediate, preserving prior behavior).

Run: `pytest manager/tests -q`
Expected: PASS (full suite green).

- [ ] **Step 6: Commit**

```bash
git add manager/supervisor.py manager/tests/test_supervisor_readiness.py
git commit -m "feat(supervisor): startup readiness gate + exponential restart backoff"
```

---

## Self-Review

**1. Spec coverage.** Checked against the spec's Terminal instance management, Critical constraints, Architecture (manager-side components), Data flow (startup), Error handling, Project structure, and Tech stack sections, plus Plan 2's deferred MUSTs.

- *Discovery via origin.txt (UTF-16 first line = install dir) + default `C:\Program Files\MetaTrader 5\`* → Task 3 (`discover_terminals`, source `appdata`/`default`). ✅
- *Provisioning via `mt5setup.exe /auto /path:"<custom path>"` to `%LOCALAPPDATA%\CopyTradesMT5\terminals\instance_<n>`, portable=True, web installer needs internet* → Task 4 (`provision_command`, `provision_instance`, `download_setup`). ✅ (portable=True is set by the worker from `config["portable"]`; the manager tags provisioned instances and Plan 4 sets `portable=True` in the config for provisioned instances — noted in the manager's `assign` contract: provisioned instances carry `source="provisioned"` which Plan 4 maps to `portable=True`.)
- *Assignment one instance per account, auto + user override* → Task 5 (`assign`, `source="override"`). ✅
- *Visibility — windows not force-hidden* → Not a Plan 3 code concern (the worker launches the terminal; Plan 4 does not hide). No task needed. ✅ (documented in Global Constraints.)
- *DPAPI credentials at rest via pywin32 win32crypt, per-user* → Task 1. ✅
- *Settings store under `%APPDATA%\CopyTradesMT5\`* → Task 2 (`_default_path`). ✅
- *Project structure files: settings/credentials.py, settings/store.py, terminal/discovery.py, terminal/provisioning.py, terminal/manager.py* → Tasks 1-5 create exactly these. ✅
- *Tech stack: MetaTrader5, pywin32 win32crypt, pytest* → pywin32 in Task 1; psutil added (used for kill-by-exe-path, the research-recommended approach); MetaTrader5 unchanged. ✅
- *Error handling: worker crash → kill leftover terminal64.exe → respawn, restart backoff, credential errors → DPAPI decrypt failure prompts re-enter* → kill via Task 5 `kill_terminal` (wired by Plan 4 into the Supervisor's existing hook); restart backoff via Task 6; DPAPI decrypt failure → `CredentialDecryptError` (Task 1) caught by Plan 4 GUI to re-prompt. ✅
- *Plan 2 deferred MUST #1 (startup race permanent-skip) → readiness gate* → Task 6 (`slave_ready`, `wait_for_slaves_ready`), pinned by `test_readiness_gate_prevents_permanent_skip_of_first_snapshot`. ✅
- *Plan 2 deferred: kill_terminal + on_restart hooks wired + restart backoff* → kill_terminal provided by Task 5 (Plan 4 wires it into `Supervisor(kill_terminal=...)`); on_restart already in Plan 2; backoff in Task 6. ✅
- *Plan 2 deferred #2 (master-death restart)* → already FIXED in Plan 2 (db55d20); Task 6's backoff preserves it (dead master is respawned with backoff, and `_read_master` returns True on EOF from the Plan 2 fix — unchanged here). ✅

- *Testability property (spec line 216): MetaTrader5 calls isolated behind the adapter; engine never touches the terminal* → preserved. All new I/O (crypto, filesystem, subprocess, process-kill) lives in `settings/` and `terminal/`, behind injectable seams. The supervisor change adds a gate + backoff but no new I/O. ✅

**2. Placeholder scan.** No TBD/TODO/"implement later"/"add appropriate". Every step has complete code or complete test code. The provisioning test's `if False else` is an intentional documented fallback, not a placeholder.

**3. Type / name consistency.** Cross-checked:
- `TerminalInstance(install_dir, exe_path, source)` — defined Task 3, reused Task 5. ✅
- `worker config["terminal_path"]` = exe path (Plan 2 contract) — `kill_terminal(exe_path)` matches it; `assign` returns `TerminalInstance.exe_path` for Plan 4 to put in `terminal_path`. ✅
- `SettingsStore.list_provisioned_instances` / `add_provisioned_instance` / `remove_provisioned_instance` — defined Task 2, consumed Task 5 (via the injected `store`). ✅
- `provision_instance(index, setup_path, install_root=None, ...)` signature — defined Task 4, the Task 5 `provision_fn` injection matches `(index, setup_path, install_root=None, **k)`. ✅
- `WorkerHandle` new fields (`got_symbol_info`, `got_status`, `restart_count`, `next_restart_at`) — added in Task 6, used in `_dispatch_slave`, `_read_master`, `slave_ready`, `_restart`. ✅
- `Supervisor.BASE_BACKOFF` / `MAX_BACKOFF` — class attributes, overridden in backoff tests via instance assignment. ✅

**4. Potential gotchas flagged for implementers.**
- Task 4 test `test_provision_instance_runs_installer_and_waits_for_exe`: the fake runner extracts the install dir from `cmd[2][len("/path:"):]` — `provision_command` guarantees `cmd = [setup_path, "/auto", "/path:<dir>"]`, so `cmd[2]` is the `/path:` element. Keep that extraction.
- Task 6 `test_wait_for_slaves_ready_times_out_when_status_missing`: a pure unit test — it constructs a `WorkerHandle` directly with `got_symbol_info=True, got_status=False` and asserts the gate returns False within a 0.1s wall timeout. Do not rewrite it to use a real fake worker (that races the fake's init sequence); the direct-handle form is the deterministic contract.
- Task 6 backoff tests use `sup._spawn = fake_spawn` (monkeypatch) and `_StubProc` — same pattern as the existing `test_consecutive_stale_failures_restart`, so the implementer should mirror that test's structure.

No issues found that require a plan edit. The plan is complete.

---

## Forward-looking (NOT in this plan — for Plan 4: GUI + tray + wiring + smoke runbook)

- The GUI's Start action: `TerminalManager.discover_all()` → populate dropdowns → `provision_shortfall(num_slaves)` with a progress indicator → `assign(accounts)` → for each provisioned instance set `config["portable"] = True` and `config["terminal_path"] = assigned.exe_path` → `Supervisor(kill_terminal=terminal_manager.kill_terminal)` → `spawn_slave` per slave → `wait_for_slaves_ready()` → `spawn_master` → `start()`.
- Credential re-prompt: catch `CredentialDecryptError` from `credentials.decrypt_password` in the GUI and re-show the login form for that account.
- Smoke runbook (`docs/`): demo accounts only, two real demo terminals, verify the readiness gate logs "slave ready" before the master's first snapshot, verify kill_terminal clears a `-10003` after a forced terminal crash.
- On-demand symbol-info request for same-name-fallback symbols not in the symbol map (the `build_symbol_info_msg` forward-looking note from Plan 2).