from manager.engine.models import Record
from manager.engine.record_table import RecordTable
from manager.engine.baseline import is_too_old, seed_from_recovery

NOW = 1700000000


def test_is_too_old_true_beyond_max_age():
    # 30 minutes old, max 10 minutes
    assert is_too_old(NOW - 30 * 60, NOW, 10) is True


def test_is_too_old_false_within_max_age():
    # 5 minutes old, max 10 minutes
    assert is_too_old(NOW - 5 * 60, NOW, 10) is False


def test_is_too_old_boundary_strictly_greater():
    # exactly max_age old -> not too old (EA uses strict >)
    assert is_too_old(NOW - 10 * 60, NOW, 10) is False


def test_is_too_old_zero_open_time_never_old():
    assert is_too_old(0, NOW, 10) is False


def test_seed_from_recovery_populates_table():
    table = RecordTable()
    rec = Record(master_ticket=1, magic=1000001, slave_ticket=99,
                 master_open_volume=0.5, slave_open_volume=0.05)
    added = seed_from_recovery(table, [rec])
    assert added == 1
    assert table.has(1)
    assert table.get(1).slave_ticket == 99


def test_seed_from_recovery_skips_duplicates():
    table = RecordTable()
    rec = Record(master_ticket=1, magic=1000001, slave_ticket=99,
                 master_open_volume=0.5, slave_open_volume=0.05)
    seed_from_recovery(table, [rec])
    added = seed_from_recovery(table, [rec])
    assert added == 0
    assert len(table) == 1
