from __future__ import annotations

from manager.engine.models import Record


class RecordTable:
    """The master->slave linkage state for one slave.

    Keyed by master_ticket (the EA keyed by magic and searched linearly;
    keying by master_ticket avoids the ticket % 900000 collision edge case).
    """

    def __init__(self) -> None:
        self._records: dict[int, Record] = {}

    def has(self, master_ticket: int) -> bool:
        return master_ticket in self._records

    def get(self, master_ticket: int) -> Record | None:
        return self._records.get(master_ticket)

    def add(self, record: Record) -> None:
        self._records[record.master_ticket] = record

    def remove(self, master_ticket: int) -> None:
        self._records.pop(master_ticket, None)

    def all(self) -> list[Record]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)
