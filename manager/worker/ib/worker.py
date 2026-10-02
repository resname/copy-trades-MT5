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
# Terminal states that mean "this order never produced (final) fills" — the
# real gateway's trade statuses plus IB's other cancelled/inactive states.
# The fake never marks a parent cancelled (its parents always fill) and leaves
# the field "", so the empty/unknown case anchors as before (F4).
_CANCELLED_ORDER_STATUSES = ("Cancelled", "ApiCancelled", "Inactive")


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
    in the same pass rather than patched onto a built record. A Cancelled
    parent MKT (rejected / margin-refused order) never opened anything: it
    must not resurrect a phantom record with full holdings (F4)."""
    out: list[Record] = []
    seen: set[int] = set()
    for o in gw.tagged_orders():
        dec = decode_comment(o.tag)
        if dec is None or o.order_type != "MKT":
            continue           # record anchors on its parent MKT order only
        if str(getattr(o, "status", "")) in _CANCELLED_ORDER_STATUSES:
            continue
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
    """Remaining contracts a record still holds: its own recorded share minus
    what the executions say was closed, clamped (F4) so it can never exceed
    the record's own share (a phantom/cancelled parent can never inflate it)
    nor over-draw the live net's side at the expense of the other records —
    the close history is day-scoped (`reqExecutions` after a Gateway restart,
    whose session rolls), so a record's reported closes can silently vanish,
    its derived holdings then inflate, and a CLOSE would sweep another
    record's share. The clamp is worker-side; `closed_per_tag`'s semantics
    stay untouched (the fake stays the oracle). The net bound is reached as
    a to-fixpoint re-clamp (each record bounded by the net minus the others'
    current bound), which is a no-op when the books are consistent
    (`sum(share - closed) == net`) and sheds only the lost-history deficit
    otherwise."""
    dec = decode_comment(tag)
    share = _contracts_for(float(dec[2] or 0.0) if dec else 0.0, ratio)
    closed = gw.closed_per_tag(tag)
    if dec is None:
        return 0                          # uncoded tag: nothing attributable
    orders = [o for o in gw.tagged_orders() if o.order_type == "MKT"]
    parent = next((o for o in orders if o.tag == tag), None)
    if parent is None:
        return max(0, math.floor(share - closed + 1e-9))  # no anchor to net against
    side = _tag_side(gw, tag, parent.symbol)
    peers: dict[str, int] = {}            # same symbol+side records: raw derived
    for o in orders:
        if o.tag != tag and o.symbol != parent.symbol:
            continue
        if o.tag != tag and (BUY if o.action == "BUY" else SELL) != side:
            continue
        odec = decode_comment(o.tag)
        if odec is None or o.tag in peers:
            continue
        oshare = _contracts_for(float(odec[2] or 0.0), ratio)
        ohold = max(0, math.floor(oshare - gw.closed_per_tag(o.tag) + 1e-9))
        peers[o.tag] = min(ohold, oshare)
    peers.setdefault(tag, min(max(0, math.floor(share - closed + 1e-9)), share))
    net = _current_qty(gw, parent.symbol, side)
    helds = dict(peers)
    changed = True
    while changed:                        # each: own <= net - the others'
        changed = False
        for t, h in list(helds.items()):
            rest = sum(v for t2, v in helds.items() if t2 != t)
            bound = max(0, math.floor(net - rest + 1e-9))
            if h > bound:
                helds[t] = bound
                changed = True
    return helds[tag]


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


def _tag_side(gw, tag: str, symbol: str) -> int | None:
    """A record's open side. Its tagged MKT orders say it (the open parent),
    but a reduce order under the same tag carries the OPPOSITE action and the
    open parent can be archived away (post-restart order archive), so prefer
    the side a live net position still holds — a record that still owns
    contracts sits on the net's side. Falls back to the raw first-MKT pick."""
    sides: list[int] = []
    for o in gw.tagged_orders():
        if o.tag == tag and o.order_type == "MKT" and o.symbol == symbol:
            s = BUY if o.action == "BUY" else SELL
            if s not in sides:
                sides.append(s)
    live = [p.side for p in gw.net_positions() if p.symbol == symbol]
    for s in sides:
        if s in live:
            return s
    if live:
        return live[0]
    return sides[0] if sides else None


