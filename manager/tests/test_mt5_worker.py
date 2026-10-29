import pytest

from manager.engine.models import Position, Record, SymbolInfo, BUY, SELL
from manager.engine.linkage import magic_for, encode_comment, MAGIC_BASE
from manager.ipc.messages import CommandMsg, SnapshotMsg, RecoveryMsg, SymbolInfoMsg, StatusMsg
from manager.worker.mt5_adapter import FakeMt5
from manager.worker.mt5_constants import (
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN, select_filling_mode,
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, ORDER_TYPE_BUY, ORDER_TYPE_SELL,
    ORDER_TIME_GTC, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL,
    TRADE_RETCODE_PLACED,
)
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


def test_status_carries_trade_allowed_from_adapter():
    from manager.worker.mt5_worker import _status
    from manager.worker.mt5_adapter import FakeMt5
    acc = {"login": 1, "balance": 0.0, "equity": 0.0, "currency": "USD", "server": "Demo"}
    assert _status(FakeMt5(account=acc), "s1", "slave", True).trade_allowed is True
    assert _status(FakeMt5(account=acc, terminal_info={"trade_allowed": False}),
                   "s1", "slave", True).trade_allowed is False


# ---- trade-request constants match the MetaTrader5 package ----
# The worker builds request dicts with these hardcoded constants and NEVER
# imports MetaTrader5 for them, so the values must equal the package's. If they
# drift, the server rejects the request with a confusing retcode: a wrong
# TRADE_ACTION_SLTP (3, the standard-MQL5 PENDING_SLTP value, instead of the
# Python package's 6) made MODIFY/SLTP fail with retcode 10030/10014 while
# OPEN/CLOSE/PARTIAL (TRADE_ACTION_DEAL=1) kept working -- the 'OPEN copies but
# MODIFY does not' bug. FakeMt5 checks `action == TRADE_ACTION_SLTP` against
# the SAME constant, so the fake-based tests below are tautological and never
# caught it; this test pins the values to the real package.

def test_trade_constants_match_metatrader5_package():
    mt5 = pytest.importorskip("MetaTrader5")
    assert TRADE_ACTION_DEAL == mt5.TRADE_ACTION_DEAL
    assert TRADE_ACTION_SLTP == mt5.TRADE_ACTION_SLTP
    assert ORDER_TYPE_BUY == mt5.ORDER_TYPE_BUY
    assert ORDER_TYPE_SELL == mt5.ORDER_TYPE_SELL
    assert ORDER_TIME_GTC == mt5.ORDER_TIME_GTC
    assert ORDER_FILLING_FOK == mt5.ORDER_FILLING_FOK
    assert ORDER_FILLING_IOC == mt5.ORDER_FILLING_IOC
    assert ORDER_FILLING_RETURN == mt5.ORDER_FILLING_RETURN
    assert TRADE_RETCODE_DONE == mt5.TRADE_RETCODE_DONE
    assert TRADE_RETCODE_PLACED == mt5.TRADE_RETCODE_PLACED
    assert TRADE_RETCODE_DONE_PARTIAL == mt5.TRADE_RETCODE_DONE_PARTIAL


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


def test_build_symbol_info_reply_names_requested_and_confirms_missing():
    # regex rows like '(.+)\\.m=$1' derive slave names at trade time; the
    # reply names every requested symbol so the engine can settle its inflight
    # request (absent symbols are confirmed missing on the terminal)
    from manager.worker.mt5_worker import build_symbol_info_reply
    msg = build_symbol_info_reply(_adapter(), "s1", ["EURUSD", "WS30"])
    assert isinstance(msg, SymbolInfoMsg)
    assert set(msg.infos.keys()) == {"EURUSD"}  # WS30 not on this terminal
    assert msg.requested == ["EURUSD", "WS30"]


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


