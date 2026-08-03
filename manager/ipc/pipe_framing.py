from __future__ import annotations

import json

from manager.ipc.messages import encode, decode


def send_msg(conn, msg) -> None:
    """Serialize `msg` to JSON bytes and send it over the pipe. The underlying
    Connection (multiprocessing.connection.Connection.send_bytes) adds its own
    length framing to the blob, so message boundaries are preserved."""
    payload = json.dumps(encode(msg)).encode("utf-8")
    conn.send_bytes(payload)


def recv_msg(conn):
    """Block until one framed message arrives; decode it. Raises EOFError when
    the peer has closed the pipe (the manager-death signal workers rely on)."""
    payload = conn.recv_bytes()
    return decode(json.loads(payload.decode("utf-8")))