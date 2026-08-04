"""Integration test composing all Plan 1 engine modules through the
NEW -> MODIFY -> PARTIAL -> CLOSE lifecycle. Uses a test-local helper that
calls the modules in sequence (the production copy-loop arrives in Plan 2)."""

from manager.engine.models import Position, Snapshot, BUY, Record
from manager.engine.linkage import magic_for, encode_comment
from manager.engine.transform import (
    SymbolMapper, calculate_lots, normalize_sltp, round_to_tick,
)
from manager.engine.snapshot_diff import diff
from manager.engine.record_table import RecordTable
from manager.engine.baseline import is_too_old, seed_from_recovery

NOW = 1700000000

# Slave config + slave symbol info (injected, deterministic).
SLAVE_SYMBOL_INFO = {"lot_step": 0.01, "min_lot": 0.01, "max_lot": 100,
                     "tick_size": 0.00001, "digits": 5}
SLAVE_CFG = {"balance": 1000, "step_amount": 100, "step_size": 0.01,
             "max_lot": 10, "max_age_minutes": 10, "normalize_sltp": True}
SLAVE_OPEN_PRICE = 1.20000  # the slave fill price (from the worker's Ack in production)


def _master_pos(ticket, volume=0.5, sl=1.09500, tp=1.10500, open_time=NOW):
    return Position(ticket=ticket, symbol="EURUSD", side=BUY, open_price=1.10000,
                    volume=volume, sl=sl, tp=tp, open_time=open_time, point=0.00001)


def _handle_new(event, table, mapper):
    """Returns the OPEN parameters the slave worker would receive, or None to skip."""
    pos = event.position
    if table.has(pos.ticket):
        return None  # already copied
    if is_too_old(pos.open_time, NOW, SLAVE_CFG["max_age_minutes"]):
        return None  # baseline: too old
    slave_symbol = mapper.resolve(pos.symbol)
    if not slave_symbol:
        return None  # not mappable
    lots = calculate_lots(
        SLAVE_CFG["balance"], SLAVE_CFG["step_amount"], SLAVE_CFG["step_size"],
        SLAVE_CFG["max_lot"], SLAVE_SYMBOL_INFO["lot_step"],
        SLAVE_SYMBOL_INFO["min_lot"], SLAVE_SYMBOL_INFO["max_lot"],
    )
    assert lots > 0.0
    if SLAVE_CFG["normalize_sltp"]:
        slave_sl, slave_tp = normalize_sltp(pos.open_price, pos.sl, pos.tp,
                                            SLAVE_OPEN_PRICE, pos.side)
    else:
        slave_sl, slave_tp = pos.sl, pos.tp
    slave_sl = round_to_tick(slave_sl, SLAVE_SYMBOL_INFO["tick_size"],
                             SLAVE_SYMBOL_INFO["digits"])
    slave_tp = round_to_tick(slave_tp, SLAVE_SYMBOL_INFO["tick_size"],
                             SLAVE_SYMBOL_INFO["digits"])
    comment = encode_comment(pos.ticket, pos.volume, lots)
    magic = magic_for(pos.ticket)
    # simulate the worker's Ack: slave ticket 777, fill at SLAVE_OPEN_PRICE
    table.add(Record(master_ticket=pos.ticket, magic=magic, slave_ticket=777,
                     master_open_volume=pos.volume, slave_open_volume=lots))
    return {"symbol": slave_symbol, "lots": lots, "sl": slave_sl, "tp": slave_tp,
            "magic": magic, "comment": comment}


def test_clean_start_copies_recent_open():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    events = diff(prev=[], curr=[_master_pos(12345)])
    assert [e.kind for e in events] == ["NEW"]
    params = _handle_new(events[0], table, mapper)
    assert params == {
        "symbol": "EURUSD", "lots": 0.10, "sl": 1.19500, "tp": 1.20500,
        "magic": 1012345, "comment": "CPY#12345|MV0.5|SV0.1",
    }
    assert table.has(12345)


def test_old_position_at_start_is_skipped():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    # 30 minutes old, max_age 10 -> skipped
    events = diff(prev=[], curr=[_master_pos(12345, open_time=NOW - 30 * 60)])
    assert _handle_new(events[0], table, mapper) is None
    assert not table.has(12345)


def test_restart_recovery_prevents_duplicate_open():
    table = RecordTable()
    recovered = [Record(master_ticket=12345, magic=1012345, slave_ticket=777,
                        master_open_volume=0.5, slave_open_volume=0.10)]
    seed_from_recovery(table, recovered)
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    events = diff(prev=[], curr=[_master_pos(12345)])
    assert _handle_new(events[0], table, mapper) is None  # already in table
    assert len(table) == 1


def test_modify_renormalizes_to_slave_open():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    # establish the position first
    _handle_new(diff(prev=[], curr=[_master_pos(12345)])[0], table, mapper)
    # master moves TP from 1.10500 to 1.11000
    events = diff(prev=[_master_pos(12345)], curr=[_master_pos(12345, tp=1.11000)])
    assert [e.kind for e in events] == ["MODIFY"]
    pos = events[0].position
    slave_sl, slave_tp = normalize_sltp(pos.open_price, pos.sl, pos.tp,
                                        SLAVE_OPEN_PRICE, pos.side)
    slave_tp = round_to_tick(slave_tp, SLAVE_SYMBOL_INFO["tick_size"],
                             SLAVE_SYMBOL_INFO["digits"])
    assert slave_tp == 1.21000  # 1.20000 + (1.11000 - 1.10000)


def test_partial_close_math():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    _handle_new(diff(prev=[], curr=[_master_pos(12345)])[0], table, mapper)
    rec = table.get(12345)
    # master volume 0.5 -> 0.3
    events = diff(prev=[_master_pos(12345, volume=0.5)],
                  curr=[_master_pos(12345, volume=0.3)])
    assert [e.kind for e in events] == ["PARTIAL"]
    fraction = 0.3 / rec.master_open_volume       # 0.3 / 0.5 = 0.6
    target = rec.slave_open_volume * fraction      # 0.10 * 0.6 = 0.06
    current_slave_volume = 0.10                     # slave still holds full size
    vol_to_close = current_slave_volume - target    # 0.04
    import math
    vol_to_close = math.floor(vol_to_close / SLAVE_SYMBOL_INFO["lot_step"]) \
        * SLAVE_SYMBOL_INFO["lot_step"]
    assert vol_to_close == 0.04


def test_close_removes_record():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    _handle_new(diff(prev=[], curr=[_master_pos(12345)])[0], table, mapper)
    events = diff(prev=[_master_pos(12345)], curr=[])
    assert [e.kind for e in events] == ["CLOSE"]
    assert events[0].position.ticket == 12345
    # the production copy-loop would call table.remove(12345) after a successful close
    table.remove(12345)
    assert not table.has(12345)