def test_execute_partial_close_float_epsilon_keeps_full_step():
    # delta 0.58 - 0.29 sits epsilon below 29 steps (0.29/0.01 =
    # 28.999999999999996 in IEEE-754); a naive floor closes 0.28 and leaves
    # the slave 0.01 overweight -- same float bug class as the lot-sizer
    cmt = encode_comment(1, 1.0, 0.58)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.58, 0, 0, 0,
                                      0.00001, comment=cmt)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.5,
                     master_open_volume=1.0, slave_open_volume=0.58)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert ack.remaining_volume == pytest.approx(0.29, abs=1e-8)


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


def test_slave_loop_symbol_info_request_replies_with_requested():
    """On SymbolInfoRequestMsg the slave loop replies with a SymbolInfoMsg
    naming the requested symbols (absent ones confirmed missing) — the reply
    that releases the engine's held NEW for regex-derived slave symbols."""
    import multiprocessing
    import threading
    from manager.ipc.messages import (
        RecoveryMsg, SymbolInfoMsg, StatusMsg, SymbolInfoRequestMsg,
    )
    from manager.ipc.pipe_framing import send_msg, recv_msg
    from manager.worker.mt5_worker import _slave_loop

    mt = FakeMt5(
        symbol_infos={"EURUSD": SI, "GBPUSD": SI},
        account={"login": 2, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
    )
    cfg = {"slave_id": "s1", "symbol_map_csv": "(.+)\\.m=$1",
           "normalize_sltp": True, "retry_count": 1, "retry_delay_ms": 0,
           "slave_status_interval_ms": 60000}

    parent, child = multiprocessing.Pipe(duplex=True)
    t = threading.Thread(target=_slave_loop, args=(child, mt, cfg), daemon=True)
    t.start()

    def _recv(timeout=5.0):
        assert parent.poll(timeout), "worker did not reply in time"
        return recv_msg(parent)

    try:
        # drain init: RecoveryMsg, SymbolInfoMsg (empty: map value '$1' is a
        # template, not a symbol), StatusMsg
        init = [_recv() for _ in range(3)]
        assert isinstance(init[0], RecoveryMsg)
        assert isinstance(init[1], SymbolInfoMsg) and init[1].infos == {}
        assert isinstance(init[2], StatusMsg)

        send_msg(parent, SymbolInfoRequestMsg(source_id="s1",
                                               symbols=["EURUSD", "WS30"]))
        reply = _recv()
        assert isinstance(reply, SymbolInfoMsg)
        assert set(reply.infos.keys()) == {"EURUSD"}
        assert reply.requested == ["EURUSD", "WS30"]
    finally:
        parent.close()
        t.join(timeout=2.0)
        assert not t.is_alive(), "slave loop must exit when the pipe closes"


# ---- filling-mode selection (was hardcoded ORDER_FILLING_RETURN -> 10030 on
# symbols like WS30 that only allow FOK/IOC) ----

def test_select_filling_mode_picks_supported_mode():
    assert select_filling_mode(0b001) == ORDER_FILLING_FOK      # FOK only
    assert select_filling_mode(0b010) == ORDER_FILLING_IOC      # IOC only
    assert select_filling_mode(0b011) == ORDER_FILLING_FOK      # FOK+IOC -> FOK
    assert select_filling_mode(0) == ORDER_FILLING_RETURN       # none -> RETURN


def _adapter_filling(filling_mode):
    si = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                    volume_step=0.01, volume_min=0.01, volume_max=100.0,
                    filling_mode=filling_mode)
    return FakeMt5(
        positions=[],
        symbol_infos={"EURUSD": si},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks={"EURUSD": (1.10000, 1.10010)},
    )


