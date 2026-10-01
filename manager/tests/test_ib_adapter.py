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
