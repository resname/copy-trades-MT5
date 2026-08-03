from __future__ import annotations

import math
from typing import Callable

from manager.engine.models import BUY


def parse_symbol_map(map_csv: str) -> dict[str, str]:
    """Parse 'master=slave,master2=slave2' into a dict. Spaces are stripped;
    pairs without exactly one '=' are skipped. Mirrors CSymbolMapper::Init."""
    result: dict[str, str] = {}
    if not map_csv:
        return result
    for pair in map_csv.split(","):
        pair = pair.replace(" ", "")
        if not pair:
            continue
        sides = pair.split("=")
        if len(sides) != 2:
            continue
        master, slave = sides[0], sides[1]
        result[master] = slave
    return result


class SymbolMapper:
    """Resolves a master symbol to the slave symbol to trade.

    exists_check(symbol) -> bool reports whether a symbol exists on the slave
    terminal (bound to mt5.symbol_info in production; a set/lambda in tests).
    """

    def __init__(self, map_csv: str, exists_check: Callable[[str], bool]) -> None:
        self._map = parse_symbol_map(map_csv)
        self._exists_check = exists_check

    def resolve(self, master_symbol: str) -> str:
        # 1. explicit mapping
        if master_symbol in self._map:
            return self._map[master_symbol]
        # 2. fallback to same name if it exists on the slave
        if self._exists_check(master_symbol):
            return master_symbol
        # 3. not found
        return ""


def calculate_lots(
    balance: float,
    step_amount: float,
    step_size: float,
    max_lot: float,
    lot_step: float,
    min_lot: float,
    max_lot_symbol: float,
) -> float:
    """Balance-step lot sizing. Ported from CLotSizer::CalculateLots.

    steps = floor(balance / step_amount); lots = steps * step_size;
    round DOWN to lot_step; clamp up to min_lot; cap at min(symbol max, max_lot).
    Returns 0.0 on invalid inputs (step/lot_step <= 0).
    """
    if step_amount <= 0.0 or step_size <= 0.0:
        return 0.0
    if lot_step <= 0.0:
        return 0.0

    steps = math.floor(balance / step_amount)
    lots = steps * step_size

    # round down to the lot-step grid
    lots = math.floor(lots / lot_step) * lot_step

    # clamp up to min, then cap at the two maxima
    lots = max(lots, min_lot)
    lots = min(lots, max_lot_symbol)
    lots = min(lots, max_lot)

    # normalize floating-point noise to the lot-step's digit count
    lot_digits = max(0, int(round(-math.log10(lot_step))))
    lots = round(lots, lot_digits)
    return lots


def normalize_sltp(
    master_open: float,
    master_sl: float,
    master_tp: float,
    slave_open: float,
    side: int,
) -> tuple[float, float]:
    """Reproduce the master's raw SL/TP price distance onto the slave's open
    price. Ported from CPriceNormalizer::NormalizeSLTP. A 0 SL/TP means 'none'
    and stays 0."""
    out_sl = 0.0
    out_tp = 0.0
    if side == BUY:
        if master_sl > 0.0:
            out_sl = slave_open - (master_open - master_sl)
        if master_tp > 0.0:
            out_tp = slave_open + (master_tp - master_open)
    else:  # SELL
        if master_sl > 0.0:
            out_sl = slave_open + (master_sl - master_open)
        if master_tp > 0.0:
            out_tp = slave_open - (master_open - master_tp)
    return out_sl, out_tp


def round_to_tick(price: float, tick_size: float, digits: int) -> float | None:
    """Round a price to the slave symbol's tick size, then to its digit count.
    Ported from SlaveSubscriber::RoundToTickSize. Returns None on failure
    (tick_size <= 0); returns 0.0 unchanged when price <= 0 (no SL/TP)."""
    if price <= 0.0:
        return 0.0
    if tick_size <= 0.0:
        return None
    rounded = round(price / tick_size) * tick_size
    return round(rounded, digits)
