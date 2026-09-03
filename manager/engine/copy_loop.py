from __future__ import annotations

from dataclasses import dataclass, field

from manager.engine.models import Position, Snapshot, Event, Record, SymbolInfo
from manager.engine.linkage import magic_for, encode_comment
from manager.engine.transform import SymbolMapper, calculate_slave_lot
from manager.engine.snapshot_diff import diff
from manager.engine.record_table import RecordTable
from manager.engine.baseline import is_too_old, seed_from_recovery
from manager.ipc.messages import CommandMsg, AckMsg, StatusMsg


@dataclass
class SlaveConfig:
    slave_id: str
    symbol_map_csv: str
    step_amount: float
    step_size: float
    max_lot: float
    max_trade_age_minutes: int
    normalize_sltp: bool
    sizing_mode: str = "balance_step"
    master_base_lot: float = 0.0
    fixed_lot: float = 0.01


@dataclass
class SlaveState:
    config: SlaveConfig
    table: RecordTable
    symbol_infos: dict[str, SymbolInfo]
    balance: float
    mapper: SymbolMapper
    pending: set[int] = field(default_factory=set)
    held: dict[int, Event] = field(default_factory=dict)
    info_inflight: set[str] = field(default_factory=set)
    confirmed_missing: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _AwaitInfo:
    """derive_command's signal that a NEW resolved to a slave symbol with no
    SymbolInfo yet: the engine holds the event and requests the info (regex
    rows like '(.+)\\.m=$1' derive slave names only at trade time)."""
    slave_symbol: str


def derive_command(state: SlaveState, event: Event, now: int) -> CommandMsg | _AwaitInfo | None:
    """Derive the command a slave should execute for one diff event, _AwaitInfo
    to hold the event until symbol info arrives, or None to skip. Pure: no I/O,
    no mutation. The slave normalizes SL/TP + computes partial volume, so
    OPEN/MODIFY carry RAW master sl/tp + master_open_price."""
    pos = event.position
    ticket = pos.ticket
    cfg = state.config

    if event.kind == "NEW":
        if state.table.has(ticket):
            return None
        if is_too_old(pos.open_time, now, cfg.max_trade_age_minutes):
            return None
        slave_symbol = state.mapper.resolve(pos.symbol)
        if slave_symbol == "":
            return None
        info = state.symbol_infos.get(slave_symbol)
        if info is None:
            if slave_symbol in state.confirmed_missing:
                return None  # worker said this symbol doesn't exist
            return _AwaitInfo(slave_symbol)
        lots = calculate_slave_lot(cfg.sizing_mode, pos.volume, state.balance,
                                   cfg.step_amount, cfg.step_size,
                                   cfg.master_base_lot, cfg.fixed_lot,
                                   cfg.max_lot, info.volume_step,
                                   info.volume_min, info.volume_max)
        if lots <= 0.0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="OPEN", master_ticket=ticket,
                         symbol=slave_symbol, volume=lots, sl=pos.sl, tp=pos.tp,
                         master_open_price=pos.open_price, side=pos.side,
                         magic=magic_for(ticket),
                         comment=encode_comment(ticket, pos.volume, lots))

    if event.kind == "MODIFY":
        rec = state.table.get(ticket)
        if rec is None or rec.slave_ticket == 0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="MODIFY", master_ticket=ticket,
                         slave_ticket=rec.slave_ticket, sl=pos.sl, tp=pos.tp,
                         master_open_price=pos.open_price, side=pos.side,
                         magic=rec.magic)

    if event.kind == "PARTIAL":
        rec = state.table.get(ticket)
        if rec is None or rec.slave_ticket == 0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="PARTIAL_CLOSE",
                         master_ticket=ticket, slave_ticket=rec.slave_ticket,
                         new_master_volume=pos.volume,
                         master_open_volume=rec.master_open_volume,
                         slave_open_volume=rec.slave_open_volume)

    if event.kind == "CLOSE":
        rec = state.table.get(ticket)
        if rec is None or rec.slave_ticket == 0:
            return None
        return CommandMsg(slave_id=cfg.slave_id, action="CLOSE",
                         master_ticket=ticket, slave_ticket=rec.slave_ticket)

    return None


