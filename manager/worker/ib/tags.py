# manager/worker/ib/tags.py
from __future__ import annotations

import hashlib

from manager.engine.linkage import decode_comment


def synthetic_ticket(symbol: str, side: int) -> int:
    """Stable positive id for an IB net position (contract + side). IB has no
    position tickets; the engine's RecordTable needs an int slave_ticket."""
    digest = hashlib.blake2b(f"pos|{symbol}|{side}".encode(),
                             digest_size=8).digest()
    return 2_000_000_000 + int.from_bytes(digest, "big") % 1_000_000_000


def tag_master_ticket(tag: str) -> int | None:
    decoded = decode_comment(tag)
    if decoded is None:
        return None
    return decoded[0]