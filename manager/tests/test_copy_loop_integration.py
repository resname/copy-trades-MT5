import threading
import time

import pytest

from manager.engine.models import Position, Snapshot, SymbolInfo, Record, BUY
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.engine.linkage import magic_for
from manager.ipc.messages import AckMsg, StatusMsg
from manager.ipc.pipe_framing import send_msg, recv_msg
from manager.worker.mt5_adapter import FakeMt5
from manager.worker.mt5_worker import execute_command

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = 1700000000


def _cfg():
    return SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                       step_amount=100.0, step_size=0.01, max_lot=10.0,
                       max_trade_age_minutes=10, normalize_sltp=True)


def _pos(ticket, volume=0.5, sl=1.09500, tp=1.10500, side=BUY):
    return Position(ticket=ticket, symbol="EURUSD", side=side, open_price=1.10000,
                    volume=volume, sl=sl, tp=tp, open_time=NOW, point=0.00001)


def _slave_adapter():
    return FakeMt5(symbol_infos={"EURUSD": SI},
                  account={"login": 2, "balance": 1000.0, "equity": 1000.0,
                           "currency": "USD", "server": "Demo"},
                  ticks={"EURUSD": (1.10000, 1.10010)})


def _slave_thread(child_pipe, adapter, stop_evt):
    while not stop_evt.is_set():
        if not child_pipe.poll(0.05):
            continue
        try:
            cmd = recv_msg(child_pipe)
        except EOFError:
            return
        ack = execute_command(adapter, cmd, normalize_sltp=True, retry_count=1,
                              retry_delay_ms=0)
        try:
            send_msg(child_pipe, ack)
        except (EOFError, OSError):
            return


def _drive(engine, parent_pipe, slave_id, positions, now=NOW):
    snap = Snapshot(timestamp=now, heartbeat=1, positions=tuple(positions))
    cmds = engine.ingest_snapshot(snap, now=now)[slave_id]
    for cmd in cmds:
        send_msg(parent_pipe, cmd)
    # drain acks (and any re-emitted held commands) until idle
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not parent_pipe.poll(0.2):
            break
        msg = recv_msg(parent_pipe)
        if isinstance(msg, AckMsg):
            for c in engine.apply_ack(slave_id, msg):
                send_msg(parent_pipe, c)


def _engine_with_slave():
    eng = CopyEngine()
    eng.add_slave(_cfg())
    eng.apply_symbol_info("s1", {"EURUSD": SI})
    eng.apply_status("s1", StatusMsg(source_id="s1", role="slave", connected=True,
                    login=2, balance=1000.0, equity=1000.0, currency="USD",
                    server="Demo"))
    return eng


def test_full_lifecycle_open_modify_partial_close():
    eng = _engine_with_slave()
    parent_pipe, child_pipe = multiprocessing_pipe()
    adapter = _slave_adapter()
    stop = threading.Event()
    t = threading.Thread(target=_slave_thread, args=(child_pipe, adapter, stop),
                        daemon=True)
    t.start()
    try:
        # 1. OPEN
        _drive(eng, parent_pipe, "s1", [_pos(42)])
        rec = eng._slaves["s1"].table.get(42)
        assert rec is not None and rec.slave_ticket != 0
        assert rec.slave_open_volume == pytest.approx(0.10)
        pos = adapter.positions_get()[-1]
        assert pos.volume == pytest.approx(0.10)
        # slave normalized SL/TP to its ask (1.10010): 1.09510 / 1.10510
        assert pos.sl == pytest.approx(1.09510, abs=1e-8)
        assert pos.tp == pytest.approx(1.10510, abs=1e-8)

        # 2. MODIFY
        _drive(eng, parent_pipe, "s1", [_pos(42, sl=1.09000, tp=1.11000)])
        pos = adapter.position_by_ticket(rec.slave_ticket)
        # normalized to fill 1.10010: sl=1.09010, tp=1.11010
        assert pos.sl == pytest.approx(1.09010, abs=1e-8)
        assert pos.tp == pytest.approx(1.11010, abs=1e-8)

        # 3. PARTIAL_CLOSE (0.5 -> 0.3): fraction 0.6, target 0.06, close 0.04
        _drive(eng, parent_pipe, "s1", [_pos(42, volume=0.30)])
        assert adapter.position_by_ticket(rec.slave_ticket).volume == pytest.approx(0.06, abs=1e-8)

        # 4. CLOSE
        _drive(eng, parent_pipe, "s1", [])
        assert adapter.position_by_ticket(rec.slave_ticket) is None
        assert eng._slaves["s1"].table.has(42) is False
    finally:
        stop.set()
        try:
            parent_pipe.close()
        except Exception:
            pass
        t.join(timeout=2.0)


def test_pending_held_modify_during_open():
    """Two snapshots before the OPEN ack: MODIFY held, re-emitted on ack."""
    eng = _engine_with_slave()
    parent_pipe, child_pipe = multiprocessing_pipe()
    adapter = _slave_adapter()
    stop = threading.Event()
    t = threading.Thread(target=_slave_thread, args=(child_pipe, adapter, stop),
                        daemon=True)
    t.start()
    try:
        # snapshot 1: NEW -> OPEN sent (pending), optimistic record added
        snap1 = Snapshot(timestamp=NOW, heartbeat=1, positions=(_pos(42),))
        cmds1 = eng.ingest_snapshot(snap1, now=NOW)["s1"]
        assert len(cmds1) == 1 and cmds1[0].action == "OPEN"
        # snapshot 2 (before ack): MODIFY -> held
        snap2 = Snapshot(timestamp=NOW, heartbeat=2,
                         positions=(_pos(42, sl=1.09000, tp=1.11000),))
        cmds2 = eng.ingest_snapshot(snap2, now=NOW)["s1"]
        assert cmds2 == []  # MODIFY held pending OPEN ack
        # send the OPEN and drain its ack -> re-emitted MODIFY must be sent+acked
        send_msg(parent_pipe, cmds1[0])
        deadline = time.time() + 2.0
        seen_modify = False
        while time.time() < deadline:
            if not parent_pipe.poll(0.2):
                break
            msg = recv_msg(parent_pipe)
            if isinstance(msg, AckMsg):
                for c in engine_apply_ack(eng, "s1", msg):
                    send_msg(parent_pipe, c)
                    if c.action == "MODIFY":
                        seen_modify = True
        # drain the MODIFY ack
        if parent_pipe.poll(0.5):
            msg = recv_msg(parent_pipe)
            if isinstance(msg, AckMsg):
                engine_apply_ack(eng, "s1", msg)
        assert seen_modify
        rec = eng._slaves["s1"].table.get(42)
        pos = adapter.position_by_ticket(rec.slave_ticket)
        assert pos.sl == pytest.approx(1.09010, abs=1e-8)
    finally:
        stop.set()
        try:
            parent_pipe.close()
        except Exception:
            pass
        t.join(timeout=2.0)


def multiprocessing_pipe():
    import multiprocessing
    return multiprocessing.Pipe(duplex=True)


def engine_apply_ack(eng, slave_id, ack):
    return eng.apply_ack(slave_id, ack)