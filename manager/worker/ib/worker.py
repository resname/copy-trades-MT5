# manager/worker/ib/worker.py
from __future__ import annotations

import math
import time
import traceback

from manager.engine.models import Record, BUY, SELL, SymbolInfo
from manager.engine.linkage import decode_comment, magic_for
from manager.engine.transform import (normalize_sltp as _normalize_sltp,
                                      round_to_tick, SIZING_BALANCE_STEP)
from manager.ipc.messages import (AckMsg, ErrorMsg, StatusMsg, SymbolInfoMsg,
                                   RecoveryMsg, ReconfigureMsg,
                                   SymbolInfoRequestMsg)
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.ib.adapter import FakeIbGateway, RealIbGateway
from manager.worker.ib.contracts import parse_contract_map
from manager.worker.ib.tags import synthetic_ticket

UNIT_VOLUME_STEP = 0.01
UNIT_VOLUME_MIN = 0.01
UNIT_VOLUME_MAX = 100_000.0
DEFAULT_ROLL_DAYS = 5
DEFAULT_ACK_TIMEOUT_MS = 15_000


def _ratio(sizing_mode: str, spec, resolved: dict) -> float:
    """Engine units -> contracts. balance_step units ARE contracts; the other
    two modes carry master CFD lots, converted by the point-value ratio."""
    if sizing_mode == SIZING_BALANCE_STEP:
        return 1.0
    mult = float(resolved.get("multiplier", 1.0)) or 1.0
    return spec.master_point_value / mult


def _contracts_for(units: float, ratio: float) -> int:
    # +1e-9: the repo's epsilon grid guard (0.28/0.01 bug class)
    return int(math.floor(units * ratio + 1e-9))


def _digits_of(tick: float) -> int:
    n, x = 0, round(float(tick), 8)
    while abs(x - round(x)) > 1e-9 and n < 8:
        x *= 10.0
        n += 1
    return n


def _resolve(gw, spec, roll_days: int, today: str = "") -> dict | None:
    return gw.resolve_contract(spec.symbol, today or time.strftime("%Y%m%d"),
                               exchange=spec.exchange, sec_type=spec.sec_type,
                               roll_days=roll_days)


def _symbol_info_for(gw, spec, today: str, roll_days: int) -> SymbolInfo | None:
    res = gw.resolve_contract(spec.symbol, today, exchange=spec.exchange,
                              sec_type=spec.sec_type, roll_days=roll_days)
    if res is None:
        return None
    return SymbolInfo(point=1.0, digits=_digits_of(res["tick_size"]),
                      tick_size=res["tick_size"],
                      volume_step=UNIT_VOLUME_STEP,
                      volume_min=UNIT_VOLUME_MIN,
                      volume_max=UNIT_VOLUME_MAX)


def build_symbol_info_msg(gw, slave_id: str, contracts: dict,
                          roll_days: int,
                          requested: list[str] | None = None) -> SymbolInfoMsg:
    """`requested=None`: the startup bulk report for every mapped symbol
    that resolves. Otherwise a SymbolInfoRequestMsg reply covering ONLY the
    requested symbols — the reply names every requested symbol; a symbol
    absent from `infos` is confirmed missing (mt5_worker's
    build_symbol_info_reply semantics)."""
    wanted = list(requested) if requested is not None else list(contracts)
    infos = {}
    for symbol in wanted:
        spec = contracts.get(symbol)
        if spec is not None:
            info = _symbol_info_for(gw, spec, time.strftime("%Y%m%d"),
                                    roll_days)
            if info is not None:
                infos[symbol] = info
    return SymbolInfoMsg(source_id=slave_id, infos=infos,
                         requested=list(requested) if requested else [])


def build_recovery_records(gw) -> list[Record]:
    """Rebuild linkage records from this slave's tagged orders (the tag IS the
    engine's CPY comment). One record per master ticket, anchored on its
    parent MKT order — `Record` is frozen, so the synthetic ticket is derived
    in the same pass rather than patched onto a built record."""
    out: list[Record] = []
    seen: set[int] = set()
    for o in gw.tagged_orders():
        dec = decode_comment(o.tag)
        if dec is None or o.order_type != "MKT":
            continue           # record anchors on its parent MKT order only
        master_ticket, mv, sv = dec
        if master_ticket in seen or mv is None or sv is None:
            continue
        seen.add(master_ticket)
        side = BUY if o.action == "BUY" else SELL
        out.append(Record(master_ticket=master_ticket,
                          magic=magic_for(master_ticket),
                          slave_ticket=synthetic_ticket(o.symbol, side),
                          master_open_volume=mv,
                          slave_open_volume=sv))
    return out


