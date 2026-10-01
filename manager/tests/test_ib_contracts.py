# manager/tests/test_ib_contracts.py
import pytest

from manager.worker.ib.contracts import ContractSpec, parse_contract_map, pick_front_month


def test_parse_contract_map_full_row():
    m = parse_contract_map({"YM": {"exchange": "CME", "sec_type": "fut",
                                    "master_point_value": "1.0"}})
    assert m["YM"] == ContractSpec(symbol="YM", exchange="CME", sec_type="FUT",
                                   master_point_value=1.0, currency="USD")


def test_parse_contract_map_rejects_malformed_rows():
    with pytest.raises(ValueError):
        parse_contract_map({"YM": {"sec_type": "FUT"}})          # no exchange
    with pytest.raises(ValueError):
        parse_contract_map({"YM": {"exchange": "CME", "sec_type": "FUT",
                                    "master_point_value": 0.0}})  # must be > 0
    with pytest.raises(ValueError):
        parse_contract_map({"YM": ["not", "a", "dict"]})


def test_pick_front_month_nearest_by_default():
    assert pick_front_month([("20261201", None), ("20270301", None)],
                            today="20261002", roll_days=5) == ("20261201", False)


def test_pick_front_month_rolls_in_window():
    # inside the 5-day roll window: next month is chosen, state says rolling
    chosen, rolling = pick_front_month([("20261218", 3000), ("20270318", 2000)],
                                       today="20261215", roll_days=5)
    assert chosen == "20270318" and rolling is True


def test_pick_front_month_no_roll_outside_window():
    chosen, rolling = pick_front_month([("20261218", 3000), ("20270318", 2000)],
                                       today="20261201", roll_days=5)
    assert chosen == "20261218" and rolling is False


def test_pick_front_month_prefers_open_interest():
    chosen, _ = pick_front_month([("20261201", 100.0), ("20270301", 9500.0)],
                                 today="20261002", roll_days=5)
    assert chosen == "20270301"


def test_pick_front_month_zero_oi_falls_back_to_nearest():
    chosen, _ = pick_front_month([("20261201", 0.0), ("20270301", 0.0)],
                                 today="20261002", roll_days=5)
    assert chosen == "20261201"


def test_pick_front_month_none_when_no_future():
    assert pick_front_month([("20250601", None)], today="20261002",
                            roll_days=5) is None