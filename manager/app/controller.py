# manager/app/controller.py
from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from uuid import uuid4

from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.engine.models import BUY, SELL  # noqa: F401  (re-exported for GUI)
from manager.engine.transform import parse_symbol_map
from manager.supervisor import Supervisor
from manager.terminal.discovery import TerminalInstance
from manager.settings.store import SettingsStore


class ControllerError(Exception):
    """Raised before Start for unrecoverable config: duplicate/unresolvable
    terminal-path assignments, or not enough instances after assignment."""


class AlgoTradingDisabledError(ControllerError):
    """Raised by start() when one or more terminals report Algo Trading
    disabled (mt5.terminal_info().trade_allowed is False). Start is blocked
    before any copy is attempted; the GUI shows a modal message box so the
    user enables the Algo Trading button and retries."""
    def __init__(self, terminals: list[str]):
        self.terminals = list(terminals)
        names = ", ".join(self.terminals)
        super().__init__(
            f"Algo Trading is disabled on: {names}. Enable the 'Algo Trading' "
            f"button in each MetaTrader terminal (toolbar, or Tools -> Options "
            f"-> Expert Advisors -> Allow algorithmic trading), then click Start.")


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
    sizing_mode: str = "balance_step"
    master_base_lot: float = 0.0
    fixed_lot: float = 0.01
    profile_id: str = ""


SHARED_PROFILE_FIELDS = (
    "symbol_map_csv", "step_amount", "step_size", "max_lot",
    "max_trade_age_minutes", "normalize_sltp", "sizing_mode",
    "master_base_lot", "fixed_lot",
)


@dataclass
class CopyProfile:
    id: str
    name: str
    symbol_map_csv: str = ""
    step_amount: float = 100.0
    step_size: float = 0.01
    max_lot: float = 10.0
    max_trade_age_minutes: float = 10.0
    normalize_sltp: bool = True
    sizing_mode: str = "balance_step"
    master_base_lot: float = 0.0
    fixed_lot: float = 0.01


def profile_from_spec(profile_id: str, name: str, spec: AccountSpec) -> CopyProfile:
    """Capture an account's shared (non-identity) trading fields as a profile."""
    return CopyProfile(id=profile_id, name=name,
                       **{f: getattr(spec, f) for f in SHARED_PROFILE_FIELDS})


def apply_profile(spec: AccountSpec, profile: CopyProfile) -> AccountSpec:
    """Return a new spec with shared fields from profile; identity (id +
    terminal_path) is preserved and profile_id is set."""
    return replace(spec, profile_id=profile.id,
                   **{f: getattr(profile, f) for f in SHARED_PROFILE_FIELDS})


def new_profile_id(existing_ids: Iterable[str] = ()) -> str:
    """Draw a short unique id (8 hex chars), avoiding existing_ids."""
    existing = set(existing_ids)
    while True:
        pid = uuid4().hex[:8]
        if pid not in existing:
            return pid


def unique_profile_name(name: str, existing_names: Iterable[str], *,
                        fallback: str = "Profile") -> str:
    """Return a trimmed name that does not collide (case-insensitively) with
    existing_names, falling back when empty and suffixing ' 2', ' 3', …."""
    base = name.strip() or fallback
    taken = {n.casefold() for n in existing_names}
    if base.casefold() not in taken:
        return base
    i = 2
    while f"{base} {i}".casefold() in taken:
        i += 1
    return f"{base} {i}"


_NUMERIC_PROFILE_FIELDS = (
    "step_amount", "step_size", "max_lot", "max_trade_age_minutes",
    "master_base_lot", "fixed_lot",
)
_SIZING_MODES = frozenset(("balance_step", "copy_master", "fixed_lot"))