def _holdings(gw, tag: str, ratio: float) -> int:
    dec = decode_comment(tag)
    share = _contracts_for(float(dec[2] or 0.0) if dec else 0.0, ratio)
    closed = gw.closed_per_tag(tag)
    return max(0, math.floor(share - closed + 1e-9))


def _current_qty(gw, symbol: str, side: int) -> float:
    for p in gw.net_positions():
        if p.symbol == symbol and p.side == side:
            return p.qty
    return 0.0


def _find_symbol(gw, slave_ticket: int):
    for p in gw.net_positions():
        if synthetic_ticket(p.symbol, p.side) == slave_ticket:
            return p.symbol, p.side, p.open_price
    return None


def _find_tag(gw, master_ticket: int) -> str | None:
    for o in gw.tagged_orders():
        dec = decode_comment(o.tag)
        if dec and dec[0] == master_ticket:
            return o.tag
    return None


def _child_prices(gw, tag: str) -> tuple[float, float]:
    sl = tp = 0.0
    for o in gw.tagged_orders():
        if o.tag == tag and not o.filled:
            if o.order_type == "STP":
                sl = o.stop_price
            elif o.order_type == "LMT":
                tp = o.limit_price
    return sl, tp


def _record_month(gw, tag: str) -> str:
    """The contract month the record's parent order went to ("" = unknown,
    e.g. no surviving order object after a Gateway restart)."""
    for o in gw.tagged_orders():
        if o.tag == tag and o.order_type == "MKT" and o.month:
            return o.month
    return ""


def _position_month(gw, symbol: str, side: int) -> str:
    for p in gw.net_positions():
        if p.symbol == symbol and p.side == side:
            return p.month
    return ""


def _target_month(gw, tag: str, symbol: str, side: int) -> str:
    """Rollover rule (spec §5): an existing position keeps its contract.
    Prefer its own tagged parent order's month; fall back to the live net
    position's month; only a brand-new OPEN uses a freshly resolved month."""
    return _record_month(gw, tag) or _position_month(gw, symbol, side)


def _all_tags(gw, symbol: str) -> set[str]:
    return {o.tag for o in gw.tagged_orders() if o.symbol == symbol}


def _connected(gw) -> bool:
    """FakeIbGateway.connected vs RealIbGateway.ib.isConnected() under one
    read (both gateways are duck-typed; never hasattr on the worker side)."""
    ib = getattr(gw, "ib", None)
    if ib is not None:
        try:
            return bool(ib.isConnected())
        except Exception:
            return False
    return bool(getattr(gw, "connected", False))


def _sync_protection_after_reduce(gw, symbol: str, ratio: float,
                                  timeout_s: float) -> None:
    """Re-run after every reduce (CLOSE/PARTIAL): each record's SL/TP
    children must be sized to its own remaining share (netting model). A
    share that is fully retired gets its children cancelled — closing one
    record of several netted ones must leave no stale stop firing past the
    survivors (REVIEW FOCUS #1). set_protective(sl=0, tp=0, qty=0) is the
    cancel-only path in both gateway implementations."""
    for tag in sorted(_all_tags(gw, symbol)):
        dec = decode_comment(tag)
        if dec is None:
            continue
        orders = [o for o in gw.tagged_orders() if o.tag == tag]
        tside = next((BUY if o.action == "BUY" else SELL
                      for o in orders if o.order_type == "MKT"), None)
        if tside is None:
            continue
        hold = _holdings(gw, tag, ratio)
        sl, tp = _child_prices(gw, tag)
        if hold <= 0:
            sl = tp = 0.0                    # share fully closed: cancel only
        gw.set_protective(symbol, tside, tag, sl, tp, float(hold),
                          month=_record_month(gw, tag) or "",
                          timeout_s=timeout_s)


def _prep_sltp(cmd, slave_open: float, normalize: bool) -> tuple[float, float]:
    """SL/TP preparation (mirrors mt5_worker's order of operations —
    normalize against the slave fill first, then round to the tick)."""
    if normalize:
        return _normalize_sltp(cmd.master_open_price, cmd.sl, cmd.tp,
                               slave_open, cmd.side)
    return cmd.sl, cmd.tp


