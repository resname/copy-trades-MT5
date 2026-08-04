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
    cfg = {"slave_id": "s1", "symbol_map_csv": "EURUSD=EURUSD"}
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


def test_worker_loop_exception_surfaces_as_fatal_error(monkeypatch):
    """An unhandled exception in the master loop must be surfaced as a FATAL
    ErrorMsg (not swallowed), so the supervisor stops instead of silently
    restarting a crashing worker (which would cycle the terminal). Before the
    fix, worker_main only caught EOFError, so any other exception killed the
    subprocess with no message back to the manager."""
    import multiprocessing
    from manager.ipc.messages import StartMsg, ErrorMsg, StatusMsg
    from manager.ipc.pipe_framing import send_msg, recv_msg
    from manager.worker import mt5_worker

    class BoomMt5(FakeMt5):
        def positions_get(self):
            raise RuntimeError("boom in loop")

    monkeypatch.setattr(mt5_worker, "FakeMt5", BoomMt5)

    parent, child = multiprocessing.Pipe(duplex=True)
    send_msg(parent, StartMsg(config={"terminal_path": "C:/t/m.exe",
                                      "master_interval_ms": 10}))
    escaped = None
    try:
        mt5_worker.worker_main(child, "master", "fake", {})
    except BaseException as exc:
        escaped = exc
    child.close()

    # Drain everything the worker wrote. On Windows a poll(0) on a
    # peer-closed empty pipe raises BrokenPipeError, so guard the drain.
    msgs = []
    while True:
        try:
            if not parent.poll(0):
                break
        except (BrokenPipeError, EOFError, OSError):
            break
        try:
            msgs.append(recv_msg(parent))
        except (EOFError, OSError, BrokenPipeError):
            break
    parent.close()

    assert escaped is None, f"loop exception must not escape worker_main: {escaped!r}"
    fatal = [m for m in msgs if isinstance(m, ErrorMsg) and m.fatal]
    assert fatal, "loop exception must be surfaced as a fatal ErrorMsg"
    assert "boom in loop" in fatal[0].message
    # the initial StatusMsg is still sent before the crash
    assert any(isinstance(m, StatusMsg) for m in msgs)


def test_slave_loop_reconfigure_re_emits_symbol_info_and_updates_normalize():
    """On ReconfigureMsg the slave loop must (1) re-emit a SymbolInfoMsg for the
    NEW map's slave symbols and (2) apply the new normalize_sltp to subsequent
    commands. Open positions are untouched (MODIFY routes by slave_ticket)."""
    import multiprocessing
    import threading
    from manager.ipc.messages import (
        ReconfigureMsg, CommandMsg, RecoveryMsg, SymbolInfoMsg, StatusMsg, AckMsg,
    )
    from manager.ipc.pipe_framing import send_msg, recv_msg
    from manager.worker.mt5_worker import _slave_loop

    cmt = encode_comment(1, 0.50, 0.10)
    mt = FakeMt5(
        positions=[Position(777, "EURUSD", BUY, 1.10010, 0.10, 1.095, 1.105, 0,
                             0.00001, comment=cmt)],
        symbol_infos={"EURUSD": SI, "GBPUSD": SI},
        account={"login": 2, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks={"EURUSD": (1.10000, 1.10010), "GBPUSD": (1.30000, 1.30010)},
    )
    cfg = {"slave_id": "s1", "symbol_map_csv": "EURUSD=EURUSD",
           "normalize_sltp": True, "retry_count": 1, "retry_delay_ms": 0,
           "slave_status_interval_ms": 60000}

    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop, args=(child, mt, cfg), daemon=True)
    t.start()
    try:
        # drain init: RecoveryMsg, SymbolInfoMsg, StatusMsg
        init = [recv_msg(parent) for _ in range(3)]
        assert isinstance(init[0], RecoveryMsg)
        assert isinstance(init[1], SymbolInfoMsg) and "EURUSD" in init[1].infos
        assert isinstance(init[2], StatusMsg)

        # reconfigure: new map EURUSD->GBPUSD, normalize OFF
        send_msg(parent, ReconfigureMsg(source_id="s1", symbol_map_csv="EURUSD=GBPUSD",
                                        normalize_sltp=False))
        si = recv_msg(parent)
        assert isinstance(si, SymbolInfoMsg)
        assert set(si.infos.keys()) == {"GBPUSD"}  # new map's slave symbol

        # a MODIFY on the open position with raw master SL/TP: because normalize
        # is now False, the slave applies the RAW sl/tp (no re-centering).
        send_msg(parent, CommandMsg(
            slave_id="s1", action="MODIFY", master_ticket=1, slave_ticket=777,
            sl=1.09400, tp=1.10600, master_open_price=1.10000, side=BUY))
        ack = recv_msg(parent)
        assert isinstance(ack, AckMsg) and ack.ok and ack.action == "MODIFY"
        _status_after = recv_msg(parent)  # loop sends a StatusMsg after each ack
        pos = mt.position_by_ticket(777)
        assert pos.sl == 1.09400 and pos.tp == 1.10600  # raw, not normalized
    finally:
        parent.close()  # -> worker reads EOFError -> graceful return
        t.join(timeout=2.0)
        assert not t.is_alive(), "slave loop must exit when the pipe closes"