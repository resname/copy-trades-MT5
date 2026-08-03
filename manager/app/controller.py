# manager/app/controller.py
from __future__ import annotations

import time
from dataclasses import dataclass

from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.engine.models import BUY, SELL  # noqa: F401  (re-exported for GUI)
from manager.supervisor import Supervisor
from manager.terminal.discovery import TerminalInstance
from manager.settings.store import SettingsStore


class ControllerError(Exception):
    """Raised before Start for unrecoverable config: duplicate/unresolvable
    terminal-path assignments, or not enough instances after assignment."""


@dataclass
class AccountSpec:
    id: str
    terminal_path: str
    symbol_map_csv: str = ""
    step_amount: float = 100.0
    step_size: float = 0.01
    max_lot: float = 10.0
    max_trade_age_minutes: float = 10.0
    normalize_sltp: bool = True


@dataclass
class StatusUpdate:
    kind: str            # "info" | "error" | "ready" | "slave_status"
    message: str
    slave_id: str | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None


def _normalize_override_exe(path: str) -> str:
    """An assignment may point at the install dir or directly at terminal64.exe.
    Normalize to the exe path."""
    p = path.replace("\\", "/")
    if p.endswith("terminal64.exe"):
        return p
    return p.rstrip("/") + "/terminal64.exe"


class CopyController:
    """Non-GUI orchestrator. Wires TerminalManager + Supervisor + CopyEngine.
    The GUI is a thin view over this. All Start/Stop logic lives here so it is
    unit-testable with the real Supervisor + FakeMt5 and a fake terminal manager
    (no Qt, no real install)."""

    def __init__(self, terminal_manager, store=None,
                 supervisor_factory=None,
                 on_status=None, on_log=None, clock=time.time):
        self._terminal_manager = terminal_manager
        self._store = store if store is not None else SettingsStore()
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

    # ---- discovery / assignment ----
    def discover_instances(self) -> list[TerminalInstance]:
        return self._terminal_manager.discover_all()

    def prepare(self, master: AccountSpec, slaves: list[AccountSpec]
                ) -> dict[str, TerminalInstance]:
        """Validate terminal-path assignments (uniqueness + normalize to exe
        path) and assign one instance per account. Raises ControllerError on
        duplicate/unresolvable assignments. No auto-provisioning — the user
        brings terminals they have already installed and logged in to."""
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
        self._status("info", "assigning terminal instances…")
        assigned = self._terminal_manager.assign(accounts)
        self._status("info", "terminal instances assigned")
        return assigned

    @staticmethod
    def _account_dict(a: AccountSpec) -> dict:
        return {"id": a.id, "terminal_path": a.terminal_path}

    # ---- worker config building (testable independently) ----
    def build_worker_configs(self, master: AccountSpec, slaves: list[AccountSpec],
                             assigned: dict[str, TerminalInstance]
                             ) -> dict[str, dict]:
        """Build the per-account worker config dicts the Supervisor spawns."""
        cfgs: dict[str, dict] = {}
        m_inst = assigned[master.id]
        cfgs[master.id] = {
            "terminal_path": m_inst.exe_path,
            "master_interval_ms": 1000,
        }
        for s in slaves:
            s_inst = assigned[s.id]
            cfgs[s.id] = {
                "slave_id": s.id,
                "terminal_path": s_inst.exe_path,
                "symbol_map_csv": s.symbol_map_csv,
                "normalize_sltp": s.normalize_sltp,
                "retry_count": 3, "retry_delay_ms": 500,
                "slave_status_interval_ms": 5000,
            }
        return cfgs

    # ---- supervisor construction (testable independently) ----
    def build_supervisor(self, heartbeat_seconds: int = 5) -> Supervisor:
        eng = CopyEngine()
        self._engine = eng
        sup = self._supervisor_factory(
            eng, heartbeat_seconds=heartbeat_seconds,
            kill_terminal=self._terminal_manager.kill_terminal)
        sup.on_restart = lambda name, role: self._status(
            "info", f"restarted {role} {name}")
        sup.on_error = lambda name, message: self._on_supervisor_error(name, message)
        return sup

    def _on_supervisor_error(self, name: str, message: str) -> None:
        """Worker/runtime errors (fatal initialize failures, crashes, lost
        heartbeat) -> status (error) + log, so the user sees the reason a
        worker stopped instead of a silent terminal open/close cycle."""
        self._status("error", message)
        self._log(message)

    # ---- the Start sequence ----
    def start(self, master: AccountSpec, slaves: list[AccountSpec],
              master_fake_state=None, slave_fake_state=None) -> None:
        """The full Start sequence:
          1. prepare (validate/assign)
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