def test_execute_open_uses_symbol_supported_filling_mode():
    for fm, expected in [(0b011, ORDER_FILLING_FOK), (0b010, ORDER_FILLING_IOC),
                         (0, ORDER_FILLING_RETURN)]:
        mt = _adapter_filling(fm)
        cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42,
                         symbol="EURUSD", volume=0.10, sl=0.0, tp=0.0,
                         master_open_price=1.10, side=BUY, magic=magic_for(42),
                         comment=encode_comment(42, 0.50, 0.10))
        ack = execute_command(mt, cmd, normalize_sltp=False, retry_count=1,
                              retry_delay_ms=0)
        assert ack.ok, f"filling_mode={fm}: ack not ok: {ack.error}"
        assert mt.last_request["type_filling"] == expected, \
            f"filling_mode={fm}: expected {expected}, got {mt.last_request['type_filling']}"


def test_execute_close_uses_symbol_supported_filling_mode():
    cmt = encode_comment(1, 0.50, 0.10)
    mt = _adapter_filling(0b001)  # FOK only
    mt.positions = [Position(777, "EURUSD", BUY, 1.10010, 0.10, 0, 0, 0,
                              0.00001, comment=cmt)]
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=777)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.last_request["type_filling"] == ORDER_FILLING_FOK


# ---- execute_command: OPEN split (oversized volumes) ----
from dataclasses import replace
from manager.ipc.messages import CommandMsg

SI_MAX05 = replace(SI, volume_max=0.5)   # same step/min (0.01), tiny per-order max

def _split_adapter(**kw):
    return FakeMt5(
        symbol_infos={"EURUSD": SI_MAX05},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo"},
        ticks={"EURUSD": (1.10000, 1.10010)},
        **kw)

def _open_cmd(volume, comment=None):
    return CommandMsg(slave_id="s1", action="OPEN", master_ticket=42,
                      symbol="EURUSD", volume=volume, sl=0.0, tp=0.0,
                      master_open_price=1.10000, side=BUY, magic=magic_for(42),
                      comment=comment if comment is not None
                      else encode_comment(42, 2.0, volume))

def test_execute_open_splits_into_max_chunks_with_per_part_comments():
    mt = _split_adapter()
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert ack.fill_volume == pytest.approx(1.2, abs=1e-9)
    parts = [p for p in mt.positions_get() if p.comment.startswith("CPY#")]
    assert [p.volume for p in parts] == [pytest.approx(0.5), pytest.approx(0.5),
                                         pytest.approx(0.2)]
    for p in parts:  # each leg its own SV, shared master ticket + magic + SL/TP
        assert p.comment in ("CPY#42|MV2|SV0.5", "CPY#42|MV2|SV0.2")
        assert p.magic == magic_for(42)
    assert ack.slave_ticket == parts[0].ticket

