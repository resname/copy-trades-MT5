from __future__ import annotations

import math
import time
import traceback

from manager.engine.models import Position, Record, BUY, SELL
from manager.engine.linkage import (
    magic_for, decode_comment, encode_comment, MAGIC_BASE, MAGIC_MOD,
)
from manager.engine.transform import (
    normalize_sltp as _normalize_sltp, round_to_tick, parse_symbol_map,
    split_volume,
)
from manager.ipc.messages import (
    AckMsg, ErrorMsg, SnapshotMsg, StatusMsg, SymbolInfoMsg, RecoveryMsg,
    ReconfigureMsg, SymbolInfoRequestMsg,
)
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.mt5_adapter import FakeMt5, RealMt5
from manager.worker.mt5_constants import (
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, ORDER_TYPE_BUY, ORDER_TYPE_SELL,
    ORDER_TIME_GTC, TRADE_RETCODE_DONE, select_filling_mode,
)


def build_snapshot(adapter, heartbeat: int, now: int) -> SnapshotMsg:
    return SnapshotMsg(source_id="master", timestamp=now, heartbeat=heartbeat,
                       positions=tuple(adapter.positions_get()))


def build_recovery_records(adapter) -> list[Record]:
    """Scan this terminal's open positions for copied positions (CPY#..|MV|SV
    comment) and rebuild linkage records. Used on (re)start so the manager
    seeds its RecordTable and never duplicates. Skips positions whose comment
    is missing or lacks both MV+SV (cannot compute partial fractions).

    A copied position is identified by its CPY comment: the master ticket is
    read from the comment and the linkage magic is recomputed as
    ``magic_for(master_ticket)``. In production ``Position.magic`` (populated by
    RealMt5 / set from the request by FakeMt5) agrees with that value; tests that
    construct positions directly leave magic=0, so the comment is the
    authoritative marker and the magic is derived from it.
    """
    out: list[Record] = []
    for p in adapter.positions_get():
        decoded = decode_comment(p.comment)
        if decoded is None:
            continue
        master_ticket, mv, sv = decoded
        if mv is None or sv is None:
            continue
        magic = magic_for(master_ticket)
        if not (MAGIC_BASE <= magic < MAGIC_BASE + MAGIC_MOD):
            # defensive: magic_for always lands in range, so this never fires
            continue
        out.append(Record(master_ticket=master_ticket, magic=magic,
                          slave_ticket=p.ticket, master_open_volume=mv,
                          slave_open_volume=sv))
    return out


def build_symbol_info_msg(adapter, slave_id: str, symbol_map_csv: str) -> SymbolInfoMsg:
    """Report symbol info for the slave symbols named in the symbol map (the
    manager needs volume params for lot-sizing). Same-name fallback symbols not
    in the map are not reported here (forward-looking: on-demand request)."""
    infos: dict[str, object] = {}
    for slave_symbol in parse_symbol_map(symbol_map_csv).values():
        info = adapter.symbol_info(slave_symbol)
        if info is not None:
            infos[slave_symbol] = info
    return SymbolInfoMsg(source_id=slave_id, infos=infos)


def build_symbol_info_reply(adapter, slave_id: str,
                            symbols: list[str]) -> SymbolInfoMsg:
    """Answer a SymbolInfoRequestMsg: report info for each requested symbol,
    naming them all in `requested` so the manager can settle its inflight
    request. Requested symbols absent from `infos` are confirmed missing on
    this terminal (the manager skips their held NEWs instead of re-asking)."""
    infos: dict[str, object] = {}
    for symbol in symbols:
        info = adapter.symbol_info(symbol)
        if info is not None:
            infos[symbol] = info
    return SymbolInfoMsg(source_id=slave_id, infos=infos,
                         requested=list(symbols))


def _status(adapter, source_id: str, role: str, connected: bool) -> StatusMsg:
    acc = adapter.account_info()
    ti = adapter.terminal_info()
    return StatusMsg(source_id=source_id, role=role, connected=connected,
                    login=int(acc.get("login", 0)), balance=float(acc.get("balance", 0.0)),
                    equity=float(acc.get("equity", 0.0)),
                    currency=str(acc.get("currency", "")),
                    server=str(acc.get("server", "")),
                    trade_allowed=bool(ti.get("trade_allowed", True)))


