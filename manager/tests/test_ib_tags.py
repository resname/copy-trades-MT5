# manager/tests/test_ib_tags.py
from manager.engine.models import BUY, SELL
from manager.worker.ib.tags import synthetic_ticket, tag_master_ticket


def test_synthetic_ticket_stable_and_positive():
    a = synthetic_ticket("YM", BUY)
    assert a == synthetic_ticket("YM", BUY)  # deterministic across processes
    # ticket space is [2e9, 3e9) — above every real MT5 ticket, no 2**31 cap
    assert 2_000_000_000 <= a < 3_000_000_000


def test_synthetic_ticket_differs_by_side_and_symbol():
    assert synthetic_ticket("YM", BUY) != synthetic_ticket("YM", SELL)
    assert synthetic_ticket("YM", BUY) != synthetic_ticket("ES", BUY)


def test_tag_master_ticket_decodes_engine_comment():
    from manager.engine.linkage import encode_comment
    tag = encode_comment(12345, 0.28, 2.0)
    assert tag_master_ticket(tag) == 12345
    assert tag_master_ticket("not a tag") is None