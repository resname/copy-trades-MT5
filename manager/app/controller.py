# manager/app/controller.py
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.engine.models import BUY, SELL  # noqa: F401  (re-exported for GUI)
from manager.supervisor import Supervisor
from manager.terminal.discovery import TerminalInstance
from manager.settings import credentials as _credentials_mod
from manager.settings.store import SettingsStore
from manager.brokers import catalog as _catalog_mod
from manager.brokers import default as _default_mod
from manager.brokers import learned as _learned_mod
from manager.brokers import live as _live_mod
from manager.brokers.catalog import BrokerCatalog


class ControllerError(Exception):
    """Raised before Start for unrecoverable config: duplicate/unresolvable
    terminal-path overrides, or not enough instances after provisioning."""


@dataclass
class AccountSpec:
    id: str
    terminal_path: str | None = None
    login: int = 0
    server: str = ""
    password: str = ""
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
        self._catalog: BrokerCatalog | None = None
        self._recorded_servers: set[str] = set()
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

    # ---- broker catalog (broker/server browser) ----
    def _cache_path(self) -> Path:
        # same directory the settings store writes to
        return self._store.path.parent / "brokers_cache.json"

    def get_catalog(self) -> BrokerCatalog:
        """The merged broker catalog (default + fresh live cache + learned),
        built lazily and cached. The GUI pickers call this to populate."""
        if self._catalog is None:
            self._catalog = self._build_catalog()
        return self._catalog

    def _build_catalog(self) -> BrokerCatalog:
        default_brokers = _default_mod.load_default()
        live_brokers: list = []
        cache = _live_mod.load_cache(self._cache_path())
        if cache is not None and _live_mod.is_fresh(cache, self._clock()):
            live_brokers = _catalog_mod.parse_brokers_json(cache, "live")
        learned_servers = _learned_mod.load(self._store)
        return BrokerCatalog(default=default_brokers, live=live_brokers,
                             learned_servers=learned_servers)

    def refresh_brokers(self) -> BrokerCatalog:
        """Best-effort refresh of the community broker list (called from the
        GUI Refresh button, off the GUI thread). Fetches live, writes the cache,
        rebuilds the catalog. On failure the cache is untouched and a warning
        is logged; the catalog is still rebuilt from default + cache + learned.
        Never raises."""
        payload = _live_mod.refresh_cache(self._cache_path(), timeout=10.0,
                                          now=self._clock())
        if payload is None:
            self._log("community broker list unavailable; using cached/default list")
        self._catalog = self._build_catalog()
        return self._catalog

    def _on_worker_status(self, name: str, role: str, msg) -> None:
        """Record the server a worker logged into, once per distinct server, so
        it appears under '(Previously used)' on the next launch. Runs on the
        supervisor's daemon thread; the settings store writes atomically."""
        server = (getattr(msg, "server", "") or "").strip()
        if not server or server in self._recorded_servers:
            return
        self._recorded_servers.add(server)
        _learned_mod.record(self._store, server)

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
        sup.on_status_msg = self._on_worker_status
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
            sup.spawn_slave(s.id, cfgs[s.id],
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
        sup.spawn_master(mcfg,
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