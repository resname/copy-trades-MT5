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
    (re)start. Skips master tickets already present (no overwrite). Returns the
    number of records added."""
    added = 0
    for r in records:
        if table.has(r.master_ticket):
            continue
        table.add(r)
        added += 1
    return added