from manager.engine.models import Position, Snapshot, Record, SymbolInfo, BUY
from manager.engine.linkage import magic_for
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.ipc.messages import StatusMsg, RecoveryMsg

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = 1700000000


def _engine():
    eng = CopyEngine()
    eng.add_slave(SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                              step_amount=100.0, step_size=0.01, max_lot=10.0,
                              max_trade_age_minutes=10, normalize_sltp=True))
    eng.apply_symbol_info("s1", {"EURUSD": SI})
    eng.apply_status("s1", StatusMsg(source_id="s1", role="slave", connected=True,
                    login=2, balance=1000.0, equity=1000.0, currency="USD",
                    server="Demo"))
    return eng


def _pos(ticket=42, volume=0.5):
    return Position(ticket=ticket, symbol="EURUSD", side=BUY, open_price=1.10000,
                    volume=volume, sl=1.09500, tp=1.10500, open_time=NOW, point=0.00001)


def test_recovery_seeds_table_so_new_is_skipped():
    eng = _engine()
    added = eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    assert added == 1
    cmds = eng.ingest_snapshot(Snapshot(timestamp=NOW, heartbeat=1,
                                        positions=(_pos(42),)), now=NOW)["s1"]
    assert cmds == []  # already copied -> no duplicate OPEN


def test_recovery_does_not_overwrite_existing_record():
    eng = _engine()
    original = Record(42, magic_for(42), 111, 0.5, 0.10)
    eng.apply_recovery("s1", [original])
    # a second recovery with a different slave_ticket must not overwrite
    added = eng.apply_recovery("s1", [Record(42, magic_for(42), 999, 0.5, 0.20)])
    assert added == 0
    assert eng._slaves["s1"].table.get(42).slave_ticket == 111


def test_restart_reset_then_recovery_reseeds_no_duplicate():
    eng = _engine()
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    # simulate worker restart: table cleared, then slave re-sends recovery
    eng.reset_slave("s1")
    assert eng._slaves["s1"].table.has(42) is False
    eng.apply_recovery("s1", [Record(42, magic_for(42), 778, 0.5, 0.10)])
    cmds = eng.ingest_snapshot(Snapshot(timestamp=NOW, heartbeat=1,
                                        positions=(_pos(42),)), now=NOW)["s1"]
    assert cmds == []  # re-seeded -> no duplicate OPEN after restart


def test_recovery_plus_recent_open_copies_the_new_one():
    eng = _engine()
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    # 42 already copied (skip); 43 is a fresh recent open -> OPEN
    cmds = eng.ingest_snapshot(Snapshot(timestamp=NOW, heartbeat=1,
                                        positions=(_pos(42), _pos(43))), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "OPEN" and cmds[0].master_ticket == 43