def _round_sltp(sl: float, tp: float,
                tick_size: float) -> tuple[float, float] | None:
    digits = _digits_of(tick_size)
    rsl = round_to_tick(sl, tick_size, digits)
    rtp = round_to_tick(tp, tick_size, digits)
    if rsl is None or rtp is None:
        return None
    return rsl, rtp


def execute_command(gw, cmd, contracts, normalize_sltp: bool,
                    sizing_mode: str, roll_days: int, timeout_ms: int,
                    today: str = "") -> AckMsg:
    """One CommandMsg -> one AckMsg. `today` is injectable for tests
    (rollover decisions are time-dependent); production passes "" (clock)."""
    def _fail(retcode: int, err: str) -> AckMsg:
        return AckMsg(slave_id=cmd.slave_id, action=cmd.action,
                      master_ticket=cmd.master_ticket, ok=False,
                      retcode=retcode, error=err)

    timeout_s = max(1.0, timeout_ms / 1000.0)

    if cmd.action == "OPEN":
        spec = contracts.get(cmd.symbol)
        if spec is None:
            return _fail(-1, f"no contract spec for {cmd.symbol}")
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error()
                         or f"cannot resolve contract for {cmd.symbol}")
        ratio = _ratio(sizing_mode, spec, res)
        qty = _contracts_for(cmd.volume, ratio)
        if qty <= 0:
            # logged skip: visible as a failed ack, retcode 0 (not an error
            # class), never silent — Global Constraints
            return _fail(0, f"skipped: {cmd.volume:g} units -> 0 contracts "
                            f"(ratio {ratio:.4g})")
        margin_est = qty * float(res.get("margin_est", 0.0))
        acc = gw.account()
        avail = float(acc.get("margin_available", 0.0))
        if margin_est > 0.0 and margin_est > avail * 0.9:
            return _fail(0, f"insufficient margin: est {margin_est:.0f} > "
                            f"available {avail:.0f}")
        tick = gw.tick(cmd.symbol)
        if tick is None:
            return _fail(-1, gw.last_error() or f"no tick for {cmd.symbol}")
        bid, ask = tick
        slave_open = ask if cmd.side == BUY else bid
        sl, tp = _prep_sltp(cmd, slave_open, normalize_sltp)
        rsltp = _round_sltp(sl, tp, res["tick_size"])
        if rsltp is None:
            return _fail(-1, "SL/TP normalization/tick rounding failed")
        rsl, rtp = rsltp
        ok, fill_px, err = gw.open_bracket(cmd.symbol, cmd.side, qty,
                                           rsl, rtp, cmd.comment,
                                           month=res["month"],
                                           timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or gw.last_error() or "bracket rejected")
        return AckMsg(slave_id=cmd.slave_id, action="OPEN",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=synthetic_ticket(cmd.symbol, cmd.side),
                      fill_price=fill_px, fill_volume=cmd.volume,
                      remaining_volume=cmd.volume, retcode=0)

    if cmd.action == "MODIFY":
        found = _find_symbol(gw, cmd.slave_ticket)
        if found is None:
            return _fail(-1, f"position for #{cmd.slave_ticket} not found")
        symbol, side, open_price = found
        spec = contracts.get(symbol)
        if spec is None:
            return _fail(-1, f"no contract spec for {symbol}")
        tag = _find_tag(gw, cmd.master_ticket)
        if tag is None:
            return _fail(-1, f"tag not found for master #{cmd.master_ticket}")
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error() or f"cannot resolve {symbol}")
        sl, tp = _prep_sltp(cmd, open_price, normalize_sltp)
        rsltp = _round_sltp(sl, tp, res["tick_size"])
        if rsltp is None:
            return _fail(-1, "SL/TP normalization/tick rounding failed")
        rsl, rtp = rsltp
        ratio = _ratio(sizing_mode, spec, res)
        hold = _holdings(gw, tag, ratio)
        ok, err = gw.set_protective(symbol, side, tag, rsl, rtp,
                                    float(hold),
                                    month=_target_month(gw, tag, symbol, side),
                                    timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or "children not replaced")
        return AckMsg(slave_id=cmd.slave_id, action="MODIFY",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket, retcode=0)

    if cmd.action == "PARTIAL_CLOSE":
        found = _find_symbol(gw, cmd.slave_ticket)
        if found is None:
            return _fail(-1, f"position for #{cmd.slave_ticket} not found")
        symbol, side, _open_price = found
        spec = contracts.get(symbol)
        tag = _find_tag(gw, cmd.master_ticket)
        if spec is None or tag is None:
            return _fail(-1, "contract spec or tag missing")
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error() or f"cannot resolve {symbol}")
        if cmd.master_open_volume <= 0.0:
            return _fail(-1, "cannot compute partial fraction")
        ratio = _ratio(sizing_mode, spec, res)
        share = _contracts_for(
            float(decode_comment(tag)[2] or 0.0), ratio)
        closed = gw.closed_per_tag(tag)
        holdings = max(0, math.floor(share - closed + 1e-9))
        fraction = cmd.new_master_volume / cmd.master_open_volume
        target = math.floor(share * fraction + 1e-9)
        current = _current_qty(gw, symbol, side)
        by = min(holdings - target, current)
        by = max(0, int(math.floor(by + 1e-9)))
        if by <= 0:
            return AckMsg(slave_id=cmd.slave_id, action="PARTIAL_CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket,
                          remaining_volume=float(holdings), retcode=0)
        month = _target_month(gw, tag, symbol, side)
        ok, filled, err = gw.reduce(symbol, side, by, tag, close=False,
                                    month=month, timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or "reduce failed")
        # REVIEW-FOCUS #2: resize children to the new remaining share so a
        # stale stop cannot fire past the holding (reverse-trade hazard)
        new_qty = max(0, holdings - filled)
        sl, tp = _child_prices(gw, tag)
        gw.set_protective(symbol, side, tag, sl, tp, float(new_qty),
                          month=month, timeout_s=timeout_s)
        return AckMsg(slave_id=cmd.slave_id, action="PARTIAL_CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket,
                      fill_volume=float(filled / (ratio if ratio else 1.0)),
                      remaining_volume=float(new_qty), retcode=0)

    if cmd.action == "CLOSE":
        found = _find_symbol(gw, cmd.slave_ticket)
        if found is None:
            # race tolerance — already flat (bracket fired): retire the record
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket, retcode=0)
        symbol, side, _open_price = found
        spec = contracts.get(symbol)
        tag = _find_tag(gw, cmd.master_ticket)
        if spec is None or tag is None:
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket, retcode=0)
        res = _resolve(gw, spec, roll_days, today)
        if res is None:
            return _fail(-1, gw.last_error() or f"cannot resolve {symbol}")
        ratio = _ratio(sizing_mode, spec, res)
        holdings = _holdings(gw, tag, ratio)
        current = _current_qty(gw, symbol, side)
        by = min(holdings, current)
        if by <= 0:
            return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                          master_ticket=cmd.master_ticket, ok=True,
                          slave_ticket=cmd.slave_ticket,
                          remaining_volume=0.0, retcode=0)
        month = _target_month(gw, tag, symbol, side)
        ok, filled, err = gw.reduce(symbol, side, by, tag, close=True,
                                    month=month, timeout_s=timeout_s)
        if not ok:
            return _fail(-1, err or "close failed")
        # Sibling protection sync: the net may still hold other records'
        # shares — retire this record's stale children (already cancelled by
        # the gateway when the net went flat) and re-size the survivors'
        # children to their own holdings.
        _sync_protection_after_reduce(gw, symbol, ratio, timeout_s)
        return AckMsg(slave_id=cmd.slave_id, action="CLOSE",
                      master_ticket=cmd.master_ticket, ok=True,
                      slave_ticket=cmd.slave_ticket,
                      remaining_volume=0.0, retcode=0)

    return _fail(-1, f"unknown action {cmd.action!r}")


