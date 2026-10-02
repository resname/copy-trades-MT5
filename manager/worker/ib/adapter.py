# manager/worker/ib/adapter.py
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Protocol

from manager.engine.models import BUY, SELL
from manager.worker.ib.contracts import pick_front_month


@dataclass
class IbOrder:
    order_id: int
    tag: str            # the CPY#... linkage comment from cmd.comment
    symbol: str
    action: str         # "BUY" | "SELL" (IB order convention)
    order_type: str     # "MKT" | "LMT" | "STP"
    qty: float
    month: str = ""     # resolved contract month ("" = non-FUT)
    limit_price: float = 0.0
    stop_price: float = 0.0
    parent_id: int = 0
    oca_group: str = ""
    active: bool = True     # children sit inactive until the parent fills
    filled: bool = False
    status: str = ""        # raw IB trade status ("" = fake: always filled)


@dataclass
class IbExecution:
    order_id: int
    tag: str
    action: str
    qty: float


@dataclass
class IbPosition:
    symbol: str
    side: int           # 0 = BUY(long), 1 = SELL(short) — engine convention
    qty: float          # absolute, in contracts
    open_price: float
    month: str = ""     # the contract this net position lives on


class IbGateway(Protocol):
    def initialize(self, host: str, port: int, client_id: int) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> str: ...
    def read_only(self) -> bool: ...
    def resolve_contract(self, symbol: str, today: str, exchange: str = "",
                         sec_type: str = "FUT", roll_days: int = 5
                         ) -> dict | None:
        # -> {"month": str, "rolling": bool, "multiplier": float,
        #     "tick_size": float, "margin_est": float}
        ...
    def tick(self, symbol: str) -> tuple[float, float] | None: ...
    def account(self) -> dict: ...
    def net_positions(self) -> list[IbPosition]: ...
    def tagged_orders(self) -> list[IbOrder]: ...
    def closed_per_tag(self, tag: str) -> float: ...
    def open_bracket(self, symbol: str, side: int, qty: float,
                     sl: float, tp: float, tag: str,
                     month: str = "", timeout_s: float = 15.0
                     ) -> tuple[bool, float, str]: ...
    def set_protective(self, symbol: str, side: int, tag: str,
                       sl: float, tp: float, qty: float,
                       month: str = "", timeout_s: float = 15.0
                       ) -> tuple[bool, str]: ...
    def reduce(self, symbol: str, side: int, qty: float, tag: str,
               close: bool, month: str = "", timeout_s: float = 15.0
               ) -> tuple[bool, float, str]: ...


OCA_PREFIX = "ct-oca-"
MARGIN_FRACTION = 0.9    # used by the worker (Task 7); one place to tune