def test_execute_open_single_order_unchanged_when_under_symbol_max():
    mt = _split_adapter()
    comment = encode_comment(42, 0.5, 0.1)
    ack = execute_command(mt, _open_cmd(0.1, comment), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok and len([p for p in mt.positions_get()
                           if p.comment.startswith("CPY#")]) == 1
    assert mt.positions_get()[-1].comment == comment  # byte-identical legacy path

def test_execute_open_split_partial_failure_keeps_filled_parts():
    from manager.worker.mt5_constants import TRADE_RETCODE_DONE
    mt = _split_adapter(order_results=[
        {"retcode": TRADE_RETCODE_DONE, "order": 777, "deal": 777,
         "price": 1.10010, "volume": 0.5},
        {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0},
    ])
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok and ack.fill_volume == pytest.approx(0.5)
    assert ack.slave_ticket == 777
    assert "shortfall" in (ack.error or "")
    assert "split" in (ack.error or "")
    # the ack prices the FIRST successful chunk, not the last attempted one
    assert ack.fill_price == pytest.approx(1.10010)


def test_execute_open_split_partial_fill_counts_actual_volume():
    # retcode 10010 (DONE_PARTIAL) counts as a filled chunk with its ACTUAL
    # volume (the request was 0.5, the fill 0.25); the clean "2/2 ok" note
    # gains a shortfall clause
    mt = _split_adapter(order_results=[
        {"retcode": TRADE_RETCODE_DONE_PARTIAL, "order": 771, "deal": 771,
         "price": 1.10010, "volume": 0.25},
        {"retcode": TRADE_RETCODE_DONE, "order": 772, "deal": 772,
         "price": 1.10010, "volume": 0.5},
    ])
    ack = execute_command(mt, _open_cmd(1.0), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert ack.fill_volume == pytest.approx(0.75)
    assert ack.fill_price == pytest.approx(1.10010)  # first successful chunk
    assert "split: 2/2 ok" in (ack.error or "")
    assert "shortfall: requested 1.0, filled 0.75" in (ack.error or "")

def test_execute_open_split_total_failure_fails_like_today():
    mt = _split_adapter(order_results=[
        {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0},
        {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0},
    ])
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == 10004

def test_execute_open_split_undecodable_comment_fails():
    mt = _split_adapter()
    ack = execute_command(mt, _open_cmd(1.2, comment="unrelated"),
                          normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and "split" in ack.error and "undecodable" in ack.error

def test_execute_open_split_heartbeats_between_chunks():
    mt = _split_adapter()
    beats = []
    ack = execute_command(mt, _open_cmd(1.2), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0,
                          heartbeat=lambda: beats.append(1))
    assert ack.ok and len(beats) == 2  # 3 chunks -> 2 inter-chunk heartbeats

def test_execute_open_split_zero_parts_requested_volume_fully_single():
    # volume exactly == volume_max -> single order, legacy path (no split)
    mt = _split_adapter()
    comment = encode_comment(42, 0.5, 0.5)
    ack = execute_command(mt, _open_cmd(0.5, comment), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok and len([p for p in mt.positions_get()
                           if p.comment.startswith("CPY#")]) == 1


def test_execute_open_split_refetches_tick_and_renormalizes_per_chunk():
    # a mid-split tick move must be reflected in each chunk's price + SL/TP
    # (all chunks were previously sent off the pre-loop tick/SL/TP)
    class MovingTickFake(FakeMt5):
        def symbol_info_tick(self, symbol):
            # drifts UP on each call, so each chunk must see a fresh price
            base = self.ticks.get(symbol)
            if base is None:
                return None
            bid, ask = base
            self.ticks[symbol] = (bid, round(ask + 0.00010, 5))
            return (bid, round(ask, 5))
    mt = MovingTickFake(symbol_infos={"EURUSD": SI_MAX05},
                        account={"login": 123, "balance": 1000.0,
                                 "equity": 1000.0, "currency": "USD",
                                 "server": "Demo"},
                        ticks={"EURUSD": (1.10000, 1.10010)})
    cmd = CommandMsg(slave_id="s1", action="OPEN", master_ticket=42,
                     symbol="EURUSD", volume=1.0, sl=1.09500, tp=0.0,
                     master_open_price=1.10000, side=BUY, magic=magic_for(42),
                     comment=encode_comment(42, 1.0, 1.0))
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1,
                          retry_delay_ms=0)
    assert ack.ok
    parts = [p for p in mt.positions_get() if p.comment.startswith("CPY#")]
    assert [p.volume for p in parts] == [pytest.approx(0.5), pytest.approx(0.5)]
    # every chunk priced at its OWN fresh tick (each fetch drifts +0.00010):
    # chunk 0 at 1.10020, chunk 1 at 1.10030 (old code sent both off the
    # pre-loop tick 1.10010)
    assert parts[0].open_price == pytest.approx(1.10020, abs=1e-8)
    assert parts[1].open_price == pytest.approx(1.10030, abs=1e-8)
    # each chunk's SL re-normalized off its OWN fill price (distance 0.00500)
    assert parts[0].sl == pytest.approx(1.09520, abs=1e-8)
    assert parts[1].sl == pytest.approx(1.09530, abs=1e-8)


def test_execute_open_split_mid_tick_gone_fails_but_keeps_filled_parts():
    class _TickVanishesAfterTwo(FakeMt5):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._tick_calls = 0
        def symbol_info_tick(self, symbol):
            self._tick_calls += 1
            if self._tick_calls > 2:  # pre-loop + chunk 0 OK, then gone
                return None
            return self.ticks.get(symbol)
    mt = _TickVanishesAfterTwo(symbol_infos={"EURUSD": SI_MAX05},
                               account={"login": 123, "balance": 1000.0,
                                        "equity": 1000.0, "currency": "USD",
                                        "server": "Demo"},
                               ticks={"EURUSD": (1.10000, 1.10010)})
    ack = execute_command(mt, _open_cmd(1.0), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == -1
    assert "no tick" in ack.error
    # parts already filled stay
    parts = [p for p in mt.positions_get() if p.comment.startswith("CPY#")]
    assert [p.volume for p in parts] == [pytest.approx(0.5)]


# ---- netting/unknown margin accounts must NOT split (clamp instead) ----

def _netting_adapter(margin_mode):
    return FakeMt5(
        symbol_infos={"EURUSD": SI_MAX05},
        account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                 "currency": "USD", "server": "Demo", "margin_mode": margin_mode},
        ticks={"EURUSD": (1.10000, 1.10010)})

def test_execute_open_netting_clamps_instead_of_splitting():
    # netting account (margin_mode 0): the terminal merges same-symbol orders
    # into ONE position and the position comment becomes the LAST deal's, so
    # per-leg SV linkage would be corrupted -> clamp, never split
    mt = _netting_adapter(0)
    comment = encode_comment(42, 2.0, 1.2)
    ack = execute_command(mt, _open_cmd(1.2, comment), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok
    cpy = [p for p in mt.positions_get() if p.comment.startswith("CPY#")]
    assert len(cpy) == 1  # exactly ONE order, no split loop
    assert cpy[0].volume == pytest.approx(0.5)  # clamped to volume_max
    assert cpy[0].comment == comment  # cmd.comment used AS-IS (no re-encode)
    assert ack.fill_volume == pytest.approx(0.5)


def test_execute_open_missing_margin_mode_key_clamps_instead_of_splitting():
    # unknown/missing margin_mode must also clamp (never split): FakeMt5 with
    # account={"margin_mode": None}
    mt = _netting_adapter(None)
    comment = encode_comment(42, 2.0, 1.2)
    ack = execute_command(mt, _open_cmd(1.2, comment), normalize_sltp=True,
                          retry_count=1, retry_delay_ms=0)
    assert ack.ok
    cpy = [p for p in mt.positions_get() if p.comment.startswith("CPY#")]
    assert len(cpy) == 1
    assert cpy[0].volume == pytest.approx(0.5)
    assert cpy[0].comment == comment
    assert ack.fill_volume == pytest.approx(0.5)


# ---- execute_command: fan-out over split legs ----

def _leg(ticket, volume, master=1, open_price=1.10010):
    return Position(ticket, "EURUSD", BUY, open_price, volume, 0, 0, 0,
                    0.00001, comment=encode_comment(master, 1.0, volume))

def test_execute_modify_fans_out_over_split_legs_at_own_open_prices():
    mt = _adapter(positions=[_leg(777, 0.6, open_price=1.10010),
                             _leg(778, 0.4, open_price=1.10005)])
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=777, sl=1.09400, tp=1.10600,
                     master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    # SL distance 0.00600 from each leg's own open price
    assert mt.position_by_ticket(777).sl == pytest.approx(1.09410, abs=1e-8)
    assert mt.position_by_ticket(778).sl == pytest.approx(1.09405, abs=1e-8)

def test_execute_modify_falls_back_to_ticket_when_comment_mangled():
    # broker rewrote the comment: CPY decode finds nothing -> legacy
    # per-ticket routing must still reach the leg (Review Focus #1)
    mt = _adapter(positions=[Position(777, "EURUSD", BUY, 1.10010, 0.6, 0, 0, 0,
                                      0.00001, comment="broker-rewritten")])
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=777, sl=1.09400, tp=0.0,
                     master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok and mt.position_by_ticket(777).sl == pytest.approx(1.09410, abs=1e-8)

def test_execute_modify_no_legs_fails_like_today():
    mt = _adapter()
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=777, sl=1.09, tp=0.0,
                     master_open_price=1.1, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and "gone" in ack.error

def test_execute_partial_close_pro_rata_each_leg():
    # master 1.0 split as 0.6 + 0.4; master halves (fraction 0.5) -> each leg
    # keeps its OWN fraction: targets 0.3 / 0.2 (Review Focus #5)
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.5,
                     master_open_volume=1.0, slave_open_volume=1.0)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777).volume == pytest.approx(0.3, abs=1e-8)
    assert mt.position_by_ticket(778).volume == pytest.approx(0.2, abs=1e-8)
    assert ack.remaining_volume == pytest.approx(0.5, abs=1e-8)

def test_execute_partial_close_pro_rata_full_chunk_close():
    # master 1.0 split 0.6 + 0.4; master closes to 0.55 -> fraction 0.55:
    # leg1 target 0.33 (close 0.27), leg2 target 0.22 (close 0.18)
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)])
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.55,
                     master_open_volume=1.0, slave_open_volume=1.0)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777).volume == pytest.approx(0.33, abs=1e-8)
    assert mt.position_by_ticket(778).volume == pytest.approx(0.22, abs=1e-8)

