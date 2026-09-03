from __future__ import annotations

import math
import re
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

    Map rows are tried in three tiers, in order:
    1. exact match on the master entry (legacy rows behave identically),
    2. regex rows in map order: the master entry is a pattern matched with
       re.fullmatch (the whole symbol must match); the slave entry is a
       template where $1..$9 substitute captured groups and $$ a literal $,
    3. same name if it exists on the slave terminal.

    exists_check(symbol) -> bool reports whether a symbol exists on the slave
    terminal (bound to mt5.symbol_info in production; a set/lambda in tests).

    A row that cannot work as a regex (master entry does not compile, or the
    template references a group the pattern never captures) falls back to
    exact-match only, so no existing config changes behavior.
    """

    def __init__(self, map_csv: str, exists_check: Callable[[str], bool]) -> None:
        self._map = parse_symbol_map(map_csv)
        self._exists_check = exists_check
        self._regex_rules: list[tuple[re.Pattern, str]] = []
        for master, slave in self._map.items():
            try:
                pattern = re.compile(master)
            except re.error:
                continue  # exact-only row
            template = _expand_template(slave, pattern.groups)
            if template is None:
                continue  # exact-only row
            self._regex_rules.append((pattern, template))

    def resolve(self, master_symbol: str) -> str:
        # 1. exact mapping
        if master_symbol in self._map:
            return self._map[master_symbol]
        # 2. regex rows, first fullmatch wins
        for pattern, template in self._regex_rules:
            match = pattern.fullmatch(master_symbol)
            if match is not None:
                return match.expand(template)
        # 3. fallback to same name if it exists on the slave
        if self._exists_check(master_symbol):
            return master_symbol
        # 4. not found
        return ""


def _expand_template(slave: str, ngroups: int) -> str | None:
    """Translate a slave entry into a Match.expand template: $1..$9 become
    backreferences, $$ a literal $. Returns None (row falls back to exact-only)
    when a referenced group exceeds the pattern's group count. Backslashes are
    escaped so a literal \\ in a symbol name survives expansion."""
    parts: list[str] = []
    i = 0
    while i < len(slave):
        ch = slave[i]
        if ch == "\\":
            parts.append("\\\\")
            i += 1
        elif ch == "$" and i + 1 < len(slave) and slave[i + 1] == "$":
            parts.append("$")
            i += 2
        elif (ch == "$" and i + 1 < len(slave)
              and slave[i + 1].isdigit() and slave[i + 1] != "0"):
            n = int(slave[i + 1])
            if n > ngroups:
                return None
            parts.append(f"\\g<{n}>")
            i += 2
        else:
            parts.append(ch)
            i += 1
    return "".join(parts)


SIZING_BALANCE_STEP = "balance_step"
SIZING_COPY_MASTER = "copy_master"
SIZING_FIXED_LOT = "fixed_lot"


def _snap_clamp(lots: float, lot_step: float, min_lot: float,
                max_lot: float, max_lot_symbol: float) -> float:
    """Snap `lots` DOWN to the lot-step grid, clamp UP to min_lot, cap at
    min(max_lot_symbol, max_lot), normalize to the lot-step's digit count.
    Returns 0.0 if lot_step <= 0 (cannot snap). Shared tail for every mode."""
    if lot_step <= 0.0:
        return 0.0
    # +1e-9: a quotient sitting epsilon below its grid multiple (0.29/0.01 =
    # 28.999999999999996 in IEEE-754) is on the grid, not a step below it.
    lots = math.floor(lots / lot_step + 1e-9) * lot_step
    lots = max(lots, min_lot)
    lots = min(lots, max_lot_symbol)
    lots = min(lots, max_lot)
    lot_digits = max(0, int(round(-math.log10(lot_step))))
    return round(lots, lot_digits)


def calculate_slave_lot(mode: str, master_volume: float, balance: float,
                        step_amount: float, step_size: float,
                        master_base_lot: float, fixed_lot: float,
                        max_lot: float, lot_step: float, min_lot: float,
                        max_lot_symbol: float) -> float:
    """Per-slave lot sizing across three modes. Each mode yields a raw lot;
    _snap_clamp then snaps/clamps/caps/rounds it. Returns 0.0 on invalid
    config (the engine skips the trade).

    - balance_step: raw = floor(balance/step_amount)*step_size; if
      master_base_lot > 0 and master_volume < master_base_lot, scale DOWN:
      raw *= master_volume / master_base_lot (never scales up).
    - copy_master: raw = master_volume.
    - fixed_lot: raw = fixed_lot.
    """
    if mode == SIZING_BALANCE_STEP:
        if step_amount <= 0.0 or step_size <= 0.0:
            return 0.0
        # +1e-9: same float-epsilon guard as _snap_clamp — a balance sitting
        # 1-ulp below a step multiple (float P/L accumulation) is on the step
        raw = math.floor(balance / step_amount + 1e-9) * step_size
        if master_base_lot > 0.0 and master_volume < master_base_lot:
            raw *= master_volume / master_base_lot
        return _snap_clamp(raw, lot_step, min_lot, max_lot, max_lot_symbol)
    if mode == SIZING_COPY_MASTER:
        return _snap_clamp(master_volume, lot_step, min_lot, max_lot,
                           max_lot_symbol)
    if mode == SIZING_FIXED_LOT:
        if fixed_lot <= 0.0:
            return 0.0
        return _snap_clamp(fixed_lot, lot_step, min_lot, max_lot, max_lot_symbol)
    return 0.0


def calculate_lots(balance: float, step_amount: float, step_size: float,
                   max_lot: float, lot_step: float, min_lot: float,
                   max_lot_symbol: float) -> float:
    """Balance-step lot sizing (legacy). Thin wrapper over
    calculate_slave_lot(balance_step, master_base_lot=0.0) so existing callers
    and tests keep working unchanged. Ported from CLotSizer::CalculateLots."""
    return calculate_slave_lot(
        SIZING_BALANCE_STEP, 0.0, balance, step_amount, step_size,
        0.0, 0.0, max_lot, lot_step, min_lot, max_lot_symbol)


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