def coerce_profiles(raw: object, *,
                    log: Callable[[str], None] | None = None
                    ) -> list[CopyProfile]:
    """Validate stored (settings-JSON) profile dicts into CopyProfiles.

    Returns [] unless raw is a list. Absent fields take CopyProfile defaults;
    present fields are validated, and an invalid entry is dropped whole. Names
    are trimmed and made non-empty/case-insensitively unique; later duplicate
    ids are dropped. Every drop/fix is reported through log when supplied."""
    if not isinstance(raw, list):
        return []
    emit = log if log is not None else (lambda message: None)
    profiles: list[CopyProfile] = []
    seen_ids: set[str] = set()
    names: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            emit("profile ? dropped: not an object")
            continue
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            emit("profile ? dropped: missing or invalid id")
            continue
        if pid in seen_ids:
            emit(f"profile {pid} dropped: duplicate id")
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            emit(f"profile {pid} dropped: missing or invalid name")
            continue
        kwargs: dict[str, object] = {}
        reason: str | None = None
        if "symbol_map_csv" in entry:
            csv = entry["symbol_map_csv"]
            if not isinstance(csv, str):
                reason = "symbol_map_csv is not a string"
            else:
                kwargs["symbol_map_csv"] = csv
        if reason is None:
            for field in _NUMERIC_PROFILE_FIELDS:
                if field not in entry:
                    continue
                value = entry[field]
                if isinstance(value, bool):
                    reason = f"{field} is not a number"
                    break
                try:
                    converted = float(value)
                except (TypeError, ValueError, OverflowError):
                    reason = f"{field} is not a number"
                    break
                # float() accepts nan/inf and a JSON float such as 1e400 is
                # already inf; a non-finite lot would raise later in the
                # engine/supervisor, so drop the profile here.
                if not math.isfinite(converted):
                    reason = f"{field} is not a finite number"
                    break
                kwargs[field] = converted
        if reason is None and "normalize_sltp" in entry:
            value = entry["normalize_sltp"]
            if not isinstance(value, bool):
                reason = "normalize_sltp is not a bool"
            else:
                kwargs["normalize_sltp"] = value
        if reason is None and "sizing_mode" in entry:
            value = entry["sizing_mode"]
            if not isinstance(value, str) or value not in _SIZING_MODES:
                reason = "invalid sizing_mode"
            else:
                kwargs["sizing_mode"] = value
        if reason is not None:
            emit(f"profile {pid} dropped: {reason}")
            continue
        final_name = unique_profile_name(name, names,
                                         fallback=f"Profile {pid[:4]}")
        if final_name != name:
            emit(f"profile {pid} renamed to {final_name!r}")
        names.append(final_name)
        seen_ids.add(pid)
        profiles.append(CopyProfile(id=pid, name=final_name, **kwargs))
    return profiles


def _numeric_key(value: object) -> object:
    """Normalize a legacy numeric value for comparison: stored configs may
    hold "100.0" while another slave holds 100.0, and both are the same
    setting. Non-numeric values fall back to the raw value."""
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return value


def migrate_legacy_profiles(slaves: list[AccountSpec]
                            ) -> tuple[list[CopyProfile], dict[str, str]]:
    """Group legacy slaves with identical shared settings into profiles.

    Slaves whose shared fields (and symbol maps, compared as parsed mappings)
    all match are grouped; groups of 2+ get a generated profile, lone slaves
    are left alone. Returns (new profiles, {slave_id: profile_id})."""
    groups: dict[tuple, list[AccountSpec]] = {}
    for spec in slaves:
        try:
            parsed = parse_symbol_map(spec.symbol_map_csv)
            symbol_map = tuple(sorted(parsed.items()))
        except Exception:
            raw_csv = spec.symbol_map_csv
            # The fallback is used in a dict key, so an unhashable value from
            # corrupt settings JSON (e.g. a list) must be made hashable.
            symbol_map = raw_csv if isinstance(raw_csv, str) else repr(raw_csv)
        key = (
            _numeric_key(spec.step_amount), _numeric_key(spec.step_size),
            _numeric_key(spec.max_lot),
            _numeric_key(spec.max_trade_age_minutes),
            _numeric_key(spec.master_base_lot),
            _numeric_key(spec.fixed_lot),
            spec.normalize_sltp, spec.sizing_mode, symbol_map,
        )
        groups.setdefault(key, []).append(spec)
    profiles: list[CopyProfile] = []
    mapping: dict[str, str] = {}
    minted_ids: list[str] = []
    minted_names: list[str] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        pid = new_profile_id(minted_ids)
        minted_ids.append(pid)
        first = group[0]
        if len(group) == 2:
            base_name = f"{first.id} + {group[1].id}"
        else:
            base_name = f"{first.id} + {len(group) - 1} more"
        name = unique_profile_name(base_name, minted_names)
        minted_names.append(name)
        profiles.append(profile_from_spec(pid, name, first))
        for spec in group:
            mapping[spec.id] = pid
    return profiles, mapping