def slave_init(adapter, config: dict):
    """Recovery + symbol-info + status emitted by a slave on connect.
    Returns (RecoveryMsg, SymbolInfoMsg, StatusMsg)."""
    slave_id = config["slave_id"]
    records = build_recovery_records(adapter)
    rec_msg = RecoveryMsg(source_id=slave_id, records=tuple(records))
    si_msg = build_symbol_info_msg(adapter, slave_id, config.get("symbol_map_csv", ""))
    st_msg = _status(adapter, slave_id, "slave", connected=True)
    return rec_msg, si_msg, st_msg


def _order_send_with_retry(adapter, request: dict, retry_count: int,
                           retry_delay_ms: int) -> dict:
    result: dict = {"retcode": -1, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0}
    for attempt in range(max(1, retry_count)):
        result = adapter.order_send(request)
        if result.get("retcode") == TRADE_RETCODE_DONE:
            return result
        if attempt < retry_count - 1 and retry_delay_ms > 0:
            time.sleep(retry_delay_ms / 1000.0)
    return result


def _round_sltp(sl: float, tp: float, tick_size: float, digits: int):
    rsl = round_to_tick(sl, tick_size, digits)
    rtp = round_to_tick(tp, tick_size, digits)
    if rsl is None or rtp is None:
        return None
    return rsl, rtp


def _find_legs(adapter, cmd) -> list[Position]:
    """Slave legs of one master trade: the UNION of (a) positions whose
    decoded CPY comment names this master ticket (the split-OPEN writer's
    per-leg comments) and (b) today's exact per-ticket position
    `cmd.slave_ticket`. The union means a leg whose comment the broker
    mangled is still reached when the record's slave_ticket names it (spec
    builder-check decision #1): in every single-leg case (b) is already the
    scan's result, and in fan-out the record linkage guarantees
    slave_ticket belongs to this master."""
    legs: dict[int, Position] = {}
    for p in adapter.positions_get():
        d = decode_comment(p.comment)
        if d and d[0] == cmd.master_ticket:
            legs[p.ticket] = p
    pos = adapter.position_by_ticket(cmd.slave_ticket)
    if pos is not None:
        legs[pos.ticket] = pos  # union; dedup keeps one object per ticket
    return list(legs.values())


