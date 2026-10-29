from __future__ import annotations

from collections.abc import Iterable

from manager.engine.models import Record
from manager.engine.record_table import RecordTable


def is_too_old(open_time: int, now: int, max_age_minutes: int) -> bool:
    """True if open_time is older than max_age_minutes relative to now.
    Ported from SlaveSubscriber::IsTooOld. An open_time of 0 is never too old."""
    if open_time == 0:
        return False
    return (now - open_time) > max_age_minutes * 60


def seed_from_recovery(table: RecordTable, records: Iterable[Record]) -> int:
    """Populate a RecordTable from records recovered by a slave worker on
    (re)start. Records are grouped by master ticket first: a split order's
    legs (N records, one per slave leg) merge into ONE Record — first leg's
    slave_ticket, summed slave_open_volume. A record whose slave_ticket was
    already merged into that master record (repeated ticket in one batch, or
    the identical record twice) is skipped, so a within-batch duplicate
    ticket cannot double-sum its leg's volume. MV/magic are identical across
    legs (the first record's wins). Master tickets already present in the
    table are skipped: production always calls reset_slave before re-seeding,
    so a pre-existing ticket means the same batch is being re-applied and
    must not double-count. Returns the number of master tickets added."""
    merged: dict[int, Record] = {}
    seen_tickets: set[tuple[int, int]] = set()  # (master_ticket, slave_ticket)
    for r in records:
        if (r.master_ticket, r.slave_ticket) in seen_tickets:
            continue  # within-batch duplicate ticket: already merged
        seen_tickets.add((r.master_ticket, r.slave_ticket))
        m = merged.get(r.master_ticket)
        if m is None:
            merged[r.master_ticket] = Record(
                master_ticket=r.master_ticket, magic=r.magic,
                slave_ticket=r.slave_ticket,
                master_open_volume=r.master_open_volume,
                slave_open_volume=r.slave_open_volume)
        else:
            m.slave_open_volume += r.slave_open_volume
    added = 0
    for rec in merged.values():
        if table.has(rec.master_ticket):
            continue
        table.add(rec)
        added += 1
    return added
