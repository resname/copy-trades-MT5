import pytest

from manager.engine.models import Position, Snapshot, Record, SymbolInfo, BUY, SELL
from manager.engine.record_table import RecordTable
from manager.engine.linkage import magic_for, encode_comment
from manager.engine.copy_loop import SlaveConfig, CopyEngine, derive_command

NOW = 1700000000
SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)


def _cfg(slave_id="s1", symbol_map="EURUSD=EURUSD", max_age=10):
    return SlaveConfig(slave_id=slave_id, symbol_map_csv=symbol_map,
                       step_amount=100.0, step_size=0.01, max_lot=10.0,
                       max_trade_age_minutes=max_age, normalize_sltp=True)


def _engine(slaves=(_cfg(),), infos=None, balance=1000.0):
    eng = CopyEngine()
    for cfg in slaves:
        eng.add_slave(cfg)
    if infos:
        for sid, m in infos.items():
            eng.apply_symbol_info(sid, m)
    for sid in (c.slave_id for c in slaves):
        eng.apply_status(sid, _status(balance))
    return eng


def _status(balance=1000.0):
    from manager.ipc.messages import StatusMsg
    return StatusMsg(source_id="s1", role="slave", connected=True, login=1,
                     balance=balance, equity=balance, currency="USD", server="Demo")


def _pos(ticket, volume=0.5, sl=1.09500, tp=1.10500, side=BUY, open_time=NOW):
    return Position(ticket=ticket, symbol="EURUSD", side=side, open_price=1.10000,
                   volume=volume, sl=sl, tp=tp, open_time=open_time, point=0.00001)


def _snap(positions, ts=NOW, hb=1):
    return Snapshot(timestamp=ts, heartbeat=hb, positions=tuple(positions))


def test_new_emits_open_with_lots_raw_sltp_magic_comment():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert len(cmds) == 1
    c = cmds[0]
    assert c.action == "OPEN" and c.master_ticket == 42
    assert c.symbol == "EURUSD" and c.volume == 0.10  # floor(1000/100)*0.01=0.10
    assert c.sl == 1.09500 and c.tp == 1.10500  # RAW master (slave normalizes)
    assert c.master_open_price == 1.10000 and c.side == BUY
    assert c.magic == magic_for(42)
    assert c.comment == encode_comment(42, 0.5, 0.10)


def test_new_already_in_record_table_is_skipped():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds == []


def test_new_too_old_is_skipped():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    old = _pos(42, open_time=NOW - 9999 * 60)  # 9999 min ago, max_age=10
    cmds = eng.ingest_snapshot(_snap([old]), now=NOW)["s1"]
    assert cmds == []


def test_new_unmapped_symbol_is_skipped():
    # info exists for the mapped slave symbol (GBPUSD), but master sends
    # EURUSD which is not in the map and has no fallback info -> skip.
    eng = _engine(infos={"s1": {"GBPUSD": SI}}, slaves=(_cfg(symbol_map="GBPUSD=GBPUSD"),))
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds == []  # EURUSD not in map, no fallback info


def test_new_without_symbol_info_is_skipped():
    eng = _engine()  # no symbol info applied
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds == []


def test_modify_emits_modify_with_slave_ticket_and_raw_sltp():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # establish prev (NEW skipped)
    cmds = eng.ingest_snapshot(
        _snap([_pos(42, sl=1.09000, tp=1.11000)]), now=NOW)["s1"]
    assert len(cmds) == 1
    c = cmds[0]
    assert c.action == "MODIFY" and c.slave_ticket == 777
    assert c.sl == 1.09000 and c.tp == 1.11000  # raw master
    assert c.master_open_price == 1.10000 and c.side == BUY


def test_partial_emits_partial_close_with_open_volumes():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # establish prev (NEW skipped)
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.30)]), now=NOW)["s1"]
    assert len(cmds) == 1
    c = cmds[0]
    assert c.action == "PARTIAL_CLOSE" and c.slave_ticket == 777
    assert c.new_master_volume == 0.30 and c.master_open_volume == 0.5
    assert c.slave_open_volume == 0.10