def execute_command(adapter, cmd, normalize_sltp: bool, retry_count: int,
                    retry_delay_ms: int, heartbeat=None) -> AckMsg:
    """Execute one CommandMsg against the terminal. The slave normalizes SL/TP
    to its live/fill price and computes partial-close volume from its live
    current position (EA-faithful). Returns an AckMsg.

    For an OPEN whose volume exceeds the slave symbol's volume_max, the order
    is split into volume_max chunks (split_volume) with a per-leg comment
    sharing the master ticket + MV; each chunk is sent in sequence, stopping at
    the first rejected chunk and counting only actual fills. `heartbeat` (the
    _slave_loop's status-emitting closure) is called before each chunk after
    the first so the supervisor's stale timer never fires mid-split."""
    def _fail(retcode: int, err: str) -> AckMsg:
        return AckMsg(slave_id=cmd.slave_id, action=cmd.action,
                      master_ticket=cmd.master_ticket, ok=False, retcode=retcode,
                      error=err)

    if cmd.action == "OPEN":
        tick = adapter.symbol_info_tick(cmd.symbol)
        if tick is None:
            return _fail(-1, f"no tick for {cmd.symbol}")
        bid, ask = tick
        slave_open = ask if cmd.side == BUY else bid
        info = adapter.symbol_info(cmd.symbol)
        if info is None:
            return _fail(-1, f"no symbol info for {cmd.symbol}")
        if normalize_sltp:
            sl, tp = _normalize_sltp(cmd.master_open_price, cmd.sl, cmd.tp,
                                   slave_open, cmd.side)
        else:
            sl, tp = cmd.sl, cmd.tp
        rounded = _round_sltp(sl, tp, info.tick_size, info.digits)
        if rounded is None:
            return _fail(-1, "SL/TP tick rounding failed")
        sl, tp = rounded
        req = {"action": TRADE_ACTION_DEAL, "symbol": cmd.symbol,
               "type": ORDER_TYPE_BUY if cmd.side == BUY else ORDER_TYPE_SELL,
               "volume": cmd.volume, "price": slave_open, "sl": sl, "tp": tp,
               "deviation": 10, "magic": cmd.magic, "comment": cmd.comment,
               "type_time": ORDER_TIME_GTC,
               "type_filling": select_filling_mode(info.filling_mode)}
        parts: list[float] | None = None
        mv: float | None = None
        if cmd.volume > info.volume_max + 1e-9:
            decoded = decode_comment(cmd.comment)
            mv = decoded[1] if decoded else None
            if mv is None:
                return _fail(-1, "cannot split: comment MV/SV undecodable")
            parts = split_volume(cmd.volume, info.volume_max, info.volume_step,
                                 info.volume_min)
            if len(parts) <= 1:
                parts = None  # unusable volume_max/step -> legacy single order
        if parts is None:
            res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
            if res.get("retcode") != TRADE_RETCODE_DONE:
                return _fail(res.get("retcode", -1), str(adapter.last_error()))
            return AckMsg(slave_id=cmd.slave_id, action="OPEN",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=res["order"], fill_price=res["price"],
                          fill_volume=res["volume"], remaining_volume=res["volume"],
                          retcode=TRADE_RETCODE_DONE)
        # Split path: one request per chunk, sharing the template. Each leg
        # carries its own SV in the comment; MV is the master's volume so
        # recovery/partial-close math stays whole-position.
        filled = 0.0
        first_ticket = 0
        filled_chunks = 0
        for i, part in enumerate(parts):
            if i > 0 and heartbeat is not None:
                heartbeat()  # status heartbeat so the supervisor's stale timer never fires mid-split
            req_part = dict(req, volume=part,
                            comment=encode_comment(cmd.master_ticket, mv, part))
            res = _order_send_with_retry(adapter, req_part, retry_count,
                                         retry_delay_ms)
            if res.get("retcode") != TRADE_RETCODE_DONE:
                break  # stop at first failing chunk; keep already-filled legs
            filled += res.get("volume") or 0.0  # actual fill only, not the request
            if first_ticket == 0:
                first_ticket = res["order"]
            filled_chunks += 1
        if filled <= 0.0:
            retcode = res.get("retcode", -1)
            if retcode == TRADE_RETCODE_DONE:
                retcode = -1  # ok=False must never carry a DONE retcode
            return _fail(retcode, str(adapter.last_error()))
        note = f"split: {filled_chunks}/{len(parts)} ok"
        if len(parts) > 20:
            note += f" (warning: huge split: {len(parts)} legs)"
        if filled < cmd.volume - 1e-9:
            note += f" (shortfall: requested {cmd.volume}, filled {filled})"
        return AckMsg(slave_id=cmd.slave_id, action="OPEN",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=first_ticket, fill_price=res["price"],
                      fill_volume=filled, remaining_volume=filled,
                      retcode=TRADE_RETCODE_DONE, error=note)

    if cmd.action == "MODIFY":
        legs = _find_legs(adapter, cmd)
        if not legs:
            return _fail(-1, f"position #{cmd.slave_ticket} gone")
        for pos in legs:  # same normalize + tick-round sequence per leg
            info = adapter.symbol_info(pos.symbol)
            if info is None:
                return _fail(-1, f"no symbol info for {pos.symbol}")
            slave_open = pos.open_price
            if normalize_sltp:
                sl, tp = _normalize_sltp(cmd.master_open_price, cmd.sl, cmd.tp,
                                       slave_open, cmd.side)
            else:
                sl, tp = cmd.sl, cmd.tp
            rounded = _round_sltp(sl, tp, info.tick_size, info.digits)
            if rounded is None:
                return _fail(-1, "SL/TP tick rounding failed")
            sl, tp = rounded
            req = {"action": TRADE_ACTION_SLTP, "symbol": pos.symbol,
                   "position": pos.ticket, "sl": sl, "tp": tp}
            res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
            if res.get("retcode") != TRADE_RETCODE_DONE:
                return _fail(res.get("retcode", -1), str(adapter.last_error()))
        return AckMsg(slave_id=cmd.slave_id, action="MODIFY",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, retcode=TRADE_RETCODE_DONE)

    if cmd.action == "PARTIAL_CLOSE":
        legs = _find_legs(adapter, cmd)
        if not legs:
            return _fail(-1, f"position #{cmd.slave_ticket} gone")
        if cmd.master_open_volume <= 0.0:
            return _fail(-1, "cannot compute partial fraction")
        fraction = cmd.new_master_volume / cmd.master_open_volume
        fill_total = 0.0
        for pos in legs:
            info = adapter.symbol_info(pos.symbol)
            if info is None:
                return _fail(-1, "cannot compute partial fraction")
            current = pos.volume
            decoded = decode_comment(pos.comment)
            # per-leg pro-rata: each leg targets its OWN SV * fraction; the
            # record aggregate is only the fallback for an undecodable legacy leg
            sv = decoded[2] if (decoded is not None and decoded[2] is not None) \
                else cmd.slave_open_volume
            target = sv * fraction
            # +1e-9: float-epsilon guard (same as _snap_clamp) -- a delta sitting
            # epsilon below N steps is N steps; naive floor short-closes or skips
            vol_to_close = math.floor((current - target) / info.volume_step + 1e-9) * info.volume_step
            vol_to_close = min(vol_to_close, current)
            if vol_to_close <= 0.0:
                continue  # already at (or below) its target: nothing to close
            tick = adapter.symbol_info_tick(pos.symbol)
            if tick is None:
                return _fail(-1, f"no tick for {pos.symbol}")
            bid, ask = tick
            opposite = ORDER_TYPE_SELL if pos.side == BUY else ORDER_TYPE_BUY
            price = bid if pos.side == BUY else ask
            req = {"action": TRADE_ACTION_DEAL, "symbol": pos.symbol,
                   "type": opposite, "volume": vol_to_close,
                   "position": pos.ticket, "price": price,
                   "deviation": 10, "magic": pos.magic, "comment": "partial",
                   "type_time": ORDER_TIME_GTC,
                   "type_filling": select_filling_mode(info.filling_mode)}
            res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
            if res.get("retcode") != TRADE_RETCODE_DONE:
                return _fail(res.get("retcode", -1), str(adapter.last_error()))
            fill_total += vol_to_close
        remaining = 0.0  # sum of each leg's live volume (gone -> 0.0)
        for pos in legs:
            live = adapter.position_by_ticket(pos.ticket)
            remaining += live.volume if live is not None else 0.0
        return AckMsg(slave_id=cmd.slave_id, action="PARTIAL_CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, fill_volume=fill_total,
                      remaining_volume=remaining, retcode=TRADE_RETCODE_DONE)

    if cmd.action == "CLOSE":
        legs = _find_legs(adapter, cmd)
        if not legs:
            # all legs gone upfront: today's vanished-position ok-ack
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket, retcode=TRADE_RETCODE_DONE)
        for pos in legs:
            live = adapter.position_by_ticket(pos.ticket)
            if live is None:
                continue  # vanished mid-fan-out (already closed): counts as closed
            pos = live  # current snapshot, as today's single-position fetch
            tick = adapter.symbol_info_tick(pos.symbol)
            if tick is None:
                return _fail(-1, f"no tick for {pos.symbol}")
            bid, ask = tick
            opposite = ORDER_TYPE_SELL if pos.side == BUY else ORDER_TYPE_BUY
            price = bid if pos.side == BUY else ask
            info = adapter.symbol_info(pos.symbol)
            filling = select_filling_mode(info.filling_mode) if info is not None \
                else select_filling_mode(0)
            req = {"action": TRADE_ACTION_DEAL, "symbol": pos.symbol, "type": opposite,
                   "volume": pos.volume, "position": pos.ticket, "price": price,
                   "deviation": 10, "magic": pos.magic, "comment": "close",
                   "type_time": ORDER_TIME_GTC, "type_filling": filling}
            res = _order_send_with_retry(adapter, req, retry_count, retry_delay_ms)
            if res.get("retcode") != TRADE_RETCODE_DONE:
                return _fail(res.get("retcode", -1), str(adapter.last_error()))
        return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, retcode=TRADE_RETCODE_DONE)

    return _fail(-1, f"unknown action {cmd.action!r}")