# ---- the loop half: status + init + message loop (mirrors
# mt5_worker._slave_loop plus the IB bits) ----

def _status(gw, source_id: str, connected: bool, front: dict) -> StatusMsg:
    acc = gw.account()
    detail = "; ".join(
        f"{sym} {r['month']}{' rolling' if r['rolling'] else ''}"
        for sym, r in sorted(front.items()))
    return StatusMsg(source_id=source_id, role="slave", connected=connected,
                     login=0, balance=float(acc.get("balance", 0.0)),
                     equity=float(acc.get("equity", 0.0)),
                     currency=str(acc.get("currency", "USD")),
                     server="ib-gateway",
                     trade_allowed=connected and not gw.read_only(),
                     detail=detail)


def slave_init(gw, config: dict, contracts: dict, roll_days: int):
    slave_id = config["slave_id"]
    front = {s: r for s, spec in contracts.items()
             if (r := gw.resolve_contract(
                 spec.symbol, time.strftime("%Y%m%d"),
                 exchange=spec.exchange, sec_type=spec.sec_type,
                 roll_days=roll_days)) is not None}
    rec_msg = RecoveryMsg(source_id=slave_id,
                          records=tuple(build_recovery_records(gw)))
    si_msg = build_symbol_info_msg(gw, slave_id, contracts, roll_days)
    st_msg = _status(gw, slave_id, connected=True, front=front)
    return rec_msg, si_msg, st_msg, front


