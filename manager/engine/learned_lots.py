from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable

from manager.engine.models import Position


def _valid_ticket(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class LearnedMasterLots:
    """Thread-safe per-master-symbol maximum lot observer.

    The engine updates it from every master snapshot; slaves that opted into
    master-lot tracking read the learned maximum as their effective base.

    ``reset(open_tickets)`` clears the values and suppresses the given tickets
    until they disappear from a later snapshot, so a position that is still
    open at reset time cannot immediately re-learn the old base. The
    suppression set can be persisted and replayed across restarts. When no
    snapshot has been seen yet, ``suppress_next_snapshot=True`` suppresses the
    tickets of the first snapshot instead.
    """

    def __init__(self, initial: dict[str, float] | None = None,
                 suppressed: Iterable[int] | None = None) -> None:
        self._lock = threading.Lock()
        self._lots: dict[str, float] = {}
        self._suppressed: set[int] = {
            t for t in (suppressed or ()) if _valid_ticket(t)}
        self._suppress_next = False
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
        # Materialize once: the caller may pass a one-shot iterator.
        positions = list(positions)
        grew = False
        with self._lock:
            current = {p.ticket for p in positions}
            if self._suppress_next:
                # Reset happened before the first snapshot: suppress whatever
                # it reports, so still-open positions cannot re-learn.
                self._suppressed |= current
                self._suppress_next = False
            # A ticket that disappeared from the snapshot is closed (or was a
            # rejected order); it can never re-learn anything.
            self._suppressed &= current
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

    def suppressed(self) -> set[int]:
        with self._lock:
            return set(self._suppressed)

    def reset(self, open_tickets: Iterable[int],
              suppress_next_snapshot: bool = False) -> None:
        with self._lock:
            self._lots.clear()
            self._suppressed = {t for t in open_tickets if _valid_ticket(t)}
            self._suppress_next = suppress_next_snapshot

    def with_snapshot(self, fn: Callable[[dict[str, float], set[int]], None]
                      ) -> None:
        """Run `fn(lots, suppressed)` under the lock, so a concurrent reset
        cannot interleave between the read and the caller's persistence."""
        with self._lock:
            fn(dict(self._lots), set(self._suppressed))
