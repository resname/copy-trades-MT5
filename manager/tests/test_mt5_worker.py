import pytest

from manager.engine.models import Position, Record, SymbolInfo, BUY, SELL
from manager.engine.linkage import magic_for, encode_comment, MAGIC_BASE
from manager.ipc.messages import CommandMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, StatusMsg
from manager.worker.mt5_adapter import FakeMt5
from manager.worker.mt5_worker import (
    build_snapshot, build_recovery_records, build_symbol_info_msg, slave_init,
    execute_command,
)

NOW = 1700000000
SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)


def _adapter(positions=(), ticks=None, order_results=None):
    return FakeMt5(
        positions=list(positions),
        symbol_infos={"EURUSD": SI, "GBPUSD": SI},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks=ticks or {"EURUSD": (1.10000, 1.10010)},  # (bid, ask)
        order_results=order_results,
    )


# ---- build_snapshot ----

def test_build_snapshot():
    mt = _adapter(positions=[Position(1, "EURUSD", BUY, 1.1, 0.5, 0, 0, 0, 0.00001)])
    snap = build_snapshot(mt, heartbeat=7, now=NOW)
    assert isinstance(snap, SnapshotMsg)
    assert snap.timestamp == NOW and snap.heartbeat == 7
    assert len(snap.positions) == 1 and snap.positions[0].ticket == 1


# ---- build_recovery_records ----

def test_build_recovery_records_decodes_copied_positions():
    cmt = encode_comment(42, 0.50, 0.10)
    mt = _adapter(positions=[
        Position(777, "EURUSD", BUY, 1.10, 0.10, 0, 0, 0, 0.00001, comment=cmt),  # copied
        Position(888, "EURUSD", BUY, 1.10, 0.20, 0, 0, 0, 0.00001, comment="manual"),  # not copied
    ])
    # FakeMt5 does not set magic on its positions; recovery keys on the CPY comment.
    recs = build_recovery_records(mt)
    assert len(recs) == 1
    assert recs[0] == Record(master_ticket=42, magic=magic_for(42),
                             slave_ticket=777, master_open_volume=0.50,
                             slave_open_volume=0.10)


def test_build_recovery_records_skips_malformed_comment():
    mt = _adapter(positions=[
        Position(777, "EURUSD", BUY, 1.10, 0.10, 0, 0, 0, 0.00001,
                 comment="CPY#99|MV0.5"),  # SV missing -> incomplete
    ])
    assert build_recovery_records(mt) == []


# ---- build_symbol_info_msg ----

def test_build_symbol_info_msg_reports_mapped_slave_symbols():
    msg = build_symbol_info_msg(_adapter(), slave_id="s1",
                                symbol_map_csv="EURUSD=EURUSD,GBPUSD=GBPUSD")
    assert isinstance(msg, SymbolInfoMsg)
    assert set(msg.infos.keys()) == {"EURUSD", "GBPUSD"}
    assert msg.infos["EURUSD"].volume_step == 0.01


# ---- slave_init ----

def test_slave_init_emits_recovery_symbolinfo_status():
    cmt = encode_comment(42, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cfg = {"slave_id": "s1", "symbol_map_csv": "EURUSD=EURUSD",
          "login": 123, "server": "Demo"}
    rec, si, st = slave_init(mt, cfg)
    assert isinstance(rec, RecoveryMsg) and rec.records[0].master_ticket == 42
    assert isinstance(si, SymbolInfoMsg) and "EURUSD" in si.infos
    assert isinstance(st, StatusMsg) and st.role == "slave" and st.connected is True
    assert st.balance == 1000.0


# ---- execute_command: OPEN ----

def test_execute_open_normalizes_sltp_and_opens():
    mt = _adapter()
    cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42, symbol="EURUSD",
                     volume=0.10, sl=1.09500, tp=1.10500, master_open_price=1.10000,
                     side=BUY, magic=magic_for(42),
                     comment=encode_comment(42, 0.50, 0.10))
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok and ack.action == "OPEN" and ack.master_ticket == 42
    assert ack.fill_volume == 0.10
    # slave opened at ask 1.10010; raw distance SL = master_open - master_sl = 0.00500
    # slave_sl = slave_open - 0.00500 = 1.10010 - 0.00500 = 1.09510 (tick-rounded)
    pos = mt.positions_get()[-1]
    assert pos.sl == pytest.approx(1.09510, abs=1e-8)
    assert pos.tp == pytest.approx(1.10510, abs=1e-8)
    assert pos.magic == magic_for(42)


def test_execute_open_without_normalize_passes_raw_sltp():
    mt = _adapter()
    cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42, symbol="EURUSD",
                     volume=0.10, sl=1.09500, tp=1.10500, master_open_price=1.10000,
                     side=BUY, magic=magic_for(42), comment=encode_comment(42, 0.50, 0.10))
    ack = execute_command(mt, cmd, normalize_sltp=False, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    pos = mt.positions_get()[-1]
    assert pos.sl == 1.09500 and pos.tp == 1.10500


# ---- execute_command: MODIFY ----

def test_execute_modify_normalizes_to_fill_price():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1, slave_ticket=777,
                     sl=1.09400, tp=1.10600, master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    pos = mt.position_by_ticket(777)
    # slave_open = 1.10010; SL dist = 1.10000-1.09400 = 0.00600 -> 1.10010-0.00600 = 1.09410
    assert pos.sl == pytest.approx(1.09410, abs=1e-8)
    assert pos.tp == pytest.approx(1.10610, abs=1e-8)


# ---- execute_command: PARTIAL_CLOSE (slave computes volume) ----

def test_execute_partial_close_uses_live_current_volume():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.30,
                     master_open_volume=0.50, slave_open_volume=0.10)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    # fraction = 0.30/0.50 = 0.6; target = 0.10*0.6 = 0.06; close 0.10-0.06 = 0.04
    assert mt.position_by_ticket(777).volume == pytest.approx(0.06, abs=1e-8)
    assert ack.remaining_volume == pytest.approx(0.06, abs=1e-8)


def test_execute_partial_close_noop_when_target_meets_current():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.06, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.30,
                     master_open_volume=0.50, slave_open_volume=0.10)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok  # nothing to close; not an error
    assert ack.remaining_volume == pytest.approx(0.06, abs=1e-8)


# ---- execute_command: CLOSE ----

def test_execute_close_removes_position():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=777)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok and mt.position_by_ticket(777) is None


def test_execute_open_failure_returns_failed_ack():
    mt = _adapter(order_results=[{"retcode": 10004, "order": 0}])
    cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42, symbol="EURUSD",
                     volume=0.10, sl=1.095, tp=1.105, master_open_price=1.10,
                     side=BUY, magic=1, comment="x")
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == 10004