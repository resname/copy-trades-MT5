from manager.engine.models import Position, SymbolInfo, BUY, SELL
from manager.worker.mt5_constants import (
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, ORDER_TYPE_BUY, ORDER_TYPE_SELL,
    ORDER_TIME_GTC, ORDER_FILLING_RETURN, TRADE_RETCODE_DONE,
)
from manager.worker.mt5_adapter import FakeMt5


def _si():
    return SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                     volume_step=0.01, volume_min=0.01, volume_max=100.0)


def _fake(positions=(), ticks=None, order_results=None):
    return FakeMt5(
        positions=list(positions),
        symbol_infos={"EURUSD": _si()},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks=ticks or {"EURUSD": (1.10000, 1.10010)},  # (bid, ask)
        order_results=list(order_results) if order_results else None,
    )


def test_positions_get_returns_copies():
    mt = _fake(positions=[Position(1, "EURUSD", BUY, 1.1, 0.5, 0, 0, 0, 0.00001)])
    got = mt.positions_get()
    assert got[0].ticket == 1
    got[0]  # mutating the returned list must not affect the adapter
    assert mt.positions_get()[0].ticket == 1


def test_symbol_info_tick_and_account():
    mt = _fake()
    assert mt.symbol_info_tick("EURUSD") == (1.10000, 1.10010)
    assert mt.account_info()["login"] == 123


def test_order_send_open_appends_position():
    mt = _fake()
    req = {"action": TRADE_ACTION_DEAL, "symbol": "EURUSD", "type": ORDER_TYPE_BUY,
           "volume": 0.10, "price": 1.10010, "sl": 1.09, "tp": 1.11,
           "deviation": 10, "magic": 1000042, "comment": "CPY#42|MV0.5|SV0.10",
           "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
    res = mt.order_send(req)
    assert res["retcode"] == TRADE_RETCODE_DONE
    assert res["price"] == 1.10010 and res["volume"] == 0.10
    new = mt.positions_get()[-1]
    assert new.side == BUY and new.symbol == "EURUSD" and new.volume == 0.10
    assert new.magic == 1000042 and new.comment == "CPY#42|MV0.5|SV0.10"
    assert new.point == 0.00001  # filled from symbol_info


def test_order_send_modify_updates_sltp():
    mt = _fake(positions=[Position(7, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0, 0.00001,
                                  comment="CPY#1|MV0.5|SV0.10")])
    req = {"action": TRADE_ACTION_SLTP, "symbol": "EURUSD", "position": 7,
           "sl": 1.095, "tp": 1.105}
    res = mt.order_send(req)
    assert res["retcode"] == TRADE_RETCODE_DONE
    pos = mt.position_by_ticket(7)
    assert pos.sl == 1.095 and pos.tp == 1.105


def test_order_send_partial_reduces_volume():
    mt = _fake(positions=[Position(7, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0, 0.00001,
                                  comment="CPY#1|MV0.5|SV0.10")])
    # closing a BUY -> SELL at bid
    req = {"action": TRADE_ACTION_DEAL, "symbol": "EURUSD", "type": ORDER_TYPE_SELL,
           "volume": 0.04, "position": 7, "price": 1.10000,
           "deviation": 10, "magic": 1000042, "comment": "close",
           "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
    res = mt.order_send(req)
    assert res["retcode"] == TRADE_RETCODE_DONE
    assert mt.position_by_ticket(7).volume == 0.06  # 0.10 - 0.04


def test_order_send_full_close_removes_position():
    mt = _fake(positions=[Position(7, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0, 0.00001,
                                  comment="CPY#1|MV0.5|SV0.10")])
    req = {"action": TRADE_ACTION_DEAL, "symbol": "EURUSD", "type": ORDER_TYPE_SELL,
           "volume": 0.10, "position": 7, "price": 1.10000,
           "deviation": 10, "magic": 1000042, "comment": "close",
           "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN}
    mt.order_send(req)
    assert mt.position_by_ticket(7) is None


def test_initialize_shutdown_last_error():
    mt = _fake()
    assert mt.initialize("C:/t/terminal64.exe", 123, "pw", "Demo") is True
    assert mt.last_error() == (0, "")
    mt.shutdown()


def test_failed_order_send_records_retcode():
    mt = _fake(order_results=[{"retcode": 10004, "order": 0}])
    res = mt.order_send({"action": TRADE_ACTION_DEAL, "symbol": "EURUSD",
                         "type": ORDER_TYPE_BUY, "volume": 0.10, "price": 1.1,
                         "deviation": 10, "magic": 1, "comment": "x",
                         "type_time": ORDER_TIME_GTC, "type_filling": ORDER_FILLING_RETURN})
    assert res["retcode"] == 10004
    assert mt.last_error()[0] == 10004