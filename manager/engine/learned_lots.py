from __future__ import annotations

import math
import threading
from collections.abc import Iterable

from manager.engine.models import Position


class LearnedMasterLots:
    """Thread-safe per-master-symbol maximum lot observer.

    The engine updates it from every master snapshot; slaves that opted into
    master-lot tracking read the learned maximum as their effective base.

    ``reset(open_tickets)`` clears the values and suppresses the given tickets
    until they disappear from a later snapshot, so a position that is still
    open at reset time cannot immediately re-learn the old base.
    """

    def __init__(self, initial: dict[str, float] | None = None) -> None:
        self._lock = threading.Lock()
        self._lots: dict[str, float] = {}
        self._suppressed: set[int] = set()
        if initial:
            for symbol, value in initial.items():
                if not isinstance(symbol, str) or not symbol:
                    continue
                try:
                    lot = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(lot) and lot > 0.0:
                    self._lots[symbol] = lot

    def update(self, positions: Iterable[Position]) -> bool:
        """Record the highest finite, positive volume per symbol. Returns
        whether any stored value grew."""
        grew = False
        with self._lock:
            # A ticket that disappeared from the snapshot is closed (or was a
            # rejected order); it can never re-learn anything.
            self._suppressed &= {p.ticket for p in positions}
            for pos in positions:
                if pos.ticket in self._suppressed:
                    continue
                volume = pos.volume
                if not math.isfinite(volume) or volume <= 0.0:
                    continue
                if volume > self._lots.get(pos.symbol, 0.0):
                    self._lots[pos.symbol] = volume
                    grew = True
        return grew

    def get(self, symbol: str) -> float:
        with self._lock:
            return self._lots.get(symbol, 0.0)

    def as_dict(self) -> dict[str, float]:
        with self._lock:
            return dict(self._lots)

    def reset(self, open_tickets: Iterable[int]) -> None:
        with self._lock:
            self._lots.clear()
            self._suppressed = set(open_tickets)
