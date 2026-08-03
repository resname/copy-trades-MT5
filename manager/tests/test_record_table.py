from manager.engine.models import Record
from manager.engine.record_table import RecordTable


def _rec(ticket=1, slave_ticket=99):
    return Record(master_ticket=ticket, magic=1000000 + ticket,
                  slave_ticket=slave_ticket, master_open_volume=0.5,
                  slave_open_volume=0.05)


def test_add_and_get():
    table = RecordTable()
    table.add(_rec(1, 99))
    assert table.has(1)
    assert table.get(1).slave_ticket == 99
    assert len(table) == 1


def test_get_missing_returns_none():
    table = RecordTable()
    assert table.get(123) is None
    assert not table.has(123)


def test_remove():
    table = RecordTable()
    table.add(_rec(1, 99))
    table.remove(1)
    assert not table.has(1)
    assert len(table) == 0


def test_remove_missing_is_noop():
    table = RecordTable()
    table.remove(999)  # must not raise
    assert len(table) == 0


def test_all_returns_every_record():
    table = RecordTable()
    table.add(_rec(1, 99))
    table.add(_rec(2, 100))
    records = table.all()
    assert {r.master_ticket for r in records} == {1, 2}