def _master_loop(pipe, adapter, config):
    source_id = config.get("slave_id", "master")
    send_msg(pipe, _status(adapter, source_id, "master", connected=True))
    heartbeat = 0
    interval = config.get("master_interval_ms", 1000) / 1000.0
    while True:
        snap = build_snapshot(adapter, heartbeat, now=int(time.time()))
        try:
            send_msg(pipe, snap)
        except (EOFError, OSError):
            return  # manager gone
        heartbeat += 1
        time.sleep(interval)


def _slave_loop(pipe, adapter, config):
    slave_id = config["slave_id"]
    rec_msg, si_msg, st_msg = slave_init(adapter, config)
    send_msg(pipe, rec_msg)
    send_msg(pipe, si_msg)
    send_msg(pipe, st_msg)
    normalize = bool(config.get("normalize_sltp", True))
    retry_count = int(config.get("retry_count", 3))
    retry_delay = int(config.get("retry_delay_ms", 500))
    status_interval = float(config.get("slave_status_interval_ms", 5000)) / 1000.0
    last_status = time.time()
    poll_timeout = min(1.0, status_interval)

    def _heartbeat():
        try:
            send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
        except (EOFError, OSError):
            pass  # manager gone: the next real send exits the loop

    while True:
        try:
            has_cmd = pipe.poll(poll_timeout)
        except (EOFError, OSError):
            # Windows: poll on a peer-closed empty pipe raises
            # BrokenPipeError; clean close is EOFError. Either way the
            # manager is gone — same graceful shutdown worker_main grants
            # on EOF (direct invocations, e.g. tests running this loop in
            # a bare thread, have no outer catcher).
            return
        if has_cmd:
            try:
                cmd = recv_msg(pipe)  # raises EOFError on manager close
            except (EOFError, OSError):
                return  # manager closed the pipe mid-frame
            if isinstance(cmd, ReconfigureMsg):
                # Live reconfigure: update this loop's params and re-report the
                # symbol info for the NEW map's slave symbols. Open positions
                # are unaffected (MODIFY/CLOSE route by slave_ticket). No ack.
                normalize = cmd.normalize_sltp
                symbol_map_csv = cmd.symbol_map_csv
                try:
                    send_msg(pipe, build_symbol_info_msg(adapter, slave_id,
                                                         symbol_map_csv))
                except (EOFError, OSError):
                    return  # manager gone
                last_status = time.time()
                continue
            if isinstance(cmd, SymbolInfoRequestMsg):
                # On-demand info for regex-derived slave symbols: the reply
                # names every requested symbol so the manager settles its
                # request (absent = confirmed missing). No ack.
                try:
                    send_msg(pipe, build_symbol_info_reply(adapter, slave_id,
                                                           cmd.symbols))
                except (EOFError, OSError):
                    return  # manager gone
                last_status = time.time()
                continue
            ack = execute_command(adapter, cmd, normalize, retry_count, retry_delay,
                                  heartbeat=_heartbeat)
            try:
                send_msg(pipe, ack)
                send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
            except (EOFError, OSError):
                return  # manager gone
            last_status = time.time()
        elif time.time() - last_status >= status_interval:
            try:
                send_msg(pipe, _status(adapter, slave_id, "slave", connected=True))
            except (EOFError, OSError):
                return
            last_status = time.time()


