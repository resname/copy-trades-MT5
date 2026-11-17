from manager.engine.learned_lots import LearnedMasterLots
from manager.engine.models import Position, BUY

NOW = 1700000000


def _pos(ticket, symbol="EURUSD", volume=0.5):
    return Position(ticket=ticket, symbol=symbol, side=BUY, open_price=1.1,
                    volume=volume, sl=0.0, tp=0.0, open_time=NOW, point=0.00001)


def test_update_learns_largest_volume_per_symbol():
    lots = LearnedMasterLots()
    assert lots.update([_pos(1, volume=10.0)]) is True
    assert lots.update([_pos(2, volume=23.0)]) is True
    assert lots.update([_pos(3, volume=5.0)]) is False
    assert lots.as_dict() == {"EURUSD": 23.0}


def test_update_keeps_symbols_isolated():
    lots = LearnedMasterLots()
    lots.update([_pos(1, "EURUSD", 23.0), _pos(2, "XAUUSD", 0.5)])
    assert lots.get("EURUSD") == 23.0
    assert lots.get("XAUUSD") == 0.5
    assert lots.get("GBPUSD") == 0.0


def test_update_ignores_non_finite_and_non_positive_volumes():
    lots = LearnedMasterLots()
    lots.update([_pos(1, volume=float("nan")), _pos(2, volume=float("inf")),
                 _pos(3, volume=0.0), _pos(4, volume=-5.0)])
    assert lots.as_dict() == {}


def test_initial_values_are_validated():
    lots = LearnedMasterLots({"EURUSD": 23.0, "XAUUSD": float("nan"),
                              "GBPUSD": -1.0, "": 5.0})
    assert lots.as_dict() == {"EURUSD": 23.0}


def test_as_dict_returns_a_copy():
    lots = LearnedMasterLots({"EURUSD": 23.0})
    copy = lots.as_dict()
    copy["EURUSD"] = 99.0
    assert lots.get("EURUSD") == 23.0


def test_reset_clears_and_suppresses_open_tickets_until_they_close():
    lots = LearnedMasterLots({"EURUSD": 23.0})
    lots.reset(open_tickets={7})
    assert lots.as_dict() == {}
    # ticket 7 is still open: it does not re-learn
    assert lots.update([_pos(7, volume=23.0)]) is False
    assert lots.as_dict() == {}
    # once ticket 7 disappears, a fresh large trade learns again
    lots.update([_pos(8, volume=10.0)])
    assert lots.get("EURUSD") == 10.0


def test_reset_frees_suppression_when_the_ticket_disappears():
    lots = LearnedMasterLots({"EURUSD": 23.0})
    lots.reset(open_tickets={7})
    lots.update([])  # ticket 7 is gone
    assert lots.update([_pos(7, volume=23.0)]) is True
    assert lots.get("EURUSD") == 23.0
