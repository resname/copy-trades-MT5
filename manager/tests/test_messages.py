import pytest

from manager.engine.models import Position, Record, SymbolInfo, BUY, SELL
from manager.ipc import messages as M


def _pos(ticket=1, side=BUY):
    return Position(ticket=ticket, symbol="EURUSD", side=side, open_price=1.10,
                    volume=0.5, sl=1.09, tp=1.11, open_time=1700000000,
                    point=0.00001, comment="CPY#1|MV0.5|SV0.10")


def _si():
    return SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                     volume_step=0.01, volume_min=0.01, volume_max=100.0)


def test_snapshot_round_trip():
    msg = M.SnapshotMsg(source_id="master", timestamp=1700000000,
                        heartbeat=3, positions=(_pos(11), _pos(22, SELL)))
    rt = M.decode(M.encode(msg))
    assert isinstance(rt, M.SnapshotMsg)
    assert rt.source_id == "master"
    assert rt.timestamp == 1700000000
    assert rt.heartbeat == 3
    assert len(rt.positions) == 2
    assert isinstance(rt.positions[0], Position)
    assert rt.positions[0].ticket == 11 and rt.positions[1].side == SELL
    assert rt.positions[0].comment == "CPY#1|MV0.5|SV0.10"


def test_command_open_round_trip():
    msg = M.CommandMsg(slave_id="s1", action="OPEN", master_ticket=42,
                      symbol="EURUSD", volume=0.10, sl=1.095, tp=1.205,
                      master_open_price=1.10, side=BUY, magic=1000042,
                      comment="CPY#42|MV0.5|SV0.10")
    rt = M.decode(M.encode(msg))
    assert isinstance(rt, M.CommandMsg)
    assert rt.action == "OPEN" and rt.master_ticket == 42 and rt.volume == 0.10
    assert rt.slave_ticket == 0  # unused for OPEN


def test_command_partial_close_round_trip():
    msg = M.CommandMsg(slave_id="s1", action="PARTIAL_CLOSE", master_ticket=42,
                      slave_ticket=777, new_master_volume=0.30,
                      master_open_volume=0.50, slave_open_volume=0.10)
    rt = M.decode(M.encode(msg))
    assert rt.action == "PARTIAL_CLOSE" and rt.slave_ticket == 777
    assert rt.new_master_volume == 0.30 and rt.master_open_volume == 0.50


def test_ack_round_trip():
    msg = M.AckMsg(slave_id="s1", action="OPEN", master_ticket=42, ok=True,
                  slave_ticket=777, fill_price=1.10005, fill_volume=0.10,
                  remaining_volume=0.10, retcode=10009)
    rt = M.decode(M.encode(msg))
    assert rt.ok is True and rt.slave_ticket == 777 and rt.fill_price == 1.10005
    assert rt.retcode == 10009


def test_status_symbolinfo_recovery_round_trip():
    st = M.StatusMsg(source_id="s1", role="slave", connected=True, login=123,
                    balance=1000.0, equity=1000.0, currency="USD", server="Demo")
    assert M.decode(M.encode(st)).balance == 1000.0

    si = M.SymbolInfoMsg(source_id="s1", infos={"EURUSD": _si()})
    rt = M.decode(M.encode(si))
    assert isinstance(rt.infos["EURUSD"], SymbolInfo)
    assert rt.infos["EURUSD"].volume_step == 0.01

    rec = M.RecoveryMsg(source_id="s1",
                        records=(Record(42, 1000042, 777, 0.50, 0.10),))
    rt = M.decode(M.encode(rec))
    assert isinstance(rt.records[0], Record)
    assert rt.records[0].slave_ticket == 777


def test_start_and_error_round_trip():
    st = M.StartMsg(config={"terminal_path": "C:/t/terminal64.exe"})
    rt = M.decode(M.encode(st))
    assert rt.config["terminal_path"] == "C:/t/terminal64.exe"
    # password field is gone
    assert not hasattr(rt, "password")

    err = M.ErrorMsg(source_id="s1", message="boom", fatal=True)
    assert M.decode(M.encode(err)).fatal is True


def test_decode_unknown_kind_raises():
    with pytest.raises(ValueError):
        M.decode({"_kind": "nope"})


def test_decode_missing_kind_raises():
    with pytest.raises(KeyError):
        M.decode({"source_id": "x"})