@dataclass
class StatusUpdate:
    kind: str            # "info" | "error" | "ready"
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
        mt5_accounts = [self._account_dict(master)]
        for s in slaves:
            mt5_accounts.append(self._account_dict(s))
        for a in mt5_accounts:
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
        assigned = self._terminal_manager.assign(mt5_accounts)
        self._status("info", "terminal instances assigned")
        return assigned

    @staticmethod
    def _account_dict(a: AccountSpec) -> dict:
        return {"id": a.id, "terminal_path": a.terminal_path}

    # ---- worker config building (testable independently) ----
    def build_worker_configs(self, master: AccountSpec, slaves: list[AccountSpec],
                             assigned: dict[str, TerminalInstance]
                             ) -> dict[str, dict]:
        """Build the per-account worker config dicts the Supervisor spawns.

        MT5 slave configs deliberately gain no "platform" key: the supervisor
        defaults to MT5 on absence, which keeps pre-existing configs
        byte-identical."""
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
                normalize_sltp=s.normalize_sltp,
                sizing_mode=s.sizing_mode, master_base_lot=s.master_base_lot,
                fixed_lot=s.fixed_lot))
        cfgs = self.build_worker_configs(master, slaves, assigned)
        self._status("info", "starting slave workers…")
        for s in slaves:
            sup.spawn_slave(s.id, cfgs[s.id],
                            adapter_kind="real" if slave_fake_state is None else "fake",
                            fake_state=slave_fake_state)
        # readiness gate: wait for every slave's SymbolInfo + first Status
        ready = sup.wait_for_slaves_ready(timeout=90.0)
        if not ready:
            self._status("error", "one or more slaves did not become ready")
            sup.shutdown()
            self._supervisor = None
            raise ControllerError("slaves not ready within timeout")
        # Algo-Trading preflight (slaves): block before any copy is attempted.
        # A slave with Algo Trading off would silently accept commands whose
        # order_send is blocked (retcode 10030/invalid) — surface it now.
        disabled = [(s.id, cfgs[s.id].get("terminal_path", "?"))
                    for s in slaves if not sup.slave_trade_allowed(s.id)]
        if disabled:
            names = [f"{sid} ({path})" for sid, path in disabled]
            self._status("error", "Algo Trading is disabled on: " + ", ".join(names))
            sup.shutdown()
            self._supervisor = None
            raise AlgoTradingDisabledError(names)
        self._status("ready", "slaves ready; starting master")
        mcfg = cfgs[master.id]
        sup.spawn_master(mcfg,
                         adapter_kind="real" if master_fake_state is None else "fake",
                         fake_state=master_fake_state)
        # Algo-Trading preflight (master): wait for the master's first StatusMsg
        # (it sends status before any snapshot) and require trade_allowed=True.
        if not sup.wait_for_master_status(timeout=90.0):
            self._status("error", "master did not report status in time")
            sup.shutdown()
            self._supervisor = None
            raise ControllerError("master did not report status within timeout")
        if not sup.master_trade_allowed():
            name = f"master ({cfgs[master.id]['terminal_path']})"
            self._status("error", "Algo Trading is disabled on: " + name)
            sup.shutdown()
            self._supervisor = None
            raise AlgoTradingDisabledError([name])
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

    def apply_slave_edit(self, slave_id: str, spec: AccountSpec) -> None:
        """Apply an edited slave's trading params live. No-op when not running
        (the GUI has already updated _slaves + saved config; it applies on the
        next Start via build_worker_configs). When running, update the engine
        config in-process (future opens use the new step/size/max_lot/max_age +
        new symbol map) and push normalize + symbol-info-refresh to the worker.
        Open trades are never modified: derive_command routes MODIFY/PARTIAL/
        CLOSE via the RecordTable."""
        if self._supervisor is None or self._engine is None:
            return
        self._engine.update_slave_config(
            slave_id, step_amount=spec.step_amount, step_size=spec.step_size,
            max_lot=spec.max_lot, max_trade_age_minutes=spec.max_trade_age_minutes,
            symbol_map_csv=spec.symbol_map_csv, normalize_sltp=spec.normalize_sltp,
            sizing_mode=spec.sizing_mode, master_base_lot=spec.master_base_lot,
            fixed_lot=spec.fixed_lot)
        self._supervisor.reconfigure_slave(
            slave_id, spec.symbol_map_csv, spec.normalize_sltp)

    def is_running(self) -> bool:
        return self._supervisor is not None and self._supervisor._thread is not None \
            and self._supervisor._thread.is_alive()