def test_execute_partial_close_skips_already_at_target_leg_and_closes_only_deficit_leg():
    # mixed state: leg 777 already AT its target (closed manually down from its
    # original SV 0.6 to 0.3; a manual close never rewrites the comment, so the
    # comment keeps the ORIGINAL SV) and leg 778 exact; fraction 0.5 ->
    # 777 target 0.6*0.5 = 0.3 (nothing to close), 778 target 0.2 (close 0.2)
    legs = [_leg(777, 0.6), _leg(778, 0.4)]
    legs[0] = replace(legs[0], volume=0.3)  # live volume shrunk, comment SV0.6 stays
    mt = _adapter(positions=legs)
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=777, new_master_volume=0.5,
                     master_open_volume=1.0, slave_open_volume=0.6)
    # NOTE cmd.slave_open_volume unused per leg: each leg's own SV comment governs
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777).volume == pytest.approx(0.3, abs=1e-8)  # nothing to close
    assert mt.position_by_ticket(778).volume == pytest.approx(0.2, abs=1e-8)

def test_execute_close_closes_all_legs_of_split():
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)])
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=777)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777) is None and mt.position_by_ticket(778) is None

def test_execute_close_fanout_fails_ack_on_failing_leg():
    from manager.worker.mt5_constants import TRADE_RETCODE_DONE
    mt = _adapter(positions=[_leg(777, 0.6), _leg(778, 0.4)],
                  order_results=[
                      {"retcode": TRADE_RETCODE_DONE, "order": 777, "deal": 777,
                       "price": 1.1, "volume": 0.6},
                      {"retcode": 10004, "order": 0, "deal": 0, "price": 0.0,
                       "volume": 0.0}])
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=777)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == 10004


