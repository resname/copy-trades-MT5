import pytest

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


def test_seed_from_recovery_merges_split_legs():
    magic = 1077
    table = RecordTable()
    legs = [Record(1, magic, 100, 1.0, 0.5), Record(1, magic, 101, 1.0, 0.5),
            Record(2, 1078, 102, 0.5, 0.3)]
    added = seed_from_recovery(table, legs)
    assert added == 2
    rec = table.get(1)
    assert rec is not None
    assert rec.slave_ticket == 100            # first leg's ticket is the record's
    assert rec.slave_open_volume == pytest.approx(1.0)   # legs summed
    assert rec.master_open_volume == pytest.approx(1.0)
    assert table.get(2).slave_open_volume == pytest.approx(0.3)

def test_seed_from_recovery_reapply_without_reset_is_noop():
    # second RecoveryMsg without a table reset (no reset_slave in between):
    # must not double-count or overwrite (pinned behavior preserved)
    table = RecordTable()
    legs = [Record(1, 1077, 100, 1.0, 0.5), Record(1, 1077, 101, 1.0, 0.5)]
    assert seed_from_recovery(table, legs) == 1
    assert seed_from_recovery(table, legs) == 0
    assert table.get(1).slave_open_volume == pytest.approx(1.0)
