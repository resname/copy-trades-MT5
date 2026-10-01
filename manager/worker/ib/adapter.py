# manager/worker/ib/adapter.py
from __future__ import annotations

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
        flipped = current != 0.0 and (current > 0) != (signed > 0)
        if flipped:
            self.avg_price.pop(symbol, None)
        self.positions[symbol] = current + signed
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