def worker_main(pipe, role: str, adapter_kind: str = "real", fake_state=None):
    """Subprocess entry. Reads its StartMsg (config only — no credentials)
    from the pipe, then connects to the terminal's saved account."""
    try:
        start = recv_msg(pipe)
    except EOFError:
        return
    config = start.config
    if adapter_kind == "fake":
        adapter = FakeMt5(**(fake_state or {}))
    else:
        adapter = RealMt5()
    source_id = config.get("slave_id", role)
    ok = adapter.initialize(config["terminal_path"],
                            portable=bool(config.get("portable", False)))
    if not ok:
        try:
            send_msg(pipe, ErrorMsg(source_id=source_id,
                   message=f"initialize failed: {adapter.last_error()}", fatal=True))
        except (EOFError, OSError):
            pass
        return
    try:
        if role == "master":
            _master_loop(pipe, adapter, config)
        else:
            _slave_loop(pipe, adapter, config)
    except EOFError:
        pass  # manager closed the pipe -> graceful shutdown
    except Exception as exc:
        # Surface any other loop exception as a FATAL error so the supervisor
        # stops instead of silently restarting a crashing worker (which would
        # kill+reopen the terminal in a cycle). EOFError is graceful (no msg).
        try:
            send_msg(pipe, ErrorMsg(source_id=source_id,
                   message=f"worker crashed: {exc}\n{traceback.format_exc()}",
                   fatal=True))
        except (EOFError, OSError):
            pass
    finally:
        try:
            adapter.shutdown()
        except Exception:
            pass