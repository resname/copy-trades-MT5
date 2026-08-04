from __future__ import annotations

import re

# Verbatim from CopierConfig.mqh. Slave magic = base + (master_ticket % mod).
MAGIC_BASE = 1000000
MAGIC_MOD = 900000

# Comment format from SlaveSubscriber.mqh: CPY#<ticket>|MV<vol 8dp>|SV<vol 8dp>.
_PREFIX = "CPY#"
_TICKET_RE = re.compile(r"CPY#(\d+)")
_MV_RE = re.compile(r"\|MV([0-9.]+)")
_SV_RE = re.compile(r"\|SV([0-9.]+)")


def magic_for(master_ticket: int) -> int:
    return MAGIC_BASE + (master_ticket % MAGIC_MOD)


def _fmt_volume(v: float) -> str:
    """Format a volume for the order comment, stripping trailing zeros so the
    comment fits MT5's 31-char limit while remaining decodable by
    decode_comment's ``[0-9.]+`` regex. Lot sizes come from calculate_lots
    (rounded to volume_step) so stripping zeros is lossless."""
    return f"{v:.8f}".rstrip("0").rstrip(".")


def encode_comment(master_ticket: int, master_volume: float, slave_volume: float) -> str:
    return f"CPY#{master_ticket}|MV{_fmt_volume(master_volume)}|SV{_fmt_volume(slave_volume)}"


def decode_comment(comment: str) -> tuple[int, float | None, float | None] | None:
    """Parse a copied-position comment.

    Returns (master_ticket, master_volume, slave_volume) where volumes are
    None when absent or not positive. Returns None if there is no CPY# prefix
    or no ticket digits. Mirrors SlaveSubscriber.mqh's comment parser.
    """
    if not comment or _PREFIX not in comment:
        return None
    ticket_match = _TICKET_RE.search(comment)
    if not ticket_match:
        return None
    master_ticket = int(ticket_match.group(1))

    mv_match = _MV_RE.search(comment)
    sv_match = _SV_RE.search(comment)
    master_volume: float | None = None
    slave_volume: float | None = None
    if mv_match and sv_match:
        mv = float(mv_match.group(1))
        sv = float(sv_match.group(1))
        if mv > 0.0 and sv > 0.0:
            master_volume = mv
            slave_volume = sv
    return (master_ticket, master_volume, slave_volume)