class CopyEngine:
    """The pure copy brain. Holds per-slave state + the previous master
    snapshot. ingest_snapshot -> per-slave commands; apply_ack updates records
    and re-emits held commands. No I/O."""

    def __init__(self):
        self._slaves: dict[str, SlaveState] = {}
        self._prev: list[Position] = []
        self._last_now: int = 0
        self._info_requests: dict[str, set[str]] = {}

    def add_slave(self, config: SlaveConfig) -> None:
        state = SlaveState(config=config, table=RecordTable(), symbol_infos={},
                           balance=0.0, mapper=None)  # type: ignore[arg-type]
        state.mapper = SymbolMapper(config.symbol_map_csv,
                                    lambda s: s in state.symbol_infos)
        self._slaves[config.slave_id] = state

    def pop_symbol_info_requests(self) -> dict[str, list[str]]:
        """Drain the queue of slave symbols needing on-demand SymbolInfo (built
        up by ingest_snapshot). The caller sends one SymbolInfoRequestMsg per
        slave; each request is queued at most once while its reply is inflight."""
        out = {sid: sorted(syms) for sid, syms in self._info_requests.items()
               if syms}
        self._info_requests.clear()
        return out

    def apply_symbol_info(self, slave_id: str, infos: dict[str, SymbolInfo],
                         requested: list[str] | None = None) -> list[CommandMsg]:
        """Merge reported symbol infos. A reply to our request (`requested`
        non-empty) also settles the inflight request: requested symbols absent
        from the reply are confirmed missing on the slave terminal, and held
        events are re-derived. A bulk map report (`requested` empty) only
        merges infos. Returns commands re-derived from held events."""
        state = self._slaves[slave_id]
        state.symbol_infos.update(infos)
        if not requested:
            return []
        for sym in requested:
            state.info_inflight.discard(sym)
            if sym not in infos:
                state.confirmed_missing.add(sym)
        return self._rederive_held(state)

    def apply_status(self, slave_id: str, status: StatusMsg) -> None:
        self._slaves[slave_id].balance = status.balance

    def apply_recovery(self, slave_id: str, records) -> int:
        return seed_from_recovery(self._slaves[slave_id].table, records)

    def reset_slave(self, slave_id: str) -> None:
        """Clear a slave's table/pending/held on worker restart so recovery
        re-seeds cleanly (no duplicated trades). Symbol info is kept (the slave
        re-sends it); inflight info requests are dropped (their replies died
        with the old worker process)."""
        state = self._slaves[slave_id]
        state.table = RecordTable()
        state.pending.clear()
        state.held.clear()
        state.info_inflight.clear()

    def update_slave_config(self, slave_id: str, *, step_amount: float,
                            step_size: float, max_lot: float,
                            max_trade_age_minutes: int,
                            symbol_map_csv: str,
                            normalize_sltp: bool,
                            sizing_mode: str = "balance_step",
                            master_base_lot: float = 0.0,
                            fixed_lot: float = 0.01) -> bool:
        """Live-update a running slave's config in place. Returns whether
        symbol_map_csv changed (caller may then ask the worker to re-report
        SymbolInfo). Safe for open trades: derive_command routes
        MODIFY/PARTIAL_CLOSE/CLOSE via the RecordTable (slave_ticket + stored
        open volumes), and only NEW reads these fields / the mapper."""
        state = self._slaves[slave_id]
        cfg = state.config
        map_changed = cfg.symbol_map_csv != symbol_map_csv
        cfg.step_amount = step_amount
        cfg.step_size = step_size
        cfg.max_lot = max_lot
        cfg.max_trade_age_minutes = max_trade_age_minutes
        cfg.normalize_sltp = normalize_sltp
        cfg.symbol_map_csv = symbol_map_csv
        cfg.sizing_mode = sizing_mode
        cfg.master_base_lot = master_base_lot
        cfg.fixed_lot = fixed_lot
        if map_changed:
            state.mapper = SymbolMapper(
                symbol_map_csv, lambda s: s in state.symbol_infos)
        return map_changed

    def ingest_snapshot(self, snapshot: Snapshot,
                        now: int) -> dict[str, list[CommandMsg]]:
        events = diff(self._prev, list(snapshot.positions))
        self._prev = list(snapshot.positions)
        self._last_now = now
        out: dict[str, list[CommandMsg]] = {}
        for slave_id, state in self._slaves.items():
            cmds: list[CommandMsg] = []
            for event in events:
                cmd = self._handle_event(state, event, now)
                if cmd is not None:
                    cmds.append(cmd)
            out[slave_id] = cmds
        return out

    def _handle_event(self, state: SlaveState, event: Event,
                      now: int) -> CommandMsg | None:
        ticket = event.position.ticket
        if ticket in state.pending or ticket in state.held:
            state.held[ticket] = event  # coalesce to latest; re-derive when unblocked
            return None
        return self._rederive(state, ticket, event, now=now)

    def _rederive(self, state: SlaveState, ticket: int, event: Event,
                  *, now: int) -> CommandMsg | None:
        """Derive the event into a command and mark it pending, or park it in
        held/_AwaitInfo. The single derive-and-bookkeep path for fresh events
        and re-derives after an ack or an info reply."""
        cmd = derive_command(state, event, now)
        if isinstance(cmd, _AwaitInfo):
            state.held[ticket] = event
            self._queue_info_request(state, cmd.slave_symbol)
            return None
        if cmd is None:
            if (event.kind == "CLOSE" and state.table.has(ticket)
                    and state.table.get(ticket).slave_ticket == 0):
                state.table.remove(ticket)  # failed-open + master closed -> drop marker
            return None
        if cmd.action == "OPEN":
            # optimistic record (slave_ticket=0 until ack) prevents re-NEW
            state.table.add(Record(ticket, cmd.magic, 0, event.position.volume,
                                   cmd.volume))
        state.pending.add(ticket)
        return cmd

    def _queue_info_request(self, state: SlaveState, slave_symbol: str) -> None:
        """Queue an on-demand SymbolInfo request for a regex-resolved slave
        symbol, unless one is already inflight or the symbol is known missing."""
        if (slave_symbol in state.info_inflight
                or slave_symbol in state.confirmed_missing):
            return
        state.info_inflight.add(slave_symbol)
        self._info_requests.setdefault(state.config.slave_id, set()).add(
            slave_symbol)

    def _rederive_held(self, state: SlaveState) -> list[CommandMsg]:
        """Re-derive every held event that is not still awaiting an ack. Used
        when a symbol-info reply unblocks NEW events."""
        cmds: list[CommandMsg] = []
        for ticket in list(state.held):
            if ticket in state.pending:
                continue
            event = state.held.pop(ticket)
            cmd = self._rederive(state, ticket, event, now=self._last_now)
            if cmd is not None:
                cmds.append(cmd)
        return cmds

    def apply_ack(self, slave_id: str, ack: AckMsg) -> list[CommandMsg]:
        state = self._slaves[slave_id]
        ticket = ack.master_ticket
        rec = state.table.get(ticket)
        if ack.action == "OPEN":
            if rec is not None and ack.ok:
                rec.slave_ticket = ack.slave_ticket
                rec.slave_open_volume = ack.fill_volume
            # on failure: leave slave_ticket=0 marker (not re-NEW'd)
        elif ack.action == "CLOSE":
            if ack.ok and rec is not None:
                state.table.remove(ticket)
        # MODIFY / PARTIAL_CLOSE: no record-table change (slave holds live volume)
        state.pending.discard(ticket)
        held_event = state.held.pop(ticket, None)
        if held_event is None:
            return []
        cmd = self._rederive(state, ticket, held_event, now=self._last_now)
        return [cmd] if cmd is not None else []