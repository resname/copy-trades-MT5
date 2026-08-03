from manager.engine.models import Position, Snapshot, Event, Record, BUY, SELL


def test_buy_sell_constants_match_mt5():
    assert BUY == 0
    assert SELL == 1


def test_position_construction_and_defaults():
    p = Position(
        ticket=12345, symbol="EURUSD", side=BUY, open_price=1.10,
        volume=0.5, sl=1.09, tp=1.11, open_time=1700000000, point=0.00001,
    )
    assert p.ticket == 12345
    assert p.comment == ""  # default
    assert p.side == BUY


def test_snapshot_holds_tuple_of_positions():
    p = Position(ticket=1, symbol="X", side=BUY, open_price=1.0,
                 volume=0.1, sl=0.0, tp=0.0, open_time=0, point=0.0001)
    s = Snapshot(timestamp=1700000000, heartbeat=1, positions=(p,))
    assert s.positions == (p,)
    assert len(s.positions) == 1


def test_event_kinds_and_record_fields():
    p = Position(ticket=1, symbol="X", side=SELL, open_price=1.0,
                 volume=0.1, sl=0.0, tp=0.0, open_time=0, point=0.0001)
    e = Event(kind="NEW", position=p)
    assert e.kind == "NEW"
    assert e.position == p
    r = Record(master_ticket=1, magic=1000001, slave_ticket=99,
               master_open_volume=0.5, slave_open_volume=0.05)
    assert r.master_ticket == 1


from manager.engine.models import SymbolInfo


def test_symbol_info_fields():
    si = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                    volume_step=0.01, volume_min=0.01, volume_max=100.0)
    assert si.point == 0.00001
    assert si.digits == 5
    assert si.tick_size == 0.00001
    assert si.volume_step == 0.01
    assert si.volume_min == 0.01
    assert si.volume_max == 100.0


def test_symbol_info_is_frozen():
    si = SymbolInfo(point=0.01, digits=2, tick_size=0.01,
                    volume_step=0.1, volume_min=0.1, volume_max=10.0)
    import dataclasses
    assert dataclasses.is_dataclass(si)
    # frozen: assignment must raise
    try:
        si.point = 0.0  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("SymbolInfo must be frozen")
