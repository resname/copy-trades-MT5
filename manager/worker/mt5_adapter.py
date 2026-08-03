from __future__ import annotations

from typing import Protocol

from manager.engine.models import Position, SymbolInfo, BUY, SELL
from manager.worker.mt5_constants import (
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, ORDER_TYPE_BUY,
    TRADE_RETCODE_DONE,
)


class Mt5Adapter(Protocol):
    """The terminal-touching seam. FakeMt5 implements it for tests;
    RealMt5 wraps the MetaTrader5 package (lazy-imported)."""
    def initialize(self, path: str, login: int | None = None,
                   password: str | None = None, server: str | None = None,
                   portable: bool = False) -> bool: ...
    def shutdown(self) -> None: ...
    def last_error(self) -> tuple[int, str]: ...
    def positions_get(self) -> list[Position]: ...
    def position_by_ticket(self, ticket: int) -> Position | None: ...
    def symbol_info(self, symbol: str) -> SymbolInfo | None: ...
    def symbol_info_tick(self, symbol: str) -> tuple[float, float] | None: ...
    def account_info(self) -> dict: ...
    def order_send(self, request: dict) -> dict: ...


class FakeMt5:
    """Scripted, in-memory adapter. Simulates order effects so worker tests see
    realistic post-order position state. `order_results` (optional) is a list
    of canned result dicts popped in order; when it runs out, success is
    assumed and the position list is mutated per the request."""
    def __init__(self, positions=None, symbol_infos=None, account=None,
                 ticks=None, order_results=None):
        self.positions: list[Position] = list(positions or [])
        self.symbol_infos: dict[str, SymbolInfo] = dict(symbol_infos or {})
        self.account: dict = dict(account or {})
        self.ticks: dict[str, tuple[float, float]] = dict(ticks or {})
        self._canned = list(order_results or [])
        self._order_seq = 500000
        self._last_error: tuple[int, str] = (0, "")
        self._connected = False

    def initialize(self, path, login=None, password=None, server=None,
                   portable=False):
        self._connected = True
        return True

    def shutdown(self):
        self._connected = False

    def last_error(self):
        return self._last_error

    def positions_get(self):
        return list(self.positions)

    def position_by_ticket(self, ticket):
        for p in self.positions:
            if p.ticket == ticket:
                return p
        return None

    def symbol_info(self, symbol):
        return self.symbol_infos.get(symbol)

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def account_info(self):
        return dict(self.account)

    def order_send(self, request: dict) -> dict:
        action = request["action"]
        # canned result overrides simulation
        if self._canned:
            res = self._canned.pop(0)
            if res.get("retcode") != TRADE_RETCODE_DONE:
                self._last_error = (res["retcode"], "canned failure")
            return res

        if action == TRADE_ACTION_DEAL:
            pos = request.get("position", 0)
            existing = self.position_by_ticket(pos) if pos else None
            if existing is not None:
                # close or partial close of an existing position
                remaining = round(existing.volume - request["volume"], 8)
                if remaining <= 0.0:
                    self.positions = [p for p in self.positions if p.ticket != pos]
                else:
                    self.positions = [
                        p if p.ticket != pos else
                        Position(p.ticket, p.symbol, p.side, p.open_price,
                                 remaining, p.sl, p.tp, p.open_time, p.point,
                                 p.comment, p.magic)
                        for p in self.positions
                    ]
                return {"retcode": TRADE_RETCODE_DONE, "order": pos, "deal": pos,
                        "price": request["price"], "volume": request["volume"]}
            # open new position
            self._order_seq += 1
            ticket = self._order_seq
            sym = request["symbol"]
            info = self.symbol_infos.get(sym)
            point = info.point if info else 0.00001
            side = BUY if request["type"] == ORDER_TYPE_BUY else SELL
            new = Position(ticket=ticket, symbol=sym, side=side,
                           open_price=request["price"],
                           volume=request["volume"], sl=request.get("sl", 0.0),
                           tp=request.get("tp", 0.0), open_time=0, point=point,
                           comment=request.get("comment", ""),
                           magic=request.get("magic", 0))
            self.positions.append(new)
            return {"retcode": TRADE_RETCODE_DONE, "order": ticket, "deal": ticket,
                    "price": request["price"], "volume": request["volume"]}

        if action == TRADE_ACTION_SLTP:
            pos = request["position"]
            for i, p in enumerate(self.positions):
                if p.ticket == pos:
                    self.positions[i] = Position(
                        p.ticket, p.symbol, p.side, p.open_price, p.volume,
                        request.get("sl", 0.0), request.get("tp", 0.0),
                        p.open_time, p.point, p.comment, p.magic)
                    break
            return {"retcode": TRADE_RETCODE_DONE, "order": pos, "deal": 0,
                    "price": 0.0, "volume": 0.0}

        self._last_error = (-1, "unknown action")
        return {"retcode": -1, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0}


class RealMt5:
    """Wraps the MetaTrader5 package. Imports it lazily inside methods so the
    module imports cleanly on machines without MetaTrader5 installed (tests,
    CI). NOT unit-tested; exercised only by the manual demo smoke test."""
    def __init__(self):
        self._mt5 = None
        self._last_error = (0, "")

    def _mod(self):
        if self._mt5 is None:
            import MetaTrader5 as mt5  # lazy
            self._mt5 = mt5
        return self._mt5

    def initialize(self, path, login=None, password=None, server=None,
                   portable=False):
        mt5 = self._mod()
        kwargs = {"path": path, "portable": portable}
        if login is not None:
            kwargs["login"] = int(login)
            kwargs["password"] = password
            kwargs["server"] = server
        ok = mt5.initialize(**kwargs)
        if not ok:
            self._last_error = mt5.last_error()
        return bool(ok)

    def shutdown(self):
        self._mod().shutdown()

    def last_error(self):
        return self._mod().last_error()

    def positions_get(self):
        mt5 = self._mod()
        raw = mt5.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            info = self.symbol_info(p.symbol)
            point = info.point if info else 0.0
            out.append(Position(ticket=p.ticket, symbol=p.symbol, side=p.type,
                                open_price=p.price_open, volume=p.volume,
                                sl=p.sl, tp=p.tp, open_time=p.time, point=point,
                                comment=p.comment, magic=p.magic))
        return out

    def position_by_ticket(self, ticket):
        for p in self.positions_get():
            if p.ticket == ticket:
                return p
        return None

    def symbol_info(self, symbol):
        mt5 = self._mod()
        si = mt5.symbol_info(symbol)
        if si is None:
            return None
        return SymbolInfo(point=si.point, digits=si.digits,
                          tick_size=si.trade_tick_size,
                          volume_step=si.volume_step, volume_min=si.volume_min,
                          volume_max=si.volume_max)

    def symbol_info_tick(self, symbol):
        mt5 = self._mod()
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        return (t.bid, t.ask)

    def account_info(self):
        mt5 = self._mod()
        a = mt5.account_info()
        if a is None:
            return {}
        return {"login": a.login, "balance": a.balance, "equity": a.equity,
                "currency": a.currency, "server": a.server}

    def order_send(self, request: dict):
        mt5 = self._mod()
        result = mt5.order_send(request)
        if result is None:
            return {"retcode": -1, "order": 0, "deal": 0, "price": 0.0,
                    "volume": 0.0}
        return {"retcode": result.retcode, "order": result.order,
                "deal": result.deal, "price": result.price,
                "volume": result.volume}