def _slave_loop(pipe, gw, config):
    slave_id = config["slave_id"]
    roll_days = int(config.get("roll_days", DEFAULT_ROLL_DAYS))
    timeout_ms = int(config.get("ack_timeout_ms", DEFAULT_ACK_TIMEOUT_MS))
    contracts = parse_contract_map(config.get("contract_map"))
    normalize = bool(config.get("normalize_sltp", True))
    sizing_mode = str(config.get("sizing_mode", "balance_step"))
    rec_msg, si_msg, st_msg, front = slave_init(gw, config, contracts,
                                                roll_days)
    send_msg(pipe, rec_msg); send_msg(pipe, si_msg); send_msg(pipe, st_msg)
    status_interval = float(config.get("slave_status_interval_ms", 5000)) / 1000.0
    last_status = time.time()
    poll_timeout = min(1.0, status_interval)
    while True:
        if pipe.poll(poll_timeout):
            msg = recv_msg(pipe)  # raises EOFError on manager close
            if isinstance(msg, ReconfigureMsg):
                normalize = msg.normalize_sltp
                if msg.contracts:
                    contracts = parse_contract_map(msg.contracts)
                    front = {s: r for s, spec in contracts.items()
                             if (r := gw.resolve_contract(
                                 spec.symbol, time.strftime("%Y%m%d"),
                                 exchange=spec.exchange, sec_type=spec.sec_type,
                                 roll_days=roll_days)) is not None}
                send_msg(pipe, build_symbol_info_msg(gw, slave_id, contracts,
                                                     roll_days))
                last_status = time.time()
                continue
            if isinstance(msg, SymbolInfoRequestMsg):
                send_msg(pipe, build_symbol_info_msg(gw, slave_id, contracts,
                                                     roll_days,
                                                     requested=list(msg.symbols)))
                last_status = time.time()
                continue
            ack = execute_command(gw, msg, contracts, normalize, sizing_mode,
                                  roll_days, timeout_ms)
            try:
                send_msg(pipe, ack)
                send_msg(pipe, _status(gw, slave_id, connected=_connected(gw),
                                       front=front))
            except (EOFError, OSError):
                return  # manager gone
            last_status = time.time()
        elif time.time() - last_status >= status_interval:
            try:
                send_msg(pipe, _status(gw, slave_id, connected=_connected(gw),
                                       front=front))
            except (EOFError, OSError):
                return
            last_status = time.time()


def worker_main(pipe, role: str, adapter_kind: str = "real", fake_state=None):
    """Subprocess entry (same signature as mt5_worker.worker_main). Reads the
    StartMsg config, connects to the IB Gateway socket, runs the slave loop."""
    try:
        start = recv_msg(pipe)
    except EOFError:
        return
    config = start.config
    if role == "master":
        try:
            send_msg(pipe, ErrorMsg(source_id="master",
                   message="IB accounts cannot be the master", fatal=True))
        except (EOFError, OSError):
            pass
        return
    if adapter_kind == "fake":
        gw = FakeIbGateway(**(fake_state or {}))
    else:
        gw = RealIbGateway()
    ok = gw.initialize(config.get("ib_host", "127.0.0.1"),
                       int(config.get("ib_port", 4002)),
                       int(config.get("ib_client_id", 7)))
    if not ok:
        try:
            send_msg(pipe, ErrorMsg(source_id=config["slave_id"],
                   message=f"IB Gateway connect failed: {gw.last_error()}",
                   fatal=True))
        except (EOFError, OSError):
            pass
        return
    try:
        _slave_loop(pipe, gw, config)
    except ValueError as exc:      # bad contract map: fatal, user-fixable
        try:
            send_msg(pipe, ErrorMsg(source_id=config["slave_id"],
                   message=f"bad contract map: {exc}", fatal=True))
        except (EOFError, OSError):
            pass
    except EOFError:
        pass
    except Exception as exc:
        try:
            send_msg(pipe, ErrorMsg(source_id=config["slave_id"],
                   message=f"worker crashed: {exc}\n{traceback.format_exc()}",
                   fatal=True))
        except (EOFError, OSError):
            pass
    finally:
        try:
            gw.shutdown()
        except Exception:
            pass