from __future__ import annotations

from typing import Callable


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