def test_close_emits_close_command():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    # first snapshot establishes prev; second has ticket gone -> CLOSE
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)
    cmds = eng.ingest_snapshot(_snap([]), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "CLOSE" and cmds[0].slave_ticket == 777


def test_apply_ack_open_ok_sets_slave_ticket_and_fill_volume():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)
    from manager.ipc.messages import AckMsg
    reemitted = eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN",
              master_ticket=42, ok=True, slave_ticket=777, fill_price=1.10010,
              fill_volume=0.10, remaining_volume=0.10, retcode=10009))
    assert reemitted == []
    rec = eng._slaves["s1"].table.get(42)
    assert rec.slave_ticket == 777 and rec.slave_open_volume == 0.10


def test_apply_ack_open_fail_leaves_failed_record():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)
    from manager.ipc.messages import AckMsg
    eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN", master_ticket=42,
                              ok=False, retcode=10004, error="requote"))
    rec = eng._slaves["s1"].table.get(42)
    assert rec.slave_ticket == 0  # failed-open marker; not re-NEW'd


def test_apply_ack_close_ok_removes_record():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    from manager.ipc.messages import AckMsg
    eng.apply_ack("s1", AckMsg(slave_id="s1", action="CLOSE", master_ticket=42,
                              ok=True, slave_ticket=777, retcode=10009))
    assert eng._slaves["s1"].table.has(42) is False


def test_pending_holds_modify_until_open_ack_then_reemits():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    # snapshot 1: NEW -> OPEN (optimistic record, pending)
    cmds1 = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert cmds1[0].action == "OPEN"
    # snapshot 2 (before OPEN ack): master modifies -> held, not sent
    cmds2 = eng.ingest_snapshot(_snap([_pos(42, sl=1.09000, tp=1.11000)]), now=NOW)["s1"]
    assert cmds2 == []  # MODIFY held pending the OPEN ack
    # OPEN ack arrives -> re-emit the held MODIFY (now slave_ticket is set)
    from manager.ipc.messages import AckMsg
    reemitted = eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN",
              master_ticket=42, ok=True, slave_ticket=777, fill_volume=0.10,
              fill_price=1.10010, remaining_volume=0.10, retcode=10009))
    assert len(reemitted) == 1 and reemitted[0].action == "MODIFY"
    assert reemitted[0].slave_ticket == 777 and reemitted[0].sl == 1.09000


def test_pending_coalesces_two_partials_to_latest():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # establish prev
    # snapshot: partial to 0.30 -> PARTIAL_CLOSE sent (pending)
    cmds1 = eng.ingest_snapshot(_snap([_pos(42, volume=0.30)]), now=NOW)["s1"]
    assert cmds1[0].action == "PARTIAL_CLOSE"
    # next snapshot (before ack): partial further to 0.20 -> held (coalesce)
    cmds2 = eng.ingest_snapshot(_snap([_pos(42, volume=0.20)]), now=NOW)["s1"]
    assert cmds2 == []
    # ack the first partial -> re-emit the LATEST held (0.20)
    from manager.ipc.messages import AckMsg
    reemitted = eng.apply_ack("s1", AckMsg(slave_id="s1", action="PARTIAL_CLOSE",
              master_ticket=42, ok=True, slave_ticket=777, remaining_volume=0.06,
              retcode=10009))
    assert len(reemitted) == 1 and reemitted[0].action == "PARTIAL_CLOSE"
    assert reemitted[0].new_master_volume == 0.20


def test_close_of_failed_open_cleans_up_record():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # OPEN sent
    from manager.ipc.messages import AckMsg
    eng.apply_ack("s1", AckMsg(slave_id="s1", action="OPEN", master_ticket=42,
                              ok=False, retcode=10004, error="x"))  # failed-open record
    assert eng._slaves["s1"].table.has(42)
    # master closes -> derive_command CLOSE returns None (slave_ticket==0) + cleanup
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # re-establish prev (position still here)
    cmds = eng.ingest_snapshot(_snap([]), now=NOW)["s1"]
    assert cmds == []
    assert eng._slaves["s1"].table.has(42) is False  # cleaned up