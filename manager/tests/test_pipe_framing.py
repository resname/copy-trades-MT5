import pytest

from manager.engine.models import Position, BUY
from manager.ipc.messages import SnapshotMsg, CommandMsg
from manager.ipc.pipe_framing import send_msg, recv_msg


def _make_pair():
    class End:
        def __init__(self, inbox, outbox):
            self._inbox = inbox
            self._outbox = outbox
        def send_bytes(self, b):
            self._outbox.append(b)
        def recv_bytes(self):
            if not self._inbox:
                raise EOFError
            return self._inbox.pop(0)
    a_box, b_box = [], []
    return End(b_box, a_box), End(a_box, b_box)


def test_send_recv_round_trip():
    a, b = _make_pair()
    msg = SnapshotMsg(source_id="master", timestamp=1, heartbeat=1,
                     positions=(Position(1, "EURUSD", BUY, 1.1, 0.5, 0, 0, 0, 0.00001),))
    send_msg(a, msg)
    got = recv_msg(b)
    assert isinstance(got, SnapshotMsg)
    assert got.positions[0].ticket == 1


def test_multiple_messages_in_order():
    a, b = _make_pair()
    send_msg(a, CommandMsg(slave_id="s", action="OPEN", master_ticket=1))
    send_msg(a, CommandMsg(slave_id="s", action="CLOSE", master_ticket=1, slave_ticket=2))
    first = recv_msg(b)
    second = recv_msg(b)
    assert first.action == "OPEN" and second.action == "CLOSE"


def test_recv_eof_raises():
    a, b = _make_pair()
    with pytest.raises(EOFError):
        recv_msg(b)  # nothing was ever sent