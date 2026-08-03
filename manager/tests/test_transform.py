from manager.engine.transform import parse_symbol_map, SymbolMapper


def test_parse_symbol_map_basic():
    assert parse_symbol_map("EURUSD=EURUSD,GBPUSD=GBPUSD") == {
        "EURUSD": "EURUSD", "GBPUSD": "GBPUSD",
    }


def test_parse_symbol_map_strips_spaces_and_skips_invalid():
    # spaces stripped; pair without '=' skipped; pair with 3 sides skipped
    assert parse_symbol_map("EURUSD = EURUSD , GBPUSD, X=Y=Z") == {"EURUSD": "EURUSD"}


def test_parse_symbol_map_empty():
    assert parse_symbol_map("") == {}


def test_resolve_explicit_mapping_wins():
    mapper = SymbolMapper("EURUSD=EURUSD_Z", exists_check=lambda s: False)
    assert mapper.resolve("EURUSD") == "EURUSD_Z"


def test_resolve_same_name_fallback_when_exists():
    mapper = SymbolMapper("", exists_check=lambda s: s == "USDJPY")
    assert mapper.resolve("USDJPY") == "USDJPY"


def test_resolve_returns_empty_when_missing_and_no_fallback():
    mapper = SymbolMapper("", exists_check=lambda s: False)
    assert mapper.resolve("XYZABC") == ""


def test_resolve_explicit_overrides_fallback():
    # explicit map present but slave symbol does not exist -> still returns the
    # explicit mapping (the EA returns the explicit value without an existence
    # check on the mapped symbol).
    mapper = SymbolMapper("EURUSD=EURUSD_Z", exists_check=lambda s: False)
    assert mapper.resolve("EURUSD") == "EURUSD_Z"


from manager.engine.transform import calculate_lots


def test_lots_basic_on_grid():
    # balance 1000, 100 per 0.01 step -> 10 steps -> 0.10 lot
    assert calculate_lots(1000, 100, 0.01, 10, 0.01, 0.01, 100) == 0.10


def test_lots_rounds_down_to_lot_step():
    # 2 steps * 0.05 = 0.10, lot step 0.1 -> floor(0.10/0.1)*0.1 = 0.1
    assert calculate_lots(250, 100, 0.05, 10, 0.1, 0.01, 100) == 0.1


def test_lots_clamps_up_to_min_lot():
    # balance 50 < step 100 -> 0 steps -> 0 -> clamped up to min 0.01
    assert calculate_lots(50, 100, 0.01, 10, 0.01, 0.01, 100) == 0.01


def test_lots_caps_at_max_lot_setting():
    # 100 steps * 0.01 = 1.0, capped at max_lot 0.5
    assert calculate_lots(10000, 100, 0.01, 0.5, 0.01, 0.01, 100) == 0.5


def test_lots_caps_at_symbol_max_lot():
    # 100 steps * 0.01 = 1.0, capped at symbol max 0.2
    assert calculate_lots(10000, 100, 0.01, 10, 0.01, 0.01, 0.2) == 0.2


def test_lots_invalid_step_amount_returns_zero():
    assert calculate_lots(1000, 0, 0.01, 10, 0.01, 0.01, 100) == 0.0


def test_lots_invalid_step_size_returns_zero():
    assert calculate_lots(1000, 100, 0.0, 10, 0.01, 0.01, 100) == 0.0


def test_lots_invalid_lot_step_returns_zero():
    assert calculate_lots(1000, 100, 0.01, 10, 0.0, 0.01, 100) == 0.0


import pytest
from manager.engine.transform import normalize_sltp, round_to_tick
from manager.engine.models import BUY, SELL


def test_normalize_buy_sl_tp():
    sl, tp = normalize_sltp(master_open=1.10000, master_sl=1.09500,
                           master_tp=1.10500, slave_open=1.20000, side=BUY)
    # raw-distance reproduction leaves FP noise (1.1949999...); the EA leaves
    # this raw and tick-rounds later in the caller, so compare with approx.
    assert sl == pytest.approx(1.19500)
    assert tp == pytest.approx(1.20500)


def test_normalize_buy_no_sl_no_tp():
    sl, tp = normalize_sltp(1.10000, 0.0, 0.0, 1.20000, BUY)
    assert sl == 0.0
    assert tp == 0.0


def test_normalize_sell_sl_tp():
    sl, tp = normalize_sltp(master_open=1.10000, master_sl=1.10500,
                           master_tp=1.09500, slave_open=1.20000, side=SELL)
    assert sl == pytest.approx(1.20500)
    assert tp == pytest.approx(1.19500)


def test_round_to_tick_basic():
    assert round_to_tick(1.19457, 0.00001, 5) == 1.19457


def test_round_to_tick_rounds_to_nearest():
    # 1.194576 / 0.0001 = 11945.76 -> round to 11946 -> 1.1946
    assert round_to_tick(1.194576, 0.0001, 4) == 1.1946


def test_round_to_tick_zero_price_unchanged():
    assert round_to_tick(0.0, 0.00001, 5) == 0.0


def test_round_to_tick_invalid_tick_size_returns_none():
    assert round_to_tick(1.19457, 0.0, 5) is None