def _unprotected(gw) -> set[str]:
    """F6 (recovery seam): symbols holding a live net position whose tagged
    record has ZERO active STP/LMT children — the worker (or the Gateway
    session) died between the parent fill and the child placement, so the
    position is live but unprotected. Flagged honestly (loud StatusMsg.detail
    suffix + ErrorMsg at init); recovery NEVER invents stop prices — the
    master's next MODIFY re-arms the bracket normally."""
    out: set[str] = set()
    positions = {(p.symbol, p.side) for p in gw.net_positions()}
    orders = gw.tagged_orders()
    for tag in sorted({o.tag for o in orders}):
        own = [o for o in orders if o.tag == tag]
        mkts = [o for o in own if o.order_type == "MKT"]
        if not mkts:
            continue
        parent = mkts[0]
        side = BUY if parent.action == "BUY" else SELL
        if (parent.symbol, side) not in positions:
            continue
        if not any(o.order_type in ("STP", "LMT") and o.active
                   and not o.filled for o in own):
            out.add(parent.symbol)
    return out


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
        # the record's side via the live net (a surviving reduce order under
        # the tag carries the open's OPPOSITE side; the parent can be archived)
        tside = _tag_side(gw, tag, symbol)
        if tside is None:
            continue
        hold = _holdings(gw, tag, ratio)
        sl, tp = _child_prices(gw, tag)
        if hold <= 0:
            sl = tp = 0.0                    # share fully closed: cancel only
        gw.set_protective(symbol, tside, tag, sl, tp, float(hold),
                          month=_target_month(gw, tag, symbol, tside),
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


def _bad_stop_reason(side: int, rsl: float, rtp: float,
                     bid: float, ask: float) -> str | None:
    """F1: after anchoring + tick rounding, an individual STP/LMT child must
    sit beyond the current market, or it fires as placed (a protective SELL
    STP/LMT pair works against the bid, a BUY pair against the ask). The
    anchor is NOT sufficient: copy_loop sends the master's CURRENT sl with
    the ORIGINAL master_open_price, so a master that trailed its stop to
    breakeven-or-better normalizes to an insta-firing child — on IB it is
    rejected outright or instantly filled, leaving the position without
    protection. Returns the violation's name, or None for valid levels."""
    if side == BUY:
        if 0.0 < rsl and rsl >= bid:
            return f"SL {rsl:g} not below the bid {bid:g}"
        if 0.0 < rtp and rtp <= bid:
            return f"TP {rtp:g} not above the bid {bid:g}"
    else:
        if 0.0 < rsl and rsl <= ask:
            return f"SL {rsl:g} not above the ask {ask:g}"
        if 0.0 < rtp and rtp >= ask:
            return f"TP {rtp:g} not below the ask {ask:g}"
    return None


def _stop_guard(cmd, rsl: float, rtp: float, bid: float, ask: float) -> str | None:
    """The same check, with the failed-ack error text that names the anchor."""
    viol = _bad_stop_reason(cmd.side, rsl, rtp, bid, ask)
    if viol is None:
        return None
    return (f"nonsensical stop rejected ({viol}; anchored from master sl "
            f"{cmd.sl:g} at master open {cmd.master_open_price:g}) — "
            f"children not placed")


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
        # before any placement: no bracket, no children, position untouched
        viol = _stop_guard(cmd, rsl, rtp, bid, ask)
        if viol is not None:
            return _fail(-1, viol)
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
        tick = gw.tick(symbol)
        if tick is None:
            return _fail(-1, gw.last_error() or "no tick to validate SL/TP for "
                                              f"{symbol}")
        sl, tp = _prep_sltp(cmd, open_price, normalize_sltp)
        rsltp = _round_sltp(sl, tp, res["tick_size"])
        if rsltp is None:
            return _fail(-1, "SL/TP normalization/tick rounding failed")
        rsl, rtp = rsltp
        viol = _stop_guard(cmd, rsl, rtp, tick[0], tick[1])
        if viol is not None:
            return _fail(-1, viol)          # children stay as they were
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
        # same clamped math as _holdings (F4): inflated close history must
        # not let a PARTIAL reduce past the record's own share of the net
        holdings = _holdings(gw, tag, ratio)
        share = _contracts_for(
            float(decode_comment(tag)[2] or 0.0), ratio)
        fraction = cmd.new_master_volume / cmd.master_open_volume
        target = math.floor(share * fraction + 1e-9)
        current = _current_qty(gw, symbol, side)
        by = min(holdings - target, current)
        by = max(0, int(math.floor(by + 1e-9)))
        if by <= 0:
            # F3: nothing to reduce (this share was already closed — e.g. a
            # timed-out reduce filled late), but the record's children may
            # still be at their pre-reduce size: sync them like any reduce.
            _sync_protection_after_reduce(gw, symbol, ratio, timeout_s)
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
            # race tolerance — already flat (the bracket fired, or a timed-out
            # reduce filled late): retire the record AND its stale, possibly
            # full-size children (F3: the orphaned stop would fire on the
            # flat net and open a naked reverse position)
            tag = _find_tag(gw, cmd.master_ticket)
            if tag is not None:
                symbol = next((o.symbol for o in gw.tagged_orders()
                               if o.tag == tag), None)
                spec = contracts.get(symbol) if symbol else None
                res = (_resolve(gw, spec, roll_days, today)
                       if spec is not None else None)
                if res is not None:
                    _sync_protection_after_reduce(
                        gw, symbol, _ratio(sizing_mode, spec, res), timeout_s)
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
            # F3: the record's share was already closed elsewhere (a timed-out
            # reduce filled late) — retire its possibly full-size children too.
            _sync_protection_after_reduce(gw, symbol, ratio, timeout_s)
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
    try:                            # read-only decoration: never fatal
        unprotected = _unprotected(gw)
    except Exception:
        unprotected = set()
    detail = "; ".join(
        f"{sym} {r['month']}{' rolling' if r['rolling'] else ''}"
        f"{' UNPROTECTED' if sym in unprotected else ''}"
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
    # F6: recovery re-seeds records, but a position whose children never came
    # (worker death between parent fill and child placement) must be surfaced
    # loudly — never healed with invented stop prices.
    missing_kids = _unprotected(gw)
    if missing_kids:
        send_msg(pipe, ErrorMsg(source_id=slave_id,
                                message="recovered position(s) without SL/TP "
                                        "protection (the master's next MODIFY "
                                        "re-arms the bracket): "
                                        + ", ".join(sorted(missing_kids))))
    status_interval = float(config.get("slave_status_interval_ms", 5000)) / 1000.0
    ib_host = str(config.get("ib_host", "127.0.0.1"))
    ib_port = int(config.get("ib_port", 4002))
    ib_client_id = int(config.get("ib_client_id", 7))
    reconnect_failures = 0
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
            if not _connected(gw):
                # F2: the Gateway owns the connection (the nightly IBC
                # restart drops it); a stale-but-alive worker otherwise never
                # reconnects. Each attempt is bounded by initialize's own
                # connect timeout; failures are counted and surfaced — at
                # most once per outage and once per recovery (never a fatal).
                if gw.initialize(ib_host, ib_port, ib_client_id):
                    if reconnect_failures:
                        try:
                            send_msg(pipe, ErrorMsg(
                                source_id=slave_id,
                                message=f"IB Gateway reconnected after "
                                        f"{reconnect_failures} failed "
                                        f"attempt(s)"))
                        except (EOFError, OSError):
                            return
                    reconnect_failures = 0
                else:
                    if reconnect_failures == 0:
                        try:
                            send_msg(pipe, ErrorMsg(
                                source_id=slave_id,
                                message="IB Gateway connection lost; "
                                        "reconnect attempts failing: "
                                        + gw.last_error()))
                        except (EOFError, OSError):
                            return
                    reconnect_failures += 1
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