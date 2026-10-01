# manager/tests/test_ib_adapter.py
from manager.engine.models import BUY, SELL
from manager.worker.ib.adapter import FakeIbGateway


def make_gw(**kw):
    kw.setdefault("prices", {"YM": (45_000.0, 45_001.0)})
    kw.setdefault("contract_multipliers", {"YM": 5.0})
    kw.setdefault("tick_sizes", {"YM": 0.25})
    kw.setdefault("months", {"YM": ["202612", "202703"]})
    kw.setdefault("open_interest", {"YM": {"202612": 5000.0}})
    kw.setdefault("margin_per_contract", {"YM": 12_000.0})
    return FakeIbGateway(**kw)


def test_initialize_and_account():
    gw = make_gw(account={"balance": 50_000.0, "margin_available": 90_000.0})
    assert gw.initialize("127.0.0.1", 4002, 7) is True
    acc = gw.account()
    assert acc["balance"] == 50_000.0 and acc["margin_available"] == 90_000.0


def test_resolve_contract_picks_front_month():
    gw = make_gw()
    assert gw.initialize("h", 1, 1)
    r = gw.resolve_contract("YM", today="20261002", exchange="CME")
    assert r == {"month": "202612", "rolling": False, "multiplier": 5.0,
                 "tick_size": 0.25, "margin_est": 12_000.0}


def test_resolve_contract_unknown_symbol_is_none():
    gw = make_gw()
    gw.initialize("h", 1, 1)
    assert gw.resolve_contract("NOPE", today="20261002", exchange="CME") is None


def test_open_bracket_fills_and_creates_children():
    gw = make_gw(); gw.initialize("h", 1, 1)
    ok, px, err = gw.open_bracket("YM", BUY, 2.0, 44_950.0, 45_100.0,
                                  "CPY#1|MV0.28|SV2")
    assert ok and px == 45_001.0 and err == ""
    pos = gw.net_positions()
    assert len(pos) == 1 and pos[0].side == BUY and pos[0].qty == 2.0
    kids = [o for o in gw.tagged_orders() if o.order_type in ("STP", "LMT")]
    assert {k.order_type for k in kids} == {"STP", "LMT"}
    assert all(k.active for k in kids)
    oca = {k.oca_group for k in kids}
    assert len(oca) == 1 and next(iter(oca)) != ""


def test_stop_fires_with_oca_sibling_cancel():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 2.0, 44_950.0, 45_100.0, "CPY#1|MV0.28|SV2")
    gw.set_price("YM", 44_940.0, 44_941.0)      # bid breaks the stop
    kids = [o for o in gw.tagged_orders() if o.order_type in ("STP", "LMT")]
    stp = next(k for k in kids if k.order_type == "STP")
    lmt = next(k for k in kids if k.order_type == "LMT")
    assert stp.filled and lmt.filled            # stopped, sibling OCA-cancelled
    assert gw.closed_per_tag("CPY#1|MV0.28|SV2") == 2.0
    assert gw.net_positions() == []             # flat


def test_partial_reduce_then_close():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 4.0, 44_950.0, 45_100.0, "CPY#1|MV0.28|SV4")
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV4", close=False)
    assert ok and filled == 2.0
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV4", close=True)
    assert ok and filled == 2.0
    assert gw.net_positions() == []
    # close=True + flat net => that tag's children are cancelled
    assert all(o.filled for o in gw.tagged_orders())
    assert gw.closed_per_tag("CPY#1|MV0.28|SV4") == 4.0


def test_reduce_cannot_overshoot_net():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 2.0, 0.0, 0.0, "CPY#1|MV0.28|SV2")
    ok, filled, _ = gw.reduce("YM", BUY, 5.0, "CPY#1|MV0.28|SV2", close=True)
    assert ok and filled == 2.0                 # clamped to the net long


def test_reduce_noop_when_position_gone():
    gw = make_gw(); gw.initialize("h", 1, 1)
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV2", close=True)
    assert ok and filled == 0.0                 # tolerated race no-op


