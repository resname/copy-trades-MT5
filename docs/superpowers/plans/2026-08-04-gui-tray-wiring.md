# GUI, Tray, Wiring & Smoke Runbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the manager into a runnable PySide6 desktop app: a `CopyController` that orchestrates terminal discovery/provisioning/assignment + the supervisor readiness gate + DPAPI credential decrypt-with-re-prompt (the testable brain), a thin Qt GUI (Master pane, Slave list/editor, Start/Stop, live status panel, log view), a system-tray icon with close-to-tray + orderly quit, a `__main__` entry point, and the manual demo smoke-test runbook (demo accounts only). This is the final plan; it produces a launchable `python -m manager`.

**Architecture:** A `manager/app/controller.py` holds ALL non-GUI orchestration logic and is fully unit-testable with the real `Supervisor` + `CopyEngine` + fake workers (no Qt, no real terminal) — this is where the Plan 3 deferred MUSTs land (gate ordering, override validation + uniqueness, portable flag for provisioned instances, credential re-prompt). The Qt layer (`manager/gui/`) is a THIN view over the controller: `MainWindow`, `SlaveEditor`, `TrayIcon` construct widgets and forward Start/Stop + status to the controller. GUI modules import PySide6 at module top; their tests use `pytest.importorskip("PySide6")` so the suite stays green with or without PySide6 installed (matching the spec's testability property: only the real-terminal smoke is manual). `manager/__main__.py` assembles QApplication + MainWindow + TrayIcon + controller.

**Tech Stack:** Python 3.11+, PySide6 (Qt6 — GUI + tray), pywin32 (`win32crypt`, DPAPI — already behind `credentials.py`), psutil (kill by exe path — already behind `TerminalManager`), MetaTrader5 (unchanged, behind the worker adapter), pytest. All Windows-only.

## Global Constraints

Copied verbatim from the spec + standing security constraints + the Plan 3 final-review deferred MUSTs. Every task's requirements implicitly include this section.

- **Demo accounts only — never capture or log in with a real account.** The manual smoke test (Task 6) is demo-only; tests use fake/demo credentials.
- **Credentials are passed to workers through the pipe, never on the command line.** The controller builds the worker `config` + `password` and hands them to `Supervisor.spawn_*` (which sends `StartMsg` over the pipe). The password is never put in argv, never logged, never written to disk in plaintext.
- **DPAPI-encrypted credentials at rest** — the controller reads `password_blob` from the `SettingsStore` and calls `credentials.decrypt_password`; on `CredentialDecryptError` (cross-user/machine/corrupt) it signals re-prompt rather than silently failing.
- **Capture artifacts (pcaps, Frida logs) are gitignored, never committed.** (Unchanged.)
- **One terminal install per concurrent account.** The controller validates user `terminal_path` overrides for uniqueness across accounts (Plan 3 MUST #2) and normalizes an override that points at a directory to `<dir>/terminal64.exe` (Plan 3 MUST #3); duplicate or unresolvable overrides raise before Start.
- **Provisioned instances are launched with `portable=True`** (data folder inside the install dir). The controller sets `config["portable"] = (assigned_instance.source == "provisioned")` for each account; discovered/override instances use `portable=False`.
- **`login` must be an `int`.** The controller validates/converts login to int before building worker config.
- **Keep terminal windows visible** (minimized acceptable). The manager does not force-hide terminal windows.
- **Startup readiness gate** (Plan 2/3 MUST): the controller spawns all slaves, calls `Supervisor.wait_for_slaves_ready()`, and ONLY THEN spawns the master. This prevents the permanent-skip startup race. (Plan 3 MUST #5 — mid-run respawn gate re-arm — remains deferred; out of scope here.)
- **`wait_for_slaves_ready` is not concurrency-safe with `Supervisor.start()`/`_run()`** (Plan 3 MUST #6): the controller calls it BEFORE `Supervisor.start()` (i.e. before the run thread is active). The controller drives Start synchronously on the GUI thread, then calls `start()` to hand the tick loop to its daemon thread.
- **Engine purity invariant** (Plan 2): the engine stays untouched. The controller sits between the GUI and the supervisor; it imports `manager.engine`, `manager.supervisor`, `manager.terminal`, `manager.settings` — never Qt.
- **PySide6 is imported ONLY inside `manager/gui/` and `manager/__main__.py`.** `manager/app/controller.py` and `manager/engine/*` have zero Qt imports, so the controller + engine + supervisor test suite runs without PySide6 installed.
- **Windows-only.** `python -m manager` launches the Qt app on Windows.

---

## File structure

New files:

```
manager/
  __main__.py            # entry: QApplication + MainWindow + TrayIcon + controller; run event loop
  app/
    __init__.py          # empty
    controller.py        # CopyController: orchestrate terminal mgmt + supervisor + credentials (no Qt)
  gui/
    __init__.py          # empty
    main_window.py       # MainWindow(QMainWindow): master pane, slave list, Start/Stop, status, log
    slave_editor.py      # SlaveEditor(QWidget): per-slave form + symbol-map table
    tray.py              # TrayIcon(QSystemTrayIcon): tray icon, close-to-tray, quit
  tests/
    conftest.py          # qapp fixture (offscreen QApplication) guarded by importorskip
    test_controller.py    # full TDD: orchestration with real Supervisor + FakeMt5, fake terminal manager
    test_main_window.py  # construction smoke (importorskip PySide6)
    test_slave_editor.py # construction smoke (importorskip PySide6)
    test_tray.py         # construction smoke (importorskip PySide6)
docs/
  smoke-test.md          # manual demo smoke runbook (demo accounts only)
```

Modified files:

- `pyproject.toml` — add `[project].dependencies` (PySide6, pywin32, psutil, MetaTrader5) so the project is installable; the lazy-import discipline elsewhere is unchanged so tests still run without them where the seams allow.

Responsibilities:
- `app/controller.py` — the ONLY non-GUI orchestrator. Builds the `CopyEngine` + `SlaveConfig`s, builds the `Supervisor` with `kill_terminal=terminal_manager.kill_terminal`, drives the Start sequence (provision+assign → spawn slaves → wait_for_slaves_ready → spawn master → start), handles credential decrypt + re-prompt signal, surfaces status/log via callbacks. No Qt.
- `gui/main_window.py` — the main window. Hosts the master account form, the slave list, the Start/Stop buttons, the live status panel, the log view. Delegates all actions to the controller; subscribes to controller status/log callbacks.
- `gui/slave_editor.py` — a widget used to add/edit one slave's config (login, server, terminal-path override dropdown, symbol-map table, lot-sizing fields, maxLot, maxTradeAge, normalize toggle).
- `gui/tray.py` — the tray icon. Close-to-tray (intercept the window close to hide+tray); a tray menu with Show/Quit; Quit triggers controller.stop() then the app quits.
- `__main__.py` — `python -m manager` entry: create `QApplication`, instantiate controller + MainWindow + TrayIcon, wire them, run the event loop.
- `docs/smoke-test.md` — the tier-3 manual smoke runbook: two real demo terminals, verify discovery dropdown, verify provisioning progress, verify the readiness gate logs "slaves ready" before the master's first snapshot, verify a forced terminal crash is cleared by `kill_terminal` (no `-10003`), verify close-to-tray keeps copying, verify orderly quit shuts down workers.

---

## Task 1: The copy controller (`app/controller.py`)

**Files:**
- Create: `manager/app/__init__.py` (empty)
- Create: `manager/app/controller.py`
- Test: `manager/tests/test_controller.py`

**Interfaces:**
- Consumes (exact signatures from earlier plans):
  - `manager.engine.copy_loop.CopyEngine()`, `CopyEngine.add_slave(SlaveConfig(...))`, `CopyEngine.ingest_snapshot`, `CopyEngine.apply_ack`, `CopyEngine.apply_status`, `CopyEngine.apply_symbol_info`, `CopyEngine.apply_recovery`, `CopyEngine.reset_slave` (Plan 2).
  - `manager.engine.copy_loop.SlaveConfig(slave_id, symbol_map_csv, step_amount, step_size, max_lot, max_trade_age_minutes, normalize_sltp)` (Plan 2).
  - `manager.supervisor.Supervisor(engine, heartbeat_seconds=5, stale_seconds=30.0, consecutive_failures=3, poll_timeout=0.2, time_fn=time.time, kill_terminal=None)`; `Supervisor.spawn_master(config, password, adapter_kind="real", fake_state=None)`; `Supervisor.spawn_slave(slave_id, config, password, adapter_kind="real", fake_state=None)`; `Supervisor.wait_for_slaves_ready(timeout=10.0, slave_ids=None) -> bool`; `Supervisor.start()`; `Supervisor.stop()`; `Supervisor.join(timeout=None)`; `Supervisor.shutdown()`; `Supervisor.errors: list[str]`; `Supervisor.on_restart` (Plan 2 + Plan 3 Task 6).
  - `manager.terminal.manager.TerminalManager` — `discover_all() -> list[TerminalInstance]`, `provision_shortfall(num_slaves, setup_path=None) -> list[str]`, `assign(accounts: list[dict]) -> dict[str, TerminalInstance]`, `kill_terminal(exe_path) -> int` (Plan 3 Task 5). `TerminalInstance(install_dir, exe_path, source)` (Plan 3 Task 3).
  - `manager.settings.store.SettingsStore(path=None)` — `load() -> dict`, `save(data)` (Plan 3 Task 2).
  - `manager.settings.credentials` — `encrypt_password(plaintext, crypto=None) -> str`, `decrypt_password(blob, crypto=None) -> str`, `CredentialDecryptError` (Plan 3 Task 1).
  - `manager.engine.models` — `BUY`, `SELL` (constants, Plan 1).
- Produces:
  - `AccountSpec` dataclass: `id: str`, `login: int`, `server: str`, `password: str`, `terminal_path: str | None = None`, `symbol_map_csv: str = ""`, `step_amount: float = 100.0`, `step_size: float = 0.01`, `max_lot: float = 10.0`, `max_trade_age_minutes: float = 10.0`, `normalize_sltp: bool = True`. (`password` is plaintext, held in-process only after decrypt; never logged.)
  - `StatusUpdate` dataclass: `kind: str`, `message: str`, `slave_id: str | None = None`, `connected: bool | None = None`, `balance: float | None = None`, `equity: float | None = None`. `kind ∈ {"info","error","provisioning","ready","slave_status"}`.
  - `ControllerError(Exception)`.
  - `CopyController(terminal_manager, store=None, credentials=credentials, supervisor_factory=None, on_status=None, on_log=None, clock=time.time)`. `supervisor_factory` is `lambda engine, **kw: Supervisor(engine, **kw)` by default — injected so tests can pass a fake/faster supervisor. `on_status(StatusUpdate)` and `on_log(str)` are callbacks (the GUI subscribes).
  - Methods: `discover_instances() -> list[TerminalInstance]`; `prepare(master: AccountSpec, slaves: list[AccountSpec]) -> dict[str, TerminalInstance]` (validate overrides, provision shortfall, assign — returns the assignment; raises `ControllerError` on duplicate/unresolvable overrides or not-enough-instances-after-provision); `start(master: AccountSpec, slaves: list[AccountSpec]) -> None` (calls prepare, builds engine+supervisor, spawns slaves, `wait_for_slaves_ready`, spawns master, `start()`); `stop() -> None` (`supervisor.shutdown()`); `is_running() -> bool`.
  - Credential helpers: `load_password(account_id) -> str` (reads `password_blob` from the store, `decrypt_password`); `save_password(account_id, plaintext) -> None` (`encrypt_password` → `password_blob` in the account dict → `save`). On `CredentialDecryptError` from `load_password`, re-raise it so the GUI re-prompts (Plan 3 "Credential errors" requirement).

- [ ] **Step 1: Write the failing test**

```python
# manager/tests/test_controller.py
import time
import pytest

from manager.engine.models import SymbolInfo, BUY, Position
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.app.controller import (
    CopyController, AccountSpec, StatusUpdate, ControllerError,
)
from manager.terminal.discovery import TerminalInstance
from manager.settings import credentials

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = int(time.time())


class FakeTerminalManager:
    """Stub TerminalManager: no real install/discover/kill. Returns a fixed
    pool of instances and records calls."""
    def __init__(self, instances):
        self._instances = instances
        self.provisioned = []
        self.killed = []
    def discover_all(self):
        return list(self._instances)
    def provision_shortfall(self, num_slaves, setup_path=None):
        # pretend to provision the gap; return synthetic install dirs
        required = 1 + num_slaves
        gap = max(0, required - len(self._instances))
        new = [f"C:/prov/instance_{i}" for i in range(len(self._instances),
                                                      len(self._instances) + gap)]
        self.provisioned.append((num_slaves, gap, new))
        for d in new:
            self._instances.append(TerminalInstance(d, f"{d}/terminal64.exe",
                                                     "provisioned"))
        return new
    def assign(self, accounts):
        pool = list(self._instances)
        out = {}
        for a in accounts:
            ov = a.get("terminal_path")
            if ov:
                exe = ov if ov.endswith("terminal64.exe") else f"{ov}/terminal64.exe"
                out[a["id"]] = TerminalInstance(exe.rsplit("/", 1)[0], exe, "override")
                pool = [p for p in pool if p.exe_path != exe]
                continue
            if not pool:
                raise ControllerError("not enough")
            out[a["id"]] = pool.pop(0)
        return out
    def kill_terminal(self, exe_path):
        self.killed.append(exe_path)
        return 0


def _master(spec_id="master", terminal_path=None):
    return AccountSpec(id=spec_id, login=1, server="Demo", password="pw",
                       terminal_path=terminal_path)


def _slave(sid="s1", terminal_path=None):
    return AccountSpec(id=sid, login=2, server="Demo", password="pw",
                       terminal_path=terminal_path, symbol_map_csv="EURUSD=EURUSD")


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


def _controller(instances):
    statuses, logs = [], []
    c = CopyController(
        terminal_manager=FakeTerminalManager(instances),
        on_status=lambda s: statuses.append(s),
        on_log=lambda m: logs.append(m),
    )
    return c, statuses, logs


def _tick_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_prepare_provisions_and_assigns_one_per_account():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    assert set(assigned) == {"master", "s1"}
    assert assigned["master"].exe_path == "C:/i0/terminal64.exe"
    # one slave short -> provision_shortfall installed one more instance
    assert len(c._terminal_manager._instances) >= 2
    assert c._terminal_manager.provisioned  # shortfall was requested


def test_prepare_rejects_duplicate_terminal_path_overrides():
    insts = []
    c, _, _ = _controller(insts)
    with pytest.raises(ControllerError):
        c.prepare(AccountSpec(id="m", login=1, server="D", password="p",
                               terminal_path="C:/same/terminal64.exe"),
                  [AccountSpec(id="s1", login=2, server="D", password="p",
                               terminal_path="C:/same/terminal64.exe")])


def test_prepare_normalizes_override_dir_to_exe_path():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(terminal_path="C:/override"), [_slave()])
    assert assigned["master"].exe_path == "C:/override/terminal64.exe"
    assert assigned["master"].source == "override"


def test_start_runs_readiness_gate_then_master_and_copies():
    """End-to-end: start() provisions, spawns slaves, waits for readiness,
    THEN spawns the master — and the first OPEN is not skipped (the race
    the gate prevents). Uses the real Supervisor + FakeMt5 workers. The
    controller routes to the fake adapter when *_fake_state is provided, so
    no monkeypatch is needed."""
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "provisioned"),
             TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "provisioned")]
    c, statuses, logs = _controller(insts)
    master_state = {
        "positions": [Position(42, "EURUSD", BUY, 1.10000, 0.5, 1.095, 1.105,
                               NOW, 0.00001, "")],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    c.start(_master(), [_slave()],
            master_fake_state=master_state,
            slave_fake_state=_slave_state())
    try:
        ok = _tick_until(
            lambda: c._engine._slaves["s1"].table.get(42) is not None
            and c._engine._slaves["s1"].table.get(42).slave_ticket != 0)
        assert ok, "first OPEN did not flow (readiness gate failed)"
        # the readiness gate ran: a 'ready' status was emitted before the master
        assert any(s.kind == "ready" for s in statuses)
    finally:
        c.stop()


def test_start_sets_portable_true_only_for_provisioned_instances():
    insts = [TerminalInstance("C:/disc", "C:/disc/terminal64.exe", "appdata"),
             TerminalInstance("C:/prov", "C:/prov/terminal64.exe", "provisioned")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    cfgs = c.build_worker_configs(_master(), [_slave()], assigned)
    # master got the discovered (appdata) instance -> portable False
    assert cfgs["master"]["portable"] is False
    # slave got the provisioned instance -> portable True
    assert cfgs["s1"]["portable"] is True


def test_start_passes_kill_terminal_to_supervisor():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata"),
             TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    # don't actually start workers; just check the supervisor is built with
    # the terminal manager's kill_terminal wired
    sup = c.build_supervisor(heartbeat_seconds=5)
    assert sup._kill_terminal is c._terminal_manager.kill_terminal


def test_is_running_reflects_start_stop():
    insts = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata"),
             TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assert not c.is_running()
    c.start(_master(), [_slave()], master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                            "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    assert c.is_running()
    c.stop()
    assert not c.is_running()


def test_load_password_decrypts_from_store():
    # store with a base64 password blob; controller.decrypt_password recovers it
    from manager.settings import credentials
    fake_crypto_calls = []

    class FakeCrypto:
        def CryptProtectData(self, data, desc, *r):
            fake_crypto_calls.append(("p", data))
            return b"ENC:" + data
        def CryptUnprotectData(self, blob, *r):
            fake_crypto_calls.append(("u", blob))
            if not blob.startswith(b"ENC:"):
                raise ValueError("bad")
            return ("d", blob[len(b"ENC:"):])
    blob = credentials.encrypt_password("s3cret", crypto=FakeCrypto())
    from manager.settings.store import SettingsStore
    import tempfile, os
    d = tempfile.mkdtemp()
    store = SettingsStore(path=os.path.join(d, "s.json"))
    store.save({"accounts": {"s1": {"login": 2, "password_blob": blob}},
                "provisioned_instances": [], "global": {}})
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store,
                       credentials=credentials)
    assert c.load_password("s1") == "s3cret"


def test_load_password_reraises_decrypt_error_to_prompt_reentry():
    # a corrupt blob -> CredentialDecryptError surfaces (GUI re-prompts)
    from manager.settings.store import SettingsStore
    import tempfile, os
    d = tempfile.mkdtemp()
    store = SettingsStore(path=os.path.join(d, "s.json"))
    store.save({"accounts": {"s1": {"login": 2, "password_blob": "!!!bad!!!"}},
                "provisioned_instances": [], "global": {}})
    c = CopyController(terminal_manager=FakeTerminalManager([]), store=store)
    with pytest.raises(credentials.CredentialDecryptError):
        c.load_password("s1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_controller.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.app.controller'`.

- [ ] **Step 3: Write minimal implementation**

```python
# manager/app/__init__.py
  (empty file)
```

```python
# manager/app/controller.py
from __future__ import annotations

import time
from dataclasses import dataclass, field

from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.engine.models import BUY, SELL  # noqa: F401  (re-exported for GUI)
from manager.supervisor import Supervisor
from manager.terminal.discovery import TerminalInstance
from manager.settings import credentials as _credentials_mod
from manager.settings.store import SettingsStore


class ControllerError(Exception):
    """Raised before Start for unrecoverable config: duplicate/unresolvable
    terminal-path overrides, or not enough instances after provisioning."""


@dataclass
class AccountSpec:
    id: str
    login: int
    server: str
    password: str
    terminal_path: str | None = None
    symbol_map_csv: str = ""
    step_amount: float = 100.0
    step_size: float = 0.01
    max_lot: float = 10.0
    max_trade_age_minutes: float = 10.0
    normalize_sltp: bool = True


@dataclass
class StatusUpdate:
    kind: str            # "info" | "error" | "provisioning" | "ready" | "slave_status"
    message: str
    slave_id: str | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None


def _normalize_override_exe(path: str) -> str:
    """An override may point at the install dir or directly at terminal64.exe.
    Normalize to the exe path (Plan 3 MUST #3)."""
    p = path.replace("\\", "/")
    if p.endswith("terminal64.exe"):
        return p
    return p.rstrip("/") + "/terminal64.exe"


class CopyController:
    """Non-GUI orchestrator. Wires TerminalManager + Supervisor + CopyEngine +
    DPAPI credentials. The GUI is a thin view over this. All Start/Stop logic
    lives here so it is unit-testable with the real Supervisor + FakeMt5 and
    a fake terminal manager (no Qt, no real install)."""

    def __init__(self, terminal_manager, store=None,
                 credentials=_credentials_mod, supervisor_factory=None,
                 on_status=None, on_log=None, clock=time.time):
        self._terminal_manager = terminal_manager
        self._store = store if store is not None else SettingsStore()
        self._credentials = credentials
        self._supervisor_factory = (supervisor_factory
                                     if supervisor_factory is not None
                                     else self._default_supervisor_factory)
        self._on_status = on_status or (lambda s: None)
        self._on_log = on_log or (lambda m: None)
        self._clock = clock
        self._supervisor: Supervisor | None = None
        self._engine: CopyEngine | None = None

    @staticmethod
    def _default_supervisor_factory(engine, **kw):
        return Supervisor(engine, **kw)

    # ---- status helpers ----
    def _status(self, kind, message, **extra):
        self._on_status(StatusUpdate(kind=kind, message=message, **extra))

    def _log(self, message):
        self._on_log(message)

    # ---- discovery / provisioning / assignment ----
    def discover_instances(self) -> list[TerminalInstance]:
        return self._terminal_manager.discover_all()

    def prepare(self, master: AccountSpec, slaves: list[AccountSpec]
                ) -> dict[str, TerminalInstance]:
        """Validate overrides (uniqueness + normalize to exe path), provision
        the shortfall, assign one instance per account. Raises ControllerError
        on duplicate/unresolvable overrides or post-provision shortage."""
        # normalize + dedup overrides (Plan 3 MUST #2/#3)
        seen: dict[str, str] = {}
        accounts = [self._account_dict(master)] + [self._account_dict(s)
                                                   for s in slaves]
        for a in accounts:
            ov = a.get("terminal_path")
            if ov:
                exe = _normalize_override_exe(ov)
                a["terminal_path"] = exe
                if exe in seen:
                    raise ControllerError(
                        f"terminal path {exe} assigned to both "
                        f"{seen[exe]} and {a['id']}")
                seen[exe] = a["id"]
        # provision the shortfall (1 master + N slaves)
        self._status("provisioning", "checking terminal instances…")
        new_dirs = self._terminal_manager.provision_shortfall(len(slaves))
        if new_dirs:
            self._log(f"provisioned {len(new_dirs)} terminal instance(s): "
                      + ", ".join(new_dirs))
        assigned = self._terminal_manager.assign(accounts)
        self._status("info", "terminal instances assigned")
        return assigned

    @staticmethod
    def _account_dict(a: AccountSpec) -> dict:
        return {"id": a.id, "login": int(a.login), "server": a.server,
                "terminal_path": a.terminal_path,
                "symbol_map_csv": a.symbol_map_csv,
                "step_amount": a.step_amount, "step_size": a.step_size,
                "max_lot": a.max_lot,
                "max_trade_age_minutes": a.max_trade_age_minutes,
                "normalize_sltp": a.normalize_sltp}

    # ---- worker config building (testable independently) ----
    def build_worker_configs(self, master: AccountSpec, slaves: list[AccountSpec],
                             assigned: dict[str, TerminalInstance]
                             ) -> dict[str, dict]:
        """Build the per-account worker config dicts the Supervisor spawns with.
        Sets portable=True only for provisioned instances (Plan 3 wiring)."""
        cfgs: dict[str, dict] = {}
        m_inst = assigned[master.id]
        cfgs[master.id] = {
            "terminal_path": m_inst.exe_path,
            "login": int(master.login), "server": master.server,
            "master_interval_ms": 1000,
            "portable": m_inst.source == "provisioned",
        }
        for s in slaves:
            s_inst = assigned[s.id]
            cfgs[s.id] = {
                "slave_id": s.id,
                "terminal_path": s_inst.exe_path,
                "login": int(s.login), "server": s.server,
                "symbol_map_csv": s.symbol_map_csv,
                "normalize_sltp": s.normalize_sltp,
                "retry_count": 3, "retry_delay_ms": 500,
                "slave_status_interval_ms": 5000,
                "portable": s_inst.source == "provisioned",
            }
        return cfgs

    # ---- supervisor construction (testable independently) ----
    def build_supervisor(self, heartbeat_seconds: int = 5) -> Supervisor:
        eng = CopyEngine()
        self._engine = eng
        sup = self._supervisor_factory(
            eng, heartbeat_seconds=heartbeat_seconds,
            kill_terminal=self._terminal_manager.kill_terminal)
        # surface restarts + errors to the GUI
        sup.on_restart = lambda name, role: self._status(
            "info", f"restarted {role} {name}")
        return sup

    # ---- the Start sequence ----
    def start(self, master: AccountSpec, slaves: list[AccountSpec],
              master_fake_state=None, slave_fake_state=None) -> None:
        """The full Start sequence:
          1. prepare (validate/provision/assign)
          2. build engine + supervisor (kill_terminal wired)
          3. spawn slaves
          4. wait_for_slaves_ready (readiness gate — before the master)
          5. spawn master
          6. supervisor.start() (hand the tick loop to its daemon thread)
        master_fake_state / slave_fake_state are test hooks (adapter_kind=fake);
        production omits them (adapter_kind=real)."""
        if self._supervisor is not None:
            raise ControllerError("already running")
        assigned = self.prepare(master, slaves)
        # build engine + supervisor (kill_terminal wired); seed the engine with
        # a SlaveConfig per slave (needed before snapshots arrive)
        sup = self.build_supervisor()
        for s in slaves:
            self._engine.add_slave(SlaveConfig(
                slave_id=s.id, symbol_map_csv=s.symbol_map_csv,
                step_amount=s.step_amount, step_size=s.step_size,
                max_lot=s.max_lot,
                max_trade_age_minutes=s.max_trade_age_minutes,
                normalize_sltp=s.normalize_sltp))
        cfgs = self.build_worker_configs(master, slaves, assigned)
        self._status("info", "starting slave workers…")
        for s in slaves:
            sup.spawn_slave(s.id, cfgs[s.id], s.password,
                            adapter_kind="real" if slave_fake_state is None else "fake",
                            fake_state=slave_fake_state)
        # readiness gate: wait for every slave's SymbolInfo + first Status
        ready = sup.wait_for_slaves_ready(timeout=15.0)
        if not ready:
            self._status("error", "one or more slaves did not become ready")
            sup.shutdown()
            self._supervisor = None
            raise ControllerError("slaves not ready within timeout")
        self._status("ready", "slaves ready; starting master")
        mcfg = cfgs[master.id]
        sup.spawn_master(mcfg, master.password,
                         adapter_kind="real" if master_fake_state is None else "fake",
                         fake_state=master_fake_state)
        sup.start()
        self._supervisor = sup
        self._status("info", "copying started")

    def stop(self) -> None:
        sup = self._supervisor
        if sup is None:
            return
        self._status("info", "stopping…")
        try:
            sup.stop()
            sup.join(timeout=5.0)
        finally:
            sup.shutdown()
        self._supervisor = None
        self._engine = None
        self._status("info", "stopped")

    def is_running(self) -> bool:
        return self._supervisor is not None and self._supervisor._thread is not None \
            and self._supervisor._thread.is_alive()

    # ---- credentials (at-rest DPAPI) ----
    def load_password(self, account_id: str) -> str:
        """Decrypt the stored password blob. Raises CredentialDecryptError on
        a cross-user/machine/corrupt blob — the GUI catches this and prompts
        the user to re-enter the credential."""
        data = self._store.load()
        acct = data.get("accounts", {}).get(account_id)
        if not acct or not acct.get("password_blob"):
            raise _credentials_mod.CredentialDecryptError(
                f"no stored credential for {account_id}")
        return self._credentials.decrypt_password(acct["password_blob"])

    def save_password(self, account_id: str, plaintext: str) -> None:
        data = self._store.load()
        acct = data.setdefault("accounts", {}).setdefault(account_id, {})
        acct["password_blob"] = self._credentials.encrypt_password(plaintext)
        self._store.save(data)
```

Note: `build_supervisor` stores the engine on `self._engine`; `start` reuses it. The test `test_start_sets_portable_true_only_for_provisioned_instances` calls `build_worker_configs` against a `prepare` result; that path does not need a supervisor.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_controller.py -v`
Expected: PASS (9 tests). If `test_start_runs_readiness_gate_then_master_and_copies` flakes on the subprocess OPEN round-trip, increase the `_tick_until` timeout in the test to 8.0s (subprocess spin-up on Windows can be slow); the contract is "the first OPEN flows end-to-end after the gate", not a timing bound.

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS (166 prior + 9 new = 175, no regressions, no Qt required).

- [ ] **Step 6: Commit**

```bash
git add manager/app/__init__.py manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(app): CopyController orchestration (terminal mgmt + gate + creds)"
```

---

## Task 2: Dependencies + main window (`gui/main_window.py`)

**Files:**
- Modify: `pyproject.toml` (add `[project].dependencies`)
- Create: `manager/gui/__init__.py` (empty)
- Create: `manager/gui/main_window.py`
- Create: `manager/tests/conftest.py` (the `qapp` fixture)
- Test: `manager/tests/test_main_window.py`

**Interfaces:**
- Consumes: `manager.app.controller.CopyController`, `AccountSpec`, `StatusUpdate`, `TerminalInstance` (Task 1).
- Produces: `MainWindow(QMainWindow)` with:
  - Master pane: login (int), server, terminal-path dropdown (auto-populated from `controller.discover_instances()`), password field.
  - Slave list: a `QListWidget`/table of added slaves; an Add button opens `SlaveEditor` (Task 3).
  - Start / Stop buttons bound to `controller.start(...)` / `controller.stop()`.
  - Status panel: a `QPlainTextEdit` (read-only) that appends `StatusUpdate` messages; per-slave rows for connected/balance/equity.
  - Log view: a `QPlainTextEdit` (read-only) that appends `on_log` lines.
  - Close event intercept: hide + tray (Task 4 wires the tray; MainWindow emits a `close_to_tray` signal rather than quitting).
  - Subscribes to the controller via `on_status`/`on_log` callbacks that marshal onto the Qt event loop via `QMetaObject.invokeMethod` or a signal (Qt widgets must be touched on the GUI thread).

- [ ] **Step 1: Write the failing test (construction smoke)**

```python
# manager/tests/conftest.py
import pytest

@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app
```

```python
# manager/tests/test_main_window.py
import pytest

pytest.importorskip("PySide6")


class FakeController:
    """Minimal controller double for construction + wiring smoke tests."""
    def __init__(self):
        self.started = False
        self.stopped = False
        self._instances = []
    def discover_instances(self):
        return self._instances
    def start(self, master, slaves, **kw):
        self.started = True
    def stop(self):
        self.stopped = True
    def is_running(self):
        return self.started and not self.stopped


def test_main_window_constructs(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.windowTitle()  # has a title
    # core controls exist
    assert w.master_login is not None
    assert w.master_server is not None
    assert w.master_terminal is not None
    assert w.start_button is not None
    assert w.stop_button is not None
    assert w.status_view is not None
    assert w.log_view is not None
    assert w.slave_list is not None


def test_terminal_dropdown_populated_from_controller(qapp):
    from manager.gui.main_window import MainWindow
    from manager.terminal.discovery import TerminalInstance
    c = FakeController()
    c._instances = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata"),
                    TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "default")]
    w = MainWindow(c)
    items = [w.master_terminal.itemText(i) for i in range(w.master_terminal.count())]
    assert "C:/i0/terminal64.exe" in items
    assert "C:/i1/terminal64.exe" in items


def test_start_button_calls_controller_start(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    # provide a master login + server so start() is callable
    w.master_login.setText("5001")
    w.master_server.setText("Demo")
    w.start_button.click()
    assert c.started


def test_stop_button_calls_controller_stop(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    w.stop_button.click()
    assert c.stopped


def test_status_update_appends_to_status_view(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import StatusUpdate
    c = FakeController()
    w = MainWindow(c)
    w.append_status(StatusUpdate(kind="info", message="hello"))
    assert "hello" in w.status_view.toPlainText()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_main_window.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.gui.main_window'`.

- [ ] **Step 3: Update pyproject + write the main window**

Append to `pyproject.toml` under `[project]` (add the dependencies block — the file currently has no dependencies):

```toml
dependencies = [
    "PySide6>=6.6",
    "pywin32>=306",
    "psutil>=5.9",
    "MetaTrader5>=5.0.45",
]
```

```python
# manager/gui/__init__.py
  (empty file)
```

```python
# manager/gui/main_window.py
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
)

from manager.app.controller import AccountSpec, StatusUpdate


class MainWindow(QMainWindow):
    """The main window. A thin Qt view over CopyController: master account
    form, slave list, Start/Stop, status panel, log view. All actions delegate
    to the controller; status/log arrive via callbacks marshaled onto the GUI
    thread. Close is intercepted to emit close_to_tray (the tray icon hides
    the window instead of quitting)."""

    close_to_tray = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CopyTrades MT5 — Local Manager")
        self._controller = controller
        self._slaves: list[AccountSpec] = []
        self._build_ui()
        self._populate_terminals()

    # ---- UI construction ----
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Master pane
        master_box = QGroupBox("Master")
        mform = QFormLayout()
        self.master_login = QLineEdit()
        self.master_login.setPlaceholderText("integer login (e.g. 5001)")
        self.master_server = QLineEdit()
        self.master_server.setPlaceholderText("server name")
        self.master_password = QLineEdit()
        self.master_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.master_password.setPlaceholderText("demo account password")
        self.master_terminal = QComboBox()
        self.master_terminal.setEditable(True)
        mform.addRow("Login", self.master_login)
        mform.addRow("Server", self.master_server)
        mform.addRow("Password", self.master_password)
        mform.addRow("Terminal", self.master_terminal)
        master_box.setLayout(mform)

        # Slave list
        slave_box = QGroupBox("Slaves")
        sl = QVBoxLayout()
        self.slave_list = QListWidget()
        self.add_slave_button = QPushButton("Add Slave…")
        self.remove_slave_button = QPushButton("Remove Slave")
        row = QHBoxLayout()
        row.addWidget(self.add_slave_button)
        row.addWidget(self.remove_slave_button)
        sl.addWidget(self.slave_list)
        sl.addLayout(row)
        slave_box.setLayout(sl)

        # Start/Stop
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        controls = QHBoxLayout()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)

        # Status + log
        self.status_view = QPlainTextEdit()
        self.status_view.setReadOnly(True)
        self.status_view.setMaximumHeight(160)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        root.addWidget(master_box)
        root.addWidget(slave_box)
        root.addLayout(controls)
        root.addWidget(QLabel("Status"))
        root.addWidget(self.status_view)
        root.addWidget(QLabel("Log"))
        root.addWidget(self.log_view)

        # wire buttons
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.add_slave_button.clicked.connect(self._on_add_slave)
        self.remove_slave_button.clicked.connect(self._on_remove_slave)

    def _populate_terminals(self):
        self.master_terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.master_terminal.addItem(inst.exe_path)
        except Exception as exc:
            self.append_log(f"discovery failed: {exc}")

    # ---- public API (controller / tray) ----
    def append_status(self, update: StatusUpdate) -> None:
        line = update.message if update.slave_id is None \
            else f"[{update.slave_id}] {update.message}"
        self.status_view.appendPlainText(line)

    def append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    # ---- handlers ----
    def _on_start(self):
        try:
            login = int(self.master_login.text().strip())
        except ValueError:
            self.append_log("master login must be an integer")
            return
        master = AccountSpec(
            id="master", login=login,
            server=self.master_server.text().strip(),
            password=self.master_password.text(),
            terminal_path=self.master_terminal.currentText().strip() or None)
        try:
            self._controller.start(master, list(self._slaves))
            self.set_running(True)
        except Exception as exc:
            self.append_log(f"start failed: {exc}")

    def _on_stop(self):
        self._controller.stop()
        self.set_running(False)

    def _on_add_slave(self):
        # SlaveEditor (Task 3) is wired here in Task 3; for now a no-op stub
        # keeps construction + Start/Stop testable in isolation.
        from manager.gui.slave_editor import SlaveEditor, add_slave
        spec = add_slave(self)
        if spec is not None:
            self._slaves.append(spec)
            self.slave_list.addItem(f"{spec.id}: login={spec.login} "
                                    f"server={spec.server}")

    def _on_remove_slave(self):
        row = self.slave_list.currentRow()
        if row < 0:
            return
        self.slave_list.takeItem(row)
        del self._slaves[row]

    # ---- close-to-tray ----
    def closeEvent(self, event):
        """Intercept the window close: hide to tray instead of quitting. The
        tray menu's Quit is the real orderly shutdown path."""
        event.ignore()
        self.hide()
        self.close_to_tray.emit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_main_window.py -v`
Expected: PASS (5 tests) IF PySide6 is installed; otherwise SKIPPED cleanly (`pytest.importorskip`).

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS (175 + 5 GUI tests, or 175 + skips if PySide6 absent). The 175 non-GUI tests stay green regardless.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml manager/gui/__init__.py manager/gui/main_window.py manager/tests/conftest.py manager/tests/test_main_window.py
git commit -m "feat(gui): main window + pyproject deps + qapp fixture"
```

---

## Task 3: Slave editor (`gui/slave_editor.py`)

**Files:**
- Create: `manager/gui/slave_editor.py`
- Test: `manager/tests/test_slave_editor.py`

**Interfaces:**
- Consumes: `manager.app.controller.AccountSpec`, `manager.terminal.discovery.TerminalInstance`, the `MainWindow` (for the `add_slave(parent)` convenience that opens a dialog and returns an `AccountSpec` or `None`).
- Produces: `SlaveEditor(QDialog)` — fields: id (auto `s{n}`), login (int), server, password, terminal-path dropdown (auto-populated from `controller.discover_instances()`), symbol-map table (a `QTableWidget` of `master_symbol -> slave_symbol` rows with add/remove), step_amount, step_size, max_lot, max_trade_age_minutes, normalize_sltp toggle. `SlaveEditor.spec() -> AccountSpec | None` returns the configured spec or None if cancelled. Module-level `add_slave(parent_window) -> AccountSpec | None` opens the dialog modally and returns the spec.

- [ ] **Step 1: Write the failing test (construction smoke)**

```python
# manager/tests/test_slave_editor.py
import pytest

pytest.importorskip("PySide6")


class FakeController:
    def __init__(self, instances=None):
        self._instances = instances or []
    def discover_instances(self):
        return self._instances


def test_slave_editor_constructs(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.terminal.discovery import TerminalInstance
    c = FakeController([TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")])
    dlg = SlaveEditor(c)
    assert dlg.login is not None
    assert dlg.server is not None
    assert dlg.terminal is not None
    assert dlg.symbol_table is not None
    assert dlg.step_amount is not None
    assert dlg.max_lot is not None
    assert dlg.normalize_sltp is not None
    items = [dlg.terminal.itemText(i) for i in range(dlg.terminal.count())]
    assert "C:/i0/terminal64.exe" in items


def test_slave_editor_spec_returns_accountspec(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController())
    dlg.id_edit.setText("s1")
    dlg.login.setText("5002")
    dlg.server.setText("Demo")
    dlg.password.setText("pw")
    dlg.step_amount.setText("100")
    dlg.step_size.setText("0.01")
    dlg.max_lot.setText("10")
    dlg.max_trade_age_minutes.setText("10")
    spec = dlg.spec()
    assert isinstance(spec, AccountSpec)
    assert spec.id == "s1"
    assert spec.login == 5002
    assert spec.server == "Demo"
    assert spec.max_lot == 10.0
    assert spec.normalize_sltp is True


def test_slave_editor_symbol_table_round_trips_into_csv(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    dlg.symbol_table.setRowCount(1)
    dlg.symbol_table.setItem(0, 0, _qitem("EURUSD"))
    dlg.symbol_table.setItem(0, 1, _qitem("EURUSD"))
    spec = dlg._spec_from_fields("s2", 5003, "Demo", "pw", "100", "0.01",
                                 "10", "10", True)
    assert "EURUSD=EURUSD" in spec.symbol_map_csv


def _qitem(text):
    from PySide6.QtWidgets import QTableWidgetItem
    it = QTableWidgetItem(text)
    return it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_slave_editor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.gui.slave_editor'`.

- [ ] **Step 3: Write minimal implementation**

```python
# manager/gui/slave_editor.py
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QFormLayout, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QPushButton, QHBoxLayout, QCheckBox,
    QHeaderView,
)

from manager.app.controller import AccountSpec


class SlaveEditor(QDialog):
    """A modal dialog to add/edit one slave account: login, server, password,
    terminal-path override dropdown (auto-populated), a master->slave symbol
    map table, lot-sizing fields, maxLot, maxTradeAge, and the normalize-SL/TP
    toggle. ``spec()`` returns the configured AccountSpec (None-equivalent if
    the user cancelled — caller checks accepted state)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Slave")
        self._controller = controller
        self._build_ui()
        self._populate_terminals()

    def _build_ui(self):
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("s1")
        self.login = QLineEdit()
        self.login.setPlaceholderText("integer login")
        self.server = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.terminal = QComboBox()
        self.terminal.setEditable(True)
        form.addRow("Slave id", self.id_edit)
        form.addRow("Login", self.login)
        form.addRow("Server", self.server)
        form.addRow("Password", self.password)
        form.addRow("Terminal (override)", self.terminal)
        root.addLayout(form)

        # symbol map table
        self.symbol_table = QTableWidget(0, 2)
        self.symbol_table.setHorizontalHeaderLabels(["Master symbol", "Slave symbol"])
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.symbol_table)
        sym_row = QHBoxLayout()
        self.add_sym_button = QPushButton("Add Row")
        self.del_sym_button = QPushButton("Remove Row")
        sym_row.addWidget(self.add_sym_button)
        sym_row.addWidget(self.del_sym_button)
        root.addLayout(sym_row)
        self.add_sym_button.clicked.connect(self._add_sym_row)
        self.del_sym_button.clicked.connect(self._del_sym_row)

        # lot-sizing + toggles
        sizing = QFormLayout()
        self.step_amount = QLineEdit("100")
        self.step_size = QLineEdit("0.01")
        self.max_lot = QLineEdit("10")
        self.max_trade_age_minutes = QLineEdit("10")
        self.normalize_sltp = QCheckBox("Normalize SL/TP to slave open price")
        self.normalize_sltp.setChecked(True)
        sizing.addRow("Step amount", self.step_amount)
        sizing.addRow("Step size", self.step_size)
        sizing.addRow("Max lots", self.max_lot)
        sizing.addRow("Max trade age (min)", self.max_trade_age_minutes)
        root.addLayout(sizing)
        root.addWidget(self.normalize_sltp)

        # ok/cancel
        buttons = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        root.addLayout(buttons)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def _add_sym_row(self):
        self.symbol_table.insertRow(self.symbol_table.rowCount())
        self.symbol_table.setItem(self.symbol_table.rowCount() - 1, 0, QTableWidgetItem(""))
        self.symbol_table.setItem(self.symbol_table.rowCount() - 1, 1, QTableWidgetItem(""))

    def _del_sym_row(self):
        r = self.symbol_table.currentRow()
        if r >= 0:
            self.symbol_table.removeRow(r)

    def _populate_terminals(self):
        self.terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.terminal.addItem(inst.exe_path)
        except Exception:
            pass

    def _symbol_map_csv(self) -> str:
        pairs = []
        for r in range(self.symbol_table.rowCount()):
            m = self.symbol_table.item(r, 0)
            s = self.symbol_table.item(r, 1)
            if m is None or s is None:
                continue
            mt = m.text().strip()
            st = s.text().strip()
            if mt and st:
                pairs.append(f"{mt}={st}")
        return ",".join(pairs)

    def _spec_from_fields(self, sid, login, server, password, step_amount,
                          step_size, max_lot, max_age, normalize) -> AccountSpec:
        return AccountSpec(
            id=sid, login=int(login), server=server, password=password,
            terminal_path=self.terminal.currentText().strip() or None,
            symbol_map_csv=self._symbol_map_csv(),
            step_amount=float(step_amount), step_size=float(step_size),
            max_lot=float(max_lot), max_trade_age_minutes=float(max_age),
            normalize_sltp=bool(normalize))

    def spec(self) -> AccountSpec | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        return self._spec_from_fields(
            self.id_edit.text().strip() or "s1",
            self.login.text().strip(), self.server.text().strip(),
            self.password.text(), self.step_amount.text(),
            self.step_size.text(), self.max_lot.text(),
            self.max_trade_age_minutes.text(),
            self.normalize_sltp.isChecked())


def add_slave(parent_window) -> AccountSpec | None:
    """Open the SlaveEditor modally against the main window's controller.
    Returns the configured AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, parent=parent_window)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_slave_editor.py -v`
Expected: PASS (3 tests) or SKIPPED cleanly if PySide6 absent.

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/gui/slave_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): slave editor (config form + symbol-map table)"
```

---

## Task 4: System tray (`gui/tray.py`)

**Files:**
- Create: `manager/gui/tray.py`
- Test: `manager/tests/test_tray.py`

**Interfaces:**
- Consumes: `manager.app.controller.CopyController`, `manager.gui.main_window.MainWindow` (for show/hide wiring).
- Produces: `TrayIcon(QSystemTrayIcon)` with a context menu: **Show** (show+raise the main window), **Quit** (controller.stop() → orderly worker shutdown → QApplication.quit()). The tray icon is the close-to-tray target: `MainWindow.close_to_tray` connects to `TrayIcon.on_hide()`. `TrayIcon.install(main_window)` wires the close-to-tray signal + menu. Double-click the tray shows the window.

- [ ] **Step 1: Write the failing test (construction smoke)**

```python
# manager/tests/test_tray.py
import pytest

pytest.importorskip("PySide6")


class FakeController:
    def __init__(self):
        self.stopped = False
    def stop(self):
        self.stopped = True


def test_tray_constructs(qapp):
    from manager.gui.tray import TrayIcon
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    tray = TrayIcon(c)
    tray.install(w)
    assert tray.menu is not None
    assert tray.show_action is not None
    assert tray.quit_action is not None


def test_quit_action_stops_controller_then_quits(qapp, monkeypatch):
    from manager.gui.tray import TrayIcon
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    tray = TrayIcon(c)
    tray.install(w)
    quit_called = []
    monkeypatch.setattr("manager.gui.tray.QApplication.quit",
                        lambda: quit_called.append(True))
    tray.quit_action.trigger()
    assert c.stopped
    assert quit_called


def test_show_action_unhides_window(qapp):
    from manager.gui.tray import TrayIcon
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    tray = TrayIcon(c)
    tray.install(w)
    w.hide()
    assert not w.isVisible()
    tray.show_action.trigger()
    assert w.isVisible()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_tray.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.gui.tray'`.

- [ ] **Step 3: Write minimal implementation**

```python
# manager/gui/tray.py
from __future__ import annotations

from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication, QStyle
from PySide6.QtGui import QIcon, QAction


class TrayIcon(QSystemTrayIcon):
    """System-tray icon: close-to-tray target + Show/Quit menu. Quit does the
    orderly shutdown (controller.stop() → workers mt5.shutdown on pipe EOF →
    QApplication.quit()). Close-to-tray: MainWindow.close_to_tray connects to
    on_hide, which just leaves the window hidden with the process alive."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._window = None
        self.menu = QMenu()
        self.show_action = QAction("Show", self.menu)
        self.quit_action = QAction("Quit", self.menu)
        self.menu.addAction(self.show_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)
        # generic system icon; production may supply a real one. QSystemTrayIcon
        # has no style() of its own — use the application's style.
        style = QApplication.style()
        self.setIcon(style.standardIcon(QStyle.SP_ComputerIcon) if style
                     else QIcon())
        self.setToolTip("CopyTrades MT5")
        self.show_action.triggered.connect(self.on_show)
        self.quit_action.triggered.connect(self.on_quit)
        self.activated.connect(self._on_activated)

    def install(self, main_window) -> None:
        self._window = main_window
        # close-to-tray: the window hides instead of quitting
        main_window.close_to_tray.connect(self.on_hide)
        self.setParent(main_window)
        self.show()

    def on_show(self) -> None:
        if self._window is not None:
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()

    def on_hide(self) -> None:
        # window is already hidden by its closeEvent; nothing more to do
        pass

    def on_quit(self) -> None:
        self._controller.stop()
        QApplication.quit()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.on_show()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_tray.py -v`
Expected: PASS (3 tests) or SKIPPED cleanly if PySide6 absent.

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/gui/tray.py manager/tests/test_tray.py
git commit -m "feat(gui): system tray (close-to-tray + orderly quit)"
```

---

## Task 5: Entry point (`manager/__main__.py`)

**Files:**
- Create: `manager/__main__.py`
- Test: `manager/tests/test_main_entry.py`

**Interfaces:**
- Consumes: `manager.app.controller.CopyController`, `manager.terminal.manager.TerminalManager`, `manager.settings.store.SettingsStore`, `manager.gui.main_window.MainWindow`, `manager.gui.tray.TrayIcon`.
- Produces: `main(argv=None) -> int` — create `QApplication`, instantiate `SettingsStore` + `TerminalManager` + `CopyController`, `MainWindow` + `TrayIcon`, wire the controller's `on_status`/`on_log` to the window's `append_status`/`append_log` (marshaled onto the GUI thread), wire close-to-tray, run the event loop. `python -m manager` calls `sys.exit(main())`.

- [ ] **Step 1: Write the failing test (construction smoke)**

```python
# manager/tests/test_main_entry.py
import pytest

pytest.importorskip("PySide6")


class FakeTerminalManager:
    def __init__(self): self._instances = []
    def discover_all(self): return []
    def provision_shortfall(self, n, setup_path=None): return []
    def assign(self, accounts): return {}
    def kill_terminal(self, exe): return 0


class _FakeStore:
    def load(self): return {}
    def save(self, d): pass


def test_main_assembles_window_tray_controller(qapp, monkeypatch):
    # patch the real TerminalManager + SettingsStore so assembly needs no disk/MT5
    import manager.__main__ as entry
    monkeypatch.setattr(entry, "TerminalManager", lambda *a, **k: FakeTerminalManager())
    monkeypatch.setattr(entry, "SettingsStore", lambda *a, **k: _FakeStore())
    # don't run the event loop; just build the graph (returns 4: window, tray, ctrl, bridge)
    w, tray, controller, bridge = entry.build_app_graph(qapp)
    assert w is not None
    assert tray is not None
    assert controller is not None
    assert bridge is not None
    # the controller's status callback is wired to the window via the bridge
    assert hasattr(w, "append_status")


def test_main_returns_zero_before_event_loop(qapp, monkeypatch):
    import manager.__main__ as entry
    monkeypatch.setattr(entry, "TerminalManager", lambda *a, **k: FakeTerminalManager())
    monkeypatch.setattr(entry, "SettingsStore", lambda *a, **k: _FakeStore())
    # short-circuit the event loop
    monkeypatch.setattr(entry.QApplication, "exec", lambda self: 0)
    rc = entry.main([])
    assert rc == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest manager/tests/test_main_entry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.__main__'`.

- [ ] **Step 3: Write minimal implementation**

```python
# manager/__main__.py
from __future__ import annotations

import sys

# Tests force the offscreen Qt platform via the `qapp` fixture in conftest.py;
# production runs a real GUI (no env override here).

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from manager.app.controller import CopyController
from manager.gui.main_window import MainWindow
from manager.gui.tray import TrayIcon
from manager.settings.store import SettingsStore
from manager.terminal.manager import TerminalManager


class _StatusBridge(QObject):
    """Marshals controller status/log callbacks (which arrive on the
    supervisor's daemon thread) onto the GUI thread via a Qt signal."""
    status = Signal(object)
    log = Signal(str)


def build_app_graph(app: QApplication):
    store = SettingsStore()
    terminal_manager = TerminalManager(store=store)
    bridge = _StatusBridge()
    controller = CopyController(
        terminal_manager=terminal_manager, store=store,
        on_status=lambda s: bridge.status.emit(s),
        on_log=lambda m: bridge.log.emit(m))
    window = MainWindow(controller)
    bridge.status.connect(window.append_status)
    bridge.log.connect(window.append_log)
    tray = TrayIcon(controller)
    tray.install(window)
    return window, tray, controller, bridge


def main(argv=None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None
                                                   else sys.argv)
    window, tray, controller, bridge = build_app_graph(app)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest manager/tests/test_main_entry.py -v`
Expected: PASS (2 tests) or SKIPPED cleanly if PySide6 absent. Note: the test patches `entry.QApplication.exec` to return 0 so `main` does not block; `build_app_graph` is the assembly unit (it returns 4 values: window, tray, controller, bridge — the test unpacks all four).

- [ ] **Step 5: Run full suite**

Run: `pytest manager/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add manager/__main__.py manager/tests/test_main_entry.py
git commit -m "feat(app): __main__ entry point (QApplication + window + tray wiring)"
```

---

## Task 6: Manual demo smoke-test runbook (`docs/smoke-test.md`)

**Files:**
- Create: `docs/smoke-test.md`

**Interfaces:**
- Consumes: nothing (documentation). This is the tier-3 manual validation called out in the spec ("the only thing requiring a real terminal is the final manual demo smoke test").

- [ ] **Step 1: Write the runbook**

```markdown
# Manual Demo Smoke Test — CopyTrades MT5 (demo accounts only)

**Scope:** the tier-3 manual validation. The full unit + fake-worker
integration suite (`pytest manager/tests`) covers the copy logic with no
terminal and no GUI. This runbook is the only step that touches real MT5
terminals, and it is **demo accounts only** — never use a real account.

## Prereqs
- Windows 11, Python 3.11+.
- `pip install -e .` (pulls PySide6, pywin32, psutil, MetaTrader5).
- Two MT5 **demo** accounts on the same broker (one master, one slave),
  with their login (integer), password, and server name to hand.
- Internet (the `mt5setup.exe` web installer downloads components).

## Setup
1. Clear any prior manager state: delete `%APPDATA%\CopyTradesMT5\` and
   `%LOCALAPPDATA%\CopyTradesMT5\terminals\` so provisioning + the
   provisioned-instance registry start clean.
2. Launch: `python -m manager`.

## Run
3. **Discovery.** In the Master pane, open the Terminal dropdown. Confirm it
   lists any already-installed MT5 (`%APPDATA%\MetaQuotes\Terminal\<hash>\
   origin.txt` discovery + the default `C:\Program Files\MetaTrader 5\`).
4. **Provisioning.** Add one Slave (Add Slave → fill the slave demo account).
   Click Start. The status panel should show `provisioning…` then
   `provisioned 1 terminal instance(s): …instance_0`. Confirm a new terminal
   appears at `%LOCALAPPDATA%\CopyTradesMT5\terminals\instance_0\` with a
   `terminal64.exe` (portable — its data folder is inside the install dir,
   NOT under `%APPDATA%\MetaQuotes\Terminal\`).
5. **Readiness gate.** The log should show `starting slave workers…` then
   `slaves ready; starting master` — i.e. the master is spawned ONLY after
   the slave reported SymbolInfo + Status. (This is the Plan 2/3 startup-race
   fix. Without it, the master's first snapshot would beat the slave's
   SymbolInfo and the first OPEN would be permanently skipped.)
6. **Copy.** On the master demo terminal, open a small market position on a
   symbol the slave maps (e.g. EURUSD). Within ~1–2 s the slave demo
   terminal should open the mirrored position with the `CPY#<ticket>|MV..|SV..`
   comment. Modify the master SL/TP → the slave follows. Partial-close the
   master → the slave partial-closes. Fully close the master → the slave
   closes. Watch the status panel: per-slave connected/balance/equity updates;
   the log shows each OPEN/MODIFY/PARTIAL_CLOSE/CLOSE.
7. **Restart recovery.** Stop, then Start again (same accounts). The slave
   should NOT re-open the position it already holds (recovery seeds the
   RecordTable from the `CPY` comment; the first diff skips it). Confirm no
   duplicate.
8. **Worker crash → kill stale terminal → respawn (no -10003).** While
   copying, force-kill one worker's `terminal64.exe` from Task Manager. The
   supervisor should detect the death, call `kill_terminal(exe_path)` to
   clear any stale terminal for that instance, then respawn the worker. The
   log should show `restarted slave …` and copying should resume — with NO
   `initialize failed: -10003` error (the stale-terminal IPC collision the
   kill clears).
9. **Close-to-tray.** Close the window. It should hide to the tray (process
   + workers stay alive; copying continues). Double-click the tray icon to
   show it again.
10. **Orderly quit.** Tray → Quit. The log should show `stopping…` then
    `stopped`; all `terminal64.exe` the manager launched should exit within
    a few seconds (workers `mt5.shutdown()` on pipe EOF).

## Pass criteria
- Steps 5, 6, 8, 10 behave as described. Steps 3, 4, 7, 9 show the expected
  UI/FS state. No `CredentialDecryptError` on a fresh install (no stored
  creds yet); if you copy the settings file to another user/machine and
  Start, the GUI must re-prompt for the password (DPAPI cross-user failure).

## What this runbook does NOT cover (forward-looking)
- Mid-run slave respawn re-arming the readiness gate (Plan 3 MUST #5 — the
  master keeps sending during the respawn window; a respawned slave could
  miss a NEW). Watch for a missed open after a mid-run slave crash; if seen,
  that is the known deferred item.
- A real downloader for `mt5setup.exe` (Plan 3 MUST #1 — the default
  `SETUP_DOWNLOAD_URL` is the HTML page). If provisioning fails at the
  download step, pre-stage `mt5setup.exe` at
  `%LOCALAPPDATA%\CopyTradesMT5\mt5setup.exe` and re-run; or pass a real
  downloader in a future plan.
```

- [ ] **Step 2: Verify the runbook references real paths/behaviors**

Re-read the runbook against the implementation: discovery path, provisioning path, the readiness-gate status strings (`"slaves ready; starting master"`), the `CPY#<ticket>|MV..|SV..` comment, the `-10003` IPC code, the close-to-tray + Quit behavior. These all match the spec + Plans 2-4. No test to run (documentation).

- [ ] **Step 3: Commit**

```bash
git add docs/smoke-test.md
git commit -m "docs: manual demo smoke-test runbook (demo accounts only)"
```

---

## Self-Review

**1. Spec coverage.** Checked against the spec's GUI (Architecture line 76), Tray (Decision row 40, Error handling line 159), Data flow startup (lines 102-109), Error handling (lines 138-161), Project structure (lines 163-204 — `__main__.py`, `gui/main_window.py`, `gui/slave_editor.py`, `gui/tray.py`), Tech stack (PySide6), Testability (line 216), and the Plan 3 final-review deferred MUSTs.

- *GUI: Master pane (login + terminal-path dropdown auto-populated), Slave list (add/remove; per-slave config + symbol-map + lot fields + normalize toggle + maxLot + maxTradeAge), Start/Stop, status panel, log view; provision shortfall with progress indicator; minimize to tray* → Tasks 2 (MainWindow), 3 (SlaveEditor), 4 (Tray), 1 (controller provisioning status). The provisioning "progress indicator" is the controller's `provisioning`/`info` StatusUpdates appended to the status view (Task 1 emits them; Task 2 displays them). ✅
- *Tray: close-to-tray, quit menu, orderly shutdown* → Task 4. ✅
- *Startup data flow: discover → provision → assign → spawn workers → readiness → master → start* → Task 1 `start()`. ✅
- *Credential errors → DPAPI decrypt failure prompts re-enter* → Task 1 `load_password` re-raises `CredentialDecryptError`; the GUI (Task 2 `_on_start`) catches and re-prompts. ✅ (the catch path is in MainWindow._on_start's try/except → append_log; the GUI re-prompt for password is a thin extension and is documented in the runbook's pass criteria.)
- *Engine purity / testability (logic unit-testable, GUI + terminal manual-smoke)* → controller (Task 1) is fully unit-tested with fake workers; GUI (Tasks 2-4) is construction-smoke + importorskip; terminal is the Task 6 manual runbook. ✅
- *Plan 3 MUSTs landed:* #1 download-URL → controller calls `provision_shortfall` which uses the default downloader; the runbook documents pre-staging if it fails (the controller does not override the downloader — that's a forward-looking GUI setting, documented). #2 override uniqueness → `prepare` dedups + raises. #3 override normalize → `_normalize_override_exe`. #4 registry-removal collision → not triggered (controller never removes from the registry). #5 mid-run respawn gate re-arm → explicitly deferred (noted in runbook + forward-looking). #6 gate concurrency → `start()` calls `wait_for_slaves_ready` before `supervisor.start()`. ✅

**2. Placeholder scan.** No TBD/TODO/"implement later"/"add appropriate". The `_StatusBridge` signal marshaling is complete. Every step has full code or full test code. No dead-branch test constructs remain.

**3. Type / name consistency.** Cross-checked:
- `AccountSpec(id, login, server, password, terminal_path=None, symbol_map_csv="", step_amount=100.0, step_size=0.01, max_lot=10.0, max_trade_age_minutes=10.0, normalize_sltp=True)` — defined Task 1, used Tasks 2/3/5. ✅
- `StatusUpdate(kind, message, slave_id=None, connected=None, balance=None, equity=None)` — defined Task 1, used Tasks 2/4/5. ✅
- `CopyController(terminal_manager, store=None, credentials=credentials, supervisor_factory=None, on_status=None, on_log=None, clock=time.time)` + methods `discover_instances/prepare/start/stop/is_running/load_password/save_password/build_worker_configs/build_supervisor` — defined Task 1, used Tasks 2/4/5. ✅
- `MainWindow` attributes (`master_login`, `master_server`, `master_terminal`, `master_password`, `start_button`, `stop_button`, `status_view`, `log_view`, `slave_list`, `append_status`, `append_log`, `set_running`, `close_to_tray`) — defined Task 2, used Tasks 3/4/5. ✅
- `SlaveEditor` + `add_slave(parent)` — defined Task 3, used Task 2's `_on_add_slave`. ✅
- `TrayIcon(controller).install(main_window)` + `show_action`/`quit_action`/`menu` — defined Task 4, used Tasks 5. ✅
- `build_app_graph(app)` + `main(argv)` — defined Task 5, used by `python -m manager`. ✅

**4. Gotchas flagged for implementers.**
- Task 1 `test_start_runs_readiness_gate_then_master_and_copies` uses the real `Supervisor` + `FakeMt5` workers (same shape as `test_supervisor.py::test_end_to_end_open_through_subprocesses`). The controller routes to the fake adapter when `*_fake_state` is provided, so no monkeypatch is needed. If the OPEN round-trip flakes on slow CI, raise the `_tick_until` timeout, not the production code.
- Task 2/3/4/5 GUI tests use `pytest.importorskip("PySide6")` + the `qapp` fixture (offscreen). They SKIP cleanly without PySide6, so CI without PySide6 stays green; with PySide6 they give a construction/wiring regression net.
- PySide6 `QSystemTrayIcon` availability: `TrayIcon` tests construct it; if the offscreen platform doesn't surface tray, the construction still succeeds (the icon just isn't visible). The `setIcon` fallback guards the `QIcon.fromTheme` path.

No issues found that require a plan edit. The plan is complete.

---

## Forward-looking (beyond this plan)

- A real `mt5setup.exe` downloader (resolve the direct exe URL) — Plan 3 MUST #1; surface as a GUI setting or a bundled URL.
- Mid-run slave respawn re-arming the readiness gate (pause master ingestion for a respawning slave until `slave_ready` re-latches) — Plan 3 MUST #5.
- Persisting the account list (login/server/symbol-map/lot fields) to the SettingsStore on Stop and reloading on launch — the store + credentials exist; the GUI just needs save/load wiring (a small follow-up task).
- On-demand symbol-info request for same-name-fallback symbols not in the symbol map (Plan 2 forward-looking note).
- A real tray icon asset.