class _EffectFake(FakeMt5):
    """FakeMt5 applies canned results WITHOUT the trade effect (simulation is
    skipped when a canned result is consumed). This subclass applies the SLTP
    effect for a canned DONE SLTP result so fan-out tests can assert each
    leg's post-order state."""

    def order_send(self, request):
        res = super().order_send(request)
        if (request["action"] == TRADE_ACTION_SLTP
                and res.get("retcode") == TRADE_RETCODE_DONE):
            for i, p in enumerate(self.positions):
                if p.ticket == request["position"]:
                    self.positions[i] = replace(
                        p, sl=request.get("sl", 0.0), tp=request.get("tp", 0.0))
        return res


def test_execute_modify_continues_to_other_legs_after_a_failure():
    # a first failing leg must NOT abort the fan-out: every leg is attempted,
    # the FAILURE is reported at the end (first failure's retcode + error), so
    # legs after it don't keep stale SL/TP while the master has already moved
    mt = _EffectFake(positions=[_leg(777, 0.6), _leg(778, 0.4)],
                     symbol_infos={"EURUSD": SI, "GBPUSD": SI},
                     account={"login": 123, "balance": 1000.0, "equity": 1000.0,
                              "currency": "USD", "server": "Demo"},
                     ticks={"EURUSD": (1.10000, 1.10010)},
                     order_results=[
                         {"retcode": 10004, "order": 0, "deal": 0,
                          "price": 0.0, "volume": 0.0},
                         {"retcode": TRADE_RETCODE_DONE, "order": 778,
                          "deal": 0, "price": 0.0, "volume": 0.0}])
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=777, sl=1.09400, tp=0.0,
                     master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok is False and ack.retcode == 10004
    # BOTH legs attempted: 777's SLTP was rejected (unchanged), 778 still updated
    assert mt.position_by_ticket(777).sl == 0
    assert mt.position_by_ticket(778).sl == pytest.approx(1.09410, abs=1e-8)