class FakeIbGateway:
    """In-memory IB with deterministic fills — the unit-test oracle for the
    IB worker and the supervisor's fake adapter for IB slaves."""

    def __init__(self, account=None, prices=None, contract_multipliers=None,
                 tick_sizes=None, months=None, open_interest=None,
                 margin_per_contract=None, server_read_only=False,
                 fail_reduce_timeout=False):
        self._account = {"balance": 10000.0, "equity": 10000.0,
                         "currency": "USD", "margin_available": 1_000_000.0,
                         "account_id": "DU123"}
        self._account.update(account or {})
        self.prices = {**({"YM": (45_000.0, 45_001.0), "ES": (6_000.0, 6_001.0)}
                          if prices is None else prices)}
        self.multipliers = {**({"YM": 5.0, "ES": 50.0}
                               if contract_multipliers is None
                               else contract_multipliers)}
        self.price_ticks = {**({"YM": 0.25, "ES": 0.25}
                               if tick_sizes is None else tick_sizes)}
        self.months = {**({"YM": ["202612"], "ES": ["202612", "202703"]}
                          if months is None else months)}
        self.open_interest = {} if open_interest is None else open_interest
        self.margin_per_contract = ({} if margin_per_contract is None
                                    else margin_per_contract)
        self.connected = False
        self._read_only = server_read_only
        self._fail_reduce_timeout = fail_reduce_timeout
        self._next_oid = 1000
        self.orders: list[IbOrder] = []
        self.positions: dict[str, float] = {}       # symbol -> signed net
        self.avg_price: dict[str, float] = {}       # symbol -> avg open price
        self.pos_month: dict[str, str] = {}         # symbol -> open's month
        self.executions: list[IbExecution] = []

    # ---- lifecycle ----
    def initialize(self, host: str, port: int, client_id: int) -> bool:
        self.connected = True
        return True

    def shutdown(self) -> None:
        self.connected = False

    def last_error(self) -> str:
        return ""

    def read_only(self) -> bool:
        return self._read_only

    def _alloc(self) -> int:
        self._next_oid += 1
        return self._next_oid

    @staticmethod
    def _action(side: int) -> str:
        return "BUY" if side == BUY else "SELL"

    # ---- contracts / market data ----
    def resolve_contract(self, symbol: str, today: str, exchange: str = "",
                         sec_type: str = "FUT", roll_days: int = 5) -> dict | None:
        rows = self.months.get(symbol)
        if rows is None:
            return None
        oi = self.open_interest.get(symbol, {})
        picked = pick_front_month([(m, oi.get(m)) for m in rows], today,
                                  roll_days=roll_days)
        if picked is None:
            return None
        month, rolling = picked
        return {"month": month, "rolling": rolling,
                "multiplier": self.multipliers.get(symbol, 1.0),
                "tick_size": self.price_ticks.get(symbol, 0.25),
                "margin_est": self.margin_per_contract.get(symbol, 0.0)}

    def tick(self, symbol: str) -> tuple[float, float] | None:
        return self.prices.get(symbol)

    def account(self) -> dict:
        return dict(self._account)

    # ---- reads ----
    def net_positions(self) -> list[IbPosition]:
        out = []
        for symbol, signed in sorted(self.positions.items()):
            if signed == 0.0:
                continue
            out.append(IbPosition(symbol=symbol,
                                  side=BUY if signed > 0 else SELL,
                                  qty=abs(signed),
                                  open_price=self.avg_price.get(symbol, 0.0),
                                  month=self.pos_month.get(symbol, "")))
        return out

    def tagged_orders(self) -> list[IbOrder]:
        return [o for o in self.orders if o.tag.startswith("CPY#")]

    def closed_per_tag(self, tag: str) -> float:
        return sum(e.qty for e in self.executions if e.tag == tag)

    # ---- fills ----
    def _apply_fill(self, symbol: str, action: str, qty: float,
                    price: float) -> None:
        signed = qty if action == "BUY" else -qty
        current = self.positions.get(symbol, 0.0)
        new = current + signed
        flipped = (current != 0.0 and new != 0.0
                   and (current > 0) != (new > 0))   # true net flip only
        if flipped:
            self.avg_price.pop(symbol, None)
        self.positions[symbol] = new
        if self.positions[symbol] == 0.0:
            self.positions.pop(symbol, None)
            self.avg_price.pop(symbol, None)
        elif signed * self.positions[symbol] > 0:   # increased exposure
            prev = abs(self.positions[symbol]) - qty
            base = self.avg_price.get(symbol, price)
            self.avg_price[symbol] = (base * prev + price * qty) \
                / max(prev + qty, 1e-9)
        self._trigger_checks()

    def _protect(self, parent_id: int, tag: str, symbol: str, action: str,
                 order_type: str, qty: float, *, stop_price: float = 0.0,
                 limit_price: float = 0.0, oca: str = "") -> IbOrder:
        child = IbOrder(order_id=self._alloc(), tag=tag, symbol=symbol,
                        action=action, order_type=order_type, qty=qty,
                        limit_price=limit_price, stop_price=stop_price,
                        parent_id=parent_id, oca_group=oca)
        self.orders.append(child)
        return child

    def open_bracket(self, symbol: str, side: int, qty: float,
                     sl: float, tp: float, tag: str, month: str = "",
                     timeout_s: float = 15.0) -> tuple[bool, float, str]:
        if self._read_only:
            return (False, 0.0, "IB Gateway Read-Only API is on")
        act = self._action(side)
        bid, ask = self.prices.get(symbol, (0.0, 0.0))
        px = ask if act == "BUY" else bid
        oid = self._alloc()
        self.orders.append(IbOrder(order_id=oid, tag=tag, symbol=symbol,
                                   action=act, order_type="MKT", qty=qty,
                                   month=month, filled=True))
        if month:
            self.pos_month[symbol] = month      # this net positions' contract
        oca = OCA_PREFIX + str(oid)
        prot = self._action(1 - side)   # protective side opposes the entry
        if sl > 0.0:
            self._protect(oid, tag, symbol, prot, "STP", qty,
                          stop_price=sl, oca=oca)
        if tp > 0.0:
            self._protect(oid, tag, symbol, prot, "LMT", qty,
                          limit_price=tp, oca=oca)
        self._apply_fill(symbol, act, qty, px)
        return (True, px, "")

    def set_price(self, symbol: str, bid: float, ask: float) -> None:
        self.prices[symbol] = (bid, ask)
        self._trigger_checks()

    def _trigger_checks(self) -> None:
        # Unfilled STP/LMT children fire for their full stated qty even after
        # a manual partial reduce (raw IB order semantics); the worker
        # re-syncs protections after partials instead of net-clamping here.
        for o in list(self.orders):
            if o.filled or not o.active or o.order_type not in ("STP", "LMT"):
                continue
            bid, ask = self.prices.get(o.symbol, (0.0, 0.0))
            if o.order_type == "STP":
                trig = (ask >= o.stop_price if o.action == "BUY"
                        else (bid <= o.stop_price))
                trig = trig and o.stop_price > 0
            else:
                trig = (bid >= o.limit_price if o.action == "SELL"
                        else (ask <= o.limit_price))
                trig = trig and o.limit_price > 0
            if trig:
                self._fire_child(o)

    def _fire_child(self, o: IbOrder) -> None:
        o.filled = True
        o.active = False
        bid, ask = self.prices.get(o.symbol, (0.0, 0.0))
        px = bid if o.action == "SELL" else ask
        self.executions.append(IbExecution(order_id=o.order_id, tag=o.tag,
                                           action=o.action, qty=o.qty))
        self._apply_fill(o.symbol, o.action, o.qty, px)
        self._oca_cancel(o.oca_group, o.order_id)

    def _oca_cancel(self, oca_group: str, except_id: int) -> None:
        if not oca_group:
            return
        for sib in self.orders:
            if (sib.oca_group == oca_group and sib.order_id != except_id
                    and not sib.filled):
                sib.filled = True     # cancelled by OCA
                sib.active = False

    def set_protective(self, symbol: str, side: int, tag: str,
                       sl: float, tp: float, qty: float, month: str = "",
                       timeout_s: float = 15.0) -> tuple[bool, str]:
        if self._read_only:
            return (False, "IB Gateway Read-Only API is on")
        prot = self._action(1 - side)
        for o in self.orders:                       # cancel current children
            if (o.tag == tag and o.symbol == symbol
                    and o.order_type in ("STP", "LMT") and not o.filled):
                o.filled = True
                o.active = False
        if sl <= 0.0 and tp <= 0.0:
            return (True, "")                       # children removed, none to add
        oca = OCA_PREFIX + str(self._alloc())
        if sl > 0.0:
            self._protect(0, tag, symbol, prot, "STP", qty,
                          stop_price=sl, oca=oca)
        if tp > 0.0:
            self._protect(0, tag, symbol, prot, "LMT", qty,
                          limit_price=tp, oca=oca)
        self._trigger_checks()
        return (True, "")

    def reduce(self, symbol: str, side: int, qty: float, tag: str,
               close: bool, month: str = "", timeout_s: float = 15.0
               ) -> tuple[bool, float, str]:
        if self._fail_reduce_timeout:
            return (False, 0.0, "timeout waiting for fill")
        if self._read_only:
            return (False, 0.0, "IB Gateway Read-Only API is on")
        signed = self.positions.get(symbol, 0.0)
        if side == BUY:
            filled = min(qty, signed if signed > 0 else 0.0)
            action = "SELL"
            px = self.prices.get(symbol, (0.0, 0.0))[0]
        else:
            filled = min(qty, -signed if signed < 0 else 0.0)
            action = "BUY"
            px = self.prices.get(symbol, (0.0, 0.0))[1]
        if filled <= 0.0:
            return (True, 0.0, "")                  # tolerated race no-op
        oid = self._alloc()
        self.orders.append(IbOrder(order_id=oid, tag=tag, symbol=symbol,
                                   action=action, order_type="MKT",
                                   qty=filled, filled=True))
        self.executions.append(IbExecution(order_id=oid, tag=tag,
                                           action=action, qty=filled))
        self._apply_fill(symbol, action, filled, px)
        if close and self.positions.get(symbol, 0.0) == 0.0:
            for o in self.orders:                   # kill this tag's children
                if (o.tag == tag and o.symbol == symbol
                        and o.order_type in ("STP", "LMT")
                        and not o.filled):
                    o.filled = True
                    o.active = False
                    self._oca_cancel(o.oca_group, o.order_id)
        return (True, filled, "")