def test_reduce_timeout_models_gateway_restart():
    gw = make_gw(fail_reduce_timeout=True); gw.initialize("h", 1, 1)
    ok, filled, err = gw.reduce("YM", BUY, 1.0, "CPY#1|MV0.28|SV2", close=True)
    assert not ok and err == "timeout waiting for fill"


def test_open_bracket_read_only_server():
    gw = make_gw(server_read_only=True); gw.initialize("h", 1, 1)
    ok, px, err = gw.open_bracket("YM", BUY, 1.0, 0.0, 0.0, "CPY#1|MV0.1|SV1")
    assert not ok and "Read-Only" in err


def test_partial_reduce_keeps_open_price():
    # open long 4, reduce to 2 -> the remaining position keeps its avg price
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 4.0, 0.0, 0.0, "CPY#1|MV0.28|SV4")
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV4", close=False)
    assert ok and filled == 2.0
    pos = gw.net_positions()
    assert len(pos) == 1 and pos[0].qty == 2.0
    assert pos[0].open_price == 45_001.0        # the open fill price (ask)
    # full close still drops the position from net_positions
    ok, filled, err = gw.reduce("YM", BUY, 2.0, "CPY#1|MV0.28|SV4", close=True)
    assert ok and filled == 2.0
    assert gw.net_positions() == []


def test_tick_and_positions_open_price():
    gw = make_gw(); gw.initialize("h", 1, 1)
    gw.open_bracket("YM", BUY, 2.0, 0.0, 0.0, "CPY#1|MV0.1|SV2")
    assert gw.tick("YM") == (45_000.0, 45_001.0)
    assert gw.net_positions()[0].open_price == 45_001.0


class _IBStubClass:
    """Stand-in for ib_async.IB: only the methods adapter.py touches."""
    managedAccounts = "DU123"

    def __init__(self):
        self.calls: list[tuple] = []
        self._details = [self._detail("202612"), self._detail("202703")]

    @staticmethod
    def _detail(month):
        from types import SimpleNamespace
        return SimpleNamespace(lastTradeDateOrContractMonth=month,
                               multiplier="5", minTick=0.25, contract=None)

    def connect(self, host, port, clientId=0, timeout=10.0):
        self.calls.append(("connect", host, port, clientId))
        self.connected = True

    def isConnected(self):
        return getattr(self, "connected", False)

    def reqPositions(self): ...
    def reqAccountUpdates(self, subscribe, account_id): ...
    def disconnect(self): ...

    def reqContractDetails(self, contract):
        self.calls.append(("details", contract.symbol))
        return self._details


def test_real_gateway_resolve_uses_pick_front_month(monkeypatch):
    """resolve_contract over a stubbed ib_async picks the month through the
    same pure helper the fake uses (roll window -> next month)."""
    import sys
    import types
    from types import SimpleNamespace
    from manager.worker.ib import adapter as ad

    stub = types.ModuleType("ib_async")
    stub.IB = _IBStubClass
    stub.Contract = lambda **kw: SimpleNamespace(**kw)
    stub.MarketOrder = stub.StopOrder = stub.LimitOrder = \
        lambda *a, **k: SimpleNamespace(orderId=0)
    monkeypatch.setitem(sys.modules, "ib_async", stub)

    ib = _IBStubClass()
    ib.connected = True
    gw = ad.RealIbGateway()
    gw.ib = ib                      # skip connect; simulate an initialized gw
    r = gw.resolve_contract("YM", today="20261226", exchange="CME", roll_days=5)
    assert r["month"] == "202703" and r["rolling"] is True   # roll window
    assert r["multiplier"] == 5.0 and r["tick_size"] == 0.25
    # the whatIf margin probe failed (stubbed placeOrder absent here) -> 0.0
    assert r["margin_est"] == 0.0


def test_adapter_import_does_not_require_ib_async():
    import manager.worker.ib.adapter as mod
    mod.RealIbGateway  # class object exists without ib_async present at import