# ---- _find_legs union semantics (spec decision #1): slave_ticket is a leg
# even when the comment scan is non-empty (one mangled leg must not drop out) ----

def _two_legs_one_mangled():
    return [Position(777, "EURUSD", BUY, 1.10010, 0.6, 0, 0, 0, 0.00001,
                     comment=encode_comment(1, 1.0, 0.6)),
            Position(778, "EURUSD", BUY, 1.10005, 0.4, 0, 0, 0, 0.00001,
                     comment="broker-rewritten")]

def test_execute_modify_union_includes_mangled_leg_even_when_scan_is_nonempty():
    mt = _adapter(positions=_two_legs_one_mangled())
    cmd = CommandMsg(slave_id="s1", action="MODIFY", master_ticket=1,
                     slave_ticket=778, sl=1.09400, tp=0.0,
                     master_open_price=1.10000, side=BUY)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777).sl == pytest.approx(1.09410, abs=1e-8)
    # 778's comment is mangled: reached via the slave_ticket union, normalized
    # off ITS OWN open price 1.10005
    assert mt.position_by_ticket(778).sl == pytest.approx(1.09405, abs=1e-8)

def test_execute_close_union_includes_mangled_leg_even_when_scan_is_nonempty():
    mt = _adapter(positions=_two_legs_one_mangled())
    cmd = CommandMsg(slave_id="s1", action="CLOSE", master_ticket=1, slave_ticket=778)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    assert mt.position_by_ticket(777) is None and mt.position_by_ticket(778) is None

def test_execute_partial_close_estimates_mangled_leg_sv_from_remaining_legs():
    mt = _adapter(positions=_two_legs_one_mangled())
    cmd = CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=1,
                     slave_ticket=778, new_master_volume=0.5,
                     master_open_volume=1.0, slave_open_volume=1.0)
    ack = execute_command(mt, cmd, normalize_sltp=True, retry_count=1, retry_delay_ms=0)
    assert ack.ok
    # leg 777 by its own SV: target 0.6*0.5 = 0.3, close 0.3
    assert mt.position_by_ticket(777).volume == pytest.approx(0.3, abs=1e-8)
    # leg 778 has NO decodable SV: estimated as cmd.slave_open_volume (1.0)
    # minus the other legs' decoded SVs (0.6) = 0.4 -> target 0.2 -> 778
    # closes 0.2 (the CORRECT pro-rata, not the aggregate-driven 0.5)
    assert mt.position_by_ticket(778).volume == pytest.approx(0.2, abs=1e-8)