class RealIbGateway:
    """IbGateway over ib_async (TWS API). Synchronous style: each method
    submits and waits (bounded by the worker's ack_timeout contract). All
    ib_async imports live here — MT5-only installs never import this module."""

    def __init__(self):
        from ib_async import IB     # ImportError = clear error, surfaced below
        self._IB = IB
        self.ib = None
        self._account_id = ""
        self._last_error = ""
        self._resolved: dict[str, dict] = {}     # symbol -> resolve result
        self._contracts: dict[str, object] = {}  # symbol -> current Contract
        self._contracts_by_month: dict[tuple[str, str], object] = {}

    # ---- lifecycle ----
    def initialize(self, host: str, port: int, client_id: int) -> bool:
        if self.ib is not None:
            # reconnect (F2): the dropped session's client must be torn down
            # before a fresh connect, or the dead socket lingers under self.ib
            try:
                self.ib.disconnect()
            except Exception:
                pass
            self.ib = None
        try:
            ib = self._IB()
            ib.connect(host, port, clientId=client_id, timeout=15.0)
        except Exception as exc:
            self._last_error = f"IB connect failed ({host}:{port}): {exc}"
            return False
        if not ib.isConnected():
            self._last_error = f"IB connect failed ({host}:{port})"
            return False
        self.ib = ib
        try:
            ib.reqPositions()
            self._account_id = (ib.managedAccounts or "").split(",")[0]
            if self._account_id:
                ib.reqAccountUpdates(True, self._account_id)
        except Exception as exc:
            self._last_error = f"IB setup failed: {exc}"
            return False
        return True

    def shutdown(self) -> None:
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:
                pass
            self.ib = None

    def last_error(self) -> str:
        return self._last_error

    def read_only(self) -> bool:
        # TWS/Gateway exposes no read-only introspection over the socket; the
        # Start gate is connect-only and a read-only Gateway surfaces on the
        # first rejected order (see spec + Review Focus #6).
        return False

    # ---- contracts ----
    def resolve_contract(self, symbol: str, today: str, exchange: str = "",
                         sec_type: str = "FUT", roll_days: int = 5) -> dict | None:
        from ib_async import Contract
        if self.ib is None:
            return None
        try:
            if sec_type == "FUT":
                probe = Contract(symbol=symbol, secType="FUT",
                                 exchange=exchange or "CME")
                details = sorted(self.ib.reqContractDetails(probe),
                                 key=lambda d: d.lastTradeDateOrContractMonth)
                months = [(d.lastTradeDateOrContractMonth, None)
                          for d in details]
                picked = pick_front_month(months, today, roll_days=roll_days)
                if picked is None:
                    self._last_error = f"no tradable future month for {symbol}"
                    return None
                month, rolling = picked
                contract = Contract(symbol=symbol, secType="FUT",
                                    exchange=exchange or "CME",
                                    lastTradeDateOrContractMonth=month)
                d0 = next((d for d in details
                           if d.lastTradeDateOrContractMonth == month),
                          details[0])
            else:
                probe = Contract(symbol=symbol, secType=sec_type,
                                 exchange=exchange or "")
                details = self.ib.reqContractDetails(probe)
                if not details:
                    self._last_error = f"no contract details for {symbol}"
                    return None
                month, rolling, d0 = "", False, details[0]
                contract = d0.contract
        except Exception as exc:
            self._last_error = f"contract resolution failed for {symbol}: {exc}"
            return None
        try:
            mult = float(d0.multiplier) if d0.multiplier else 1.0
            tick = float(d0.minTick) if d0.minTick else 0.0
        except (TypeError, ValueError):
            mult, tick = 1.0, 0.0
        margin_est = self._margin_estimate(contract)
        res = {"month": month, "rolling": rolling, "multiplier": mult,
               "tick_size": tick, "margin_est": margin_est,
               "sec_type": sec_type, "exchange": exchange or ""}
        self._resolved[symbol] = res
        self._contracts[symbol] = contract
        if month:
            self._contracts_by_month[(symbol, month)] = contract
        return res

    def _margin_estimate(self, contract) -> float:
        """whatIf probe: real maintenance margin per 1 contract, via
        IB.whatIfOrder -> OrderState (blocking; OrderState's margin fields
        are strings). 0.0 = unknown (the worker then skips the margin guard
        rather than refusing)."""
        from ib_async import MarketOrder
        try:
            probe = MarketOrder("BUY", 1)
            probe.whatIf = True
            state = self.ib.whatIfOrder(contract, probe)   # OrderState
            return (float(state.initMarginChange or 0.0)
                    or float(state.maintMarginChange or 0.0)
                    or float(state.initMarginAfter or 0.0)
                    or float(state.maintMarginAfter or 0.0))
        except Exception:
            return 0.0

    # ---- market data / account ----
    def tick(self, symbol: str) -> tuple[float, float] | None:
        if self.ib is None:
            return None
        contract = self._contracts.get(symbol)  # the resolved contract
        if contract is None:
            return None
        try:
            t = self.ib.reqTickers(contract)[0]
        except Exception as exc:
            self._last_error = f"market data for {symbol}: {exc}"
            return None
        bid, ask = float(t.bid or 0.0), float(t.ask or 0.0)
        if (not math.isfinite(bid) or not math.isfinite(ask)
                or bid <= 0.0 or ask <= 0.0):
            self._last_error = (f"no live market data for {symbol} "
                                f"(IB data subscription required)")
            return None
        return bid, ask

    def account(self) -> dict:
        def val(tag: str) -> float:
            return float({a.tag: a.value
                          for a in self.ib.accountValues(self._account_id)}
                         .get(tag, 0.0))
        if self.ib is None:
            return {}
        vals = {a.tag for a in self.ib.accountValues(self._account_id)}
        return {"balance": val("TotalCashValue"), "equity": val("NetLiquidation"),
                "margin_available": val("AvailableFunds"),
                "currency": "USD"}

    # ---- reads ----
    def net_positions(self) -> list[IbPosition]:
        out: list[IbPosition] = []
        if self.ib is None:
            return out
        for p in self.ib.positions():
            if not p.position:
                continue
            symbol = p.contract.symbol
            mult = float(p.contract.multiplier or 1) \
                if getattr(p.contract, "multiplier", None) else 1.0
            open_price = (abs(float(p.avgCost)) / mult) if mult else 0.0
            out.append(IbPosition(symbol=symbol,
                                  side=BUY if p.position > 0 else SELL,
                                  qty=abs(float(p.position)),
                                  open_price=open_price,
                                  month=p.contract.lastTradeDateOrContractMonth
                                  or ""))
        return out

    def tagged_orders(self) -> list[IbOrder]:
        out: list[IbOrder] = []
        if self.ib is None:
            return out
        type_map = {"MKT": "MKT", "LMT": "LMT", "STP": "STP"}
        for tr in self.ib.trades():
            ref = tr.order.orderRef or ""
            if not ref.startswith("CPY#"):
                continue
            out.append(IbOrder(
                order_id=int(tr.order.orderId), tag=ref,
                symbol=tr.contract.symbol,
                action=tr.order.action,
                order_type=type_map.get(tr.order.orderType, tr.order.orderType),
                qty=float(tr.order.totalQuantity),
                month=tr.contract.lastTradeDateOrContractMonth or "",
                limit_price=float(tr.order.lmtPrice or 0.0),
                stop_price=float(tr.order.auxPrice or 0.0),
                parent_id=int(tr.order.parentId or 0),
                oca_group=tr.order.ocaGroup or "",
                active=tr.orderStatus.status in
                       ("PreSubmitted", "PendingSubmit", "Submitted", "ApiPending"),
                filled=tr.orderStatus.status == "Filled",
                status=str(tr.orderStatus.status or "")))
        return out

    def closed_per_tag(self, tag: str) -> float:
        """Quantity closed against a tagged record: every fill under this
        tag whose side OPPOSES the record's opening fill (fired children,
        partial reduce orders, manual closes). The opening fill itself has
        the same side as its own order, so a rule keyed on the fill's own
        order action would misclassify reduce fills — key on the opening
        side instead."""
        if self.ib is None:
            return 0.0
        fills = [f for f in self.ib.fills()
                 if (f.execution.orderRef or "") == tag]
        if not fills:
            return 0.0
        open_side = fills[0].execution.side     # first fill chronologically = the open
        return sum(abs(float(f.execution.shares))
                   for f in fills if f.execution.side != open_side)

    # ---- commands (each bounded by its internal wait loop) ----
    def open_bracket(self, symbol: str, side: int, qty: float,
                     sl: float, tp: float, tag: str, month: str = "",
                     timeout_s: float = 15.0) -> tuple[bool, float, str]:
        from ib_async import MarketOrder, StopOrder, LimitOrder
        if self.ib is None:
            return (False, 0.0, self._last_error or "not connected")
        contract = self._contract_for(symbol, month)
        if contract is None:
            return (False, 0.0, f"contract not resolved for {symbol}")
        action = "BUY" if side == BUY else "SELL"
        parent = MarketOrder(action, qty)
        parent.orderRef = tag
        try:
            trade = self.ib.placeOrder(contract, parent)
        except Exception as exc:
            return (False, 0.0, f"order rejected: {exc}")
        if not self._wait_terminal(trade, timeout_s):
            return (False, 0.0, f"timeout waiting for {action} fill ({tag})")
        if trade.orderStatus.status == "Cancelled":
            reason = trade.log[-1].message if trade.log else ""
            return (False, 0.0, f"order cancelled: {reason}")
        fill_px = float(trade.orderStatus.avgFillPrice or 0.0)
        oca = OCA_PREFIX + str(parent.orderId)
        prot = "SELL" if action == "BUY" else "BUY"
        if sl > 0.0:
            child = StopOrder(prot, qty, sl)
            child.orderRef, child.parentId, child.ocaGroup = tag, parent.orderId, oca
            self.ib.placeOrder(contract, child)
        if tp > 0.0:
            child = LimitOrder(prot, qty, tp)
            child.orderRef, child.parentId, child.ocaGroup = tag, parent.orderId, oca
            self.ib.placeOrder(contract, child)
        return (True, fill_px, "")

    def set_protective(self, symbol: str, side: int, tag: str,
                       sl: float, tp: float, qty: float, month: str = "",
                       timeout_s: float = 15.0) -> tuple[bool, str]:
        from ib_async import StopOrder, LimitOrder
        if self.ib is None:
            return (False, self._last_error or "not connected")
        contract = self._contract_for(symbol, month)
        if contract is None:
            return (False, f"contract not resolved for {symbol}")
        prot = "SELL" if side == BUY else "BUY"
        oca = OCA_PREFIX + str(int(time.time() * 1000) % 1_000_000_000)
        for tr in list(self.ib.trades()):
            ref = tr.order.orderRef or ""
            if (ref == tag and tr.contract.symbol == contract.symbol
                    and (not month
                         or tr.contract.lastTradeDateOrContractMonth == month)
                    and tr.order.orderType in ("STP", "LMT")
                    and tr.orderStatus.status not in ("Filled", "Cancelled")):
                self.ib.cancelOrder(tr.order)
        if sl > 0.0:
            child = StopOrder(prot, qty, sl)
            child.orderRef, child.ocaGroup = tag, oca
            self.ib.placeOrder(contract, child)
        if tp > 0.0:
            child = LimitOrder(prot, qty, tp)
            child.orderRef, child.ocaGroup = tag, oca
            self.ib.placeOrder(contract, child)
        return (True, "")

    def reduce(self, symbol: str, side: int, qty: float, tag: str,
               close: bool, month: str = "", timeout_s: float = 15.0
               ) -> tuple[bool, float, str]:
        from ib_async import MarketOrder
        if self.ib is None:
            return (False, 0.0, self._last_error or "not connected")
        contract = self._contract_for(symbol, month)
        if contract is None:
            return (False, 0.0, f"contract not resolved for {symbol}")
        action = "SELL" if side == BUY else "BUY"
        order = MarketOrder(action, qty)
        order.orderRef = tag
        try:
            trade = self.ib.placeOrder(contract, order)
        except Exception as exc:
            return (False, 0.0, f"order rejected: {exc}")
        if not self._wait_terminal(trade, timeout_s):
            return (False, 0.0, "timeout waiting for fill")
        if trade.orderStatus.status == "Cancelled":
            reason = trade.log[-1].message if trade.log else ""
            return (False, 0.0, f"order cancelled: {reason}")
        filled = min(qty, float(trade.orderStatus.filled or 0.0))
        if close:
            flat = self._net_qty(symbol) == 0.0
            if flat:
                for tr in list(self.ib.trades()):
                    if ((tr.order.orderRef or "") == tag
                            and tr.order.orderType in ("STP", "LMT")
                            and tr.orderStatus.status not in ("Filled", "Cancelled")):
                        self.ib.cancelOrder(tr.order)
        return (True, filled, "")

    # ---- helpers ----
    def _contract_for(self, symbol: str, month: str = ""):
        """month='': the currently resolved contract. A recorded month builds
        the exact contract an existing position lives on (rollover keeps
        existing positions on their month — Review Focus #5)."""
        if self.ib is None:
            return None
        if not month:
            return self._contracts.get(symbol)
        spec = self._resolved.get(symbol, {})
        if spec.get("sec_type", "FUT") != "FUT":
            return self._contracts.get(symbol)  # non-FUT: one contract, no months
        cached = self._contracts_by_month.get((symbol, month))
        if cached is not None:
            return cached
        from ib_async import Contract
        c = Contract(symbol=symbol, secType="FUT",
                     exchange=spec.get("exchange", ""),
                     lastTradeDateOrContractMonth=month)
        self._contracts_by_month[(symbol, month)] = c
        return c

    def _net_qty(self, symbol: str) -> float:
        total = 0.0
        for p in self.ib.positions():
            if p.contract.symbol == symbol:
                total += float(p.position)
        return total

    def _wait_terminal(self, trade, timeout_s: float) -> bool:
        """Poll the live Trade until Filled/Cancelled, driving ib_async's own
        event loop with IB.sleep (staticmethod of util.sleep) — NEVER
        time.sleep (Trade is updated by the loop between sleeps)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if trade.orderStatus.status in ("Filled", "Cancelled"):
                return True
            self.ib.sleep(0.1)
        return False
