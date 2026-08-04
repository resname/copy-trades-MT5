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


def test_update_slave_config_updates_fields_without_rebuilding_mapper():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    changed = eng.update_slave_config(
        "s1", step_amount=200.0, step_size=0.02, max_lot=20.0,
        max_trade_age_minutes=5, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=False)
    assert changed is False  # symbol_map_csv unchanged -> no mapper rebuild
    cfg = eng._slaves["s1"].config
    assert cfg.step_amount == 200.0 and cfg.step_size == 0.02
    assert cfg.max_lot == 20.0 and cfg.max_trade_age_minutes == 5
    assert cfg.normalize_sltp is False


def test_update_slave_config_rebuilds_mapper_when_map_changes():
    eng = _engine(infos={"s1": {"EURUSD": SI, "GBPUSD": SI}})
    changed = eng.update_slave_config(
        "s1", step_amount=100.0, step_size=0.01, max_lot=10.0,
        max_trade_age_minutes=10, symbol_map_csv="EURUSD=GBPUSD",
        normalize_sltp=True)
    assert changed is True
    # master EURUSD now resolves to slave GBPUSD on the next NEW
    cmds = eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "OPEN"
    assert cmds[0].symbol == "GBPUSD"


def test_update_slave_config_new_open_uses_new_lots():
    eng = _engine(infos={"s1": {"EURUSD": SI}})  # balance 1000
    eng.update_slave_config(
        "s1", step_amount=500.0, step_size=0.02, max_lot=99.0,
        max_trade_age_minutes=10, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=True)
    cmds = eng.ingest_snapshot(_snap([_pos(99)]), now=NOW)["s1"]
    # steps=floor(1000/500)=2; lots=2*0.02=0.04 (volume_step 0.01)
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.04, abs=1e-8)


def test_update_slave_config_does_not_affect_open_trades():
    """An edit must not alter MODIFY/PARTIAL_CLOSE/CLOSE for an already-open
    position: those route via the RecordTable (slave_ticket + stored open
    volumes), not the live config. Only NEW reads the config/mapper."""
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.apply_recovery("s1", [Record(42, magic_for(42), 777, 0.5, 0.10)])
    eng.ingest_snapshot(_snap([_pos(42)]), now=NOW)  # establish prev (NEW skipped, in table)
    # edit everything the engine holds
    eng.update_slave_config(
        "s1", step_amount=999.0, step_size=0.5, max_lot=99.0,
        max_trade_age_minutes=1, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=False)
    # MODIFY on the open position: still routed to slave_ticket 777
    cmds = eng.ingest_snapshot(
        _snap([_pos(42, sl=1.09000, tp=1.11000)]), now=NOW)["s1"]
    assert len(cmds) == 1 and cmds[0].action == "MODIFY"
    assert cmds[0].slave_ticket == 777
    # ack the MODIFY so the ticket leaves pending (otherwise the next event
    # would be coalesced into `held`, which would mask the safety assertion)
    from manager.ipc.messages import AckMsg
    eng.apply_ack("s1", AckMsg(slave_id="s1", action="MODIFY", master_ticket=42,
                              ok=True, slave_ticket=777, retcode=10009))
    # PARTIAL_CLOSE uses stored open volumes (0.5 / 0.10), NOT the new step params
    cmds2 = eng.ingest_snapshot(_snap([_pos(42, volume=0.30)]), now=NOW)["s1"]
    assert cmds2[0].action == "PARTIAL_CLOSE"
    assert cmds2[0].master_open_volume == 0.5 and cmds2[0].slave_open_volume == 0.10


def test_new_copy_master_mode_mirrors_master_lot():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="copy_master")
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.37)]), now=NOW)["s1"]
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.37, abs=1e-8)


def test_new_fixed_lot_mode_uses_fixed_lot():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="fixed_lot", fixed_lot=0.07)
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.5)]), now=NOW)["s1"]
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.07, abs=1e-8)


def test_new_balance_step_with_base_scales_down():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="balance_step", master_base_lot=0.1)
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})  # balance 1000
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.05)]), now=NOW)["s1"]
    # raw_balance 0.10 * (0.05/0.1)=0.05
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.05, abs=1e-8)


def test_new_balance_step_with_base_no_scale_above_base():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="balance_step", master_base_lot=0.1)
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.2)]), now=NOW)["s1"]
    # down-only: master 0.2 >= base 0.1 -> raw_balance 0.10
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.10, abs=1e-8)


def test_update_slave_config_patches_sizing_mode():
    eng = _engine(infos={"s1": {"EURUSD": SI}})  # balance 1000, balance_step
    eng.update_slave_config(
        "s1", step_amount=100.0, step_size=0.01, max_lot=10.0,
        max_trade_age_minutes=10, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=True, sizing_mode="copy_master", master_base_lot=0.0,
        fixed_lot=0.01)
    cfg = eng._slaves["s1"].config
    assert cfg.sizing_mode == "copy_master"
    # next NEW mirrors the master lot, ignoring balance
    cmds = eng.ingest_snapshot(_snap([_pos(7, volume=0.42)]), now=NOW)["s1"]
    assert cmds[0].volume == pytest.approx(0.42, abs=1e-8)