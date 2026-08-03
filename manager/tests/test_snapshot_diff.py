from manager.engine.snapshot_diff import diff
from manager.engine.models import Position, BUY


def _pos(ticket, volume=0.5, sl=0.0, tp=0.0, symbol="EURUSD", side=BUY,
          open_price=1.10, open_time=1700000000, point=0.00001):
    return Position(ticket=ticket, symbol=symbol, side=side,
                    open_price=open_price, volume=volume, sl=sl, tp=tp,
                    open_time=open_time, point=point)


def test_new_position():
    events = diff(prev=[], curr=[_pos(1)])
    assert [e.kind for e in events] == ["NEW"]
    assert events[0].position.ticket == 1


def test_no_change_emits_nothing():
    p = _pos(1)
    assert diff(prev=[p], curr=[_pos(1)]) == []


def test_partial_close_when_volume_decreases():
    events = diff(prev=[_pos(1, volume=0.5)], curr=[_pos(1, volume=0.3)])
    assert [e.kind for e in events] == ["PARTIAL"]
    assert events[0].position.volume == 0.3


def test_modify_when_sl_changes():
    events = diff(prev=[_pos(1, sl=1.09)], curr=[_pos(1, sl=1.08)])
    assert [e.kind for e in events] == ["MODIFY"]


def test_modify_when_tp_changes():
    events = diff(prev=[_pos(1, tp=1.11)], curr=[_pos(1, tp=1.12)])
    assert [e.kind for e in events] == ["MODIFY"]


def test_partial_and_modify_both_emitted():
    events = diff(prev=[_pos(1, volume=0.5, tp=1.11)],
                  curr=[_pos(1, volume=0.3, tp=1.12)])
    assert [e.kind for e in events] == ["PARTIAL", "MODIFY"]


def test_close_when_ticket_gone():
    p = _pos(1)
    events = diff(prev=[p], curr=[])
    assert [e.kind for e in events] == ["CLOSE"]
    assert events[0].position.ticket == 1  # CLOSE carries the previous position


def test_mixed_new_modify_close():
    p1, p2 = _pos(1, sl=1.09), _pos(2)
    events = diff(prev=[p1, p2], curr=[_pos(1, sl=1.08), _pos(3)])
    kinds = [e.kind for e in events]
    assert kinds == ["MODIFY", "NEW", "CLOSE"]
    assert events[0].position.ticket == 1   # MODIFY of p1
    assert events[1].position.ticket == 3   # NEW p3
    assert events[2].position.ticket == 2   # CLOSE p2


def test_volume_increase_not_partial():
    # volume increasing is not a partial close (no event for volume up alone)
    events = diff(prev=[_pos(1, volume=0.3)], curr=[_pos(1, volume=0.5)])
    assert events == []
