from __future__ import annotations

from dataclasses import dataclass

# MT5 position type constants.
BUY = 0   # POSITION_TYPE_BUY
SELL = 1  # POSITION_TYPE_SELL


@dataclass(frozen=True)
class Position:
    """A single master position as it appears in a snapshot."""

    ticket: int
    symbol: str
    side: int            # BUY or SELL
    open_price: float
    volume: float
    sl: float
    tp: float
    open_time: int       # epoch seconds
    point: float
    comment: str = ""
    magic: int = 0


@dataclass(frozen=True)
class Snapshot:
    """A full master position snapshot at one point in time."""

    timestamp: int
    heartbeat: int
    positions: tuple[Position, ...]


@dataclass(frozen=True)
class Event:
    """A diff event. `position` is the current position for NEW/MODIFY/PARTIAL
    and the previous position for CLOSE."""

    kind: str            # "NEW", "MODIFY", "PARTIAL", "CLOSE"
    position: Position


@dataclass
class Record:
    """The master->slave linkage for one copied position."""

    master_ticket: int
    magic: int
    slave_ticket: int
    master_open_volume: float
    slave_open_volume: float


@dataclass(frozen=True)
class SymbolInfo:
    """Per-symbol terminal info needed for lot sizing (volume params) and
    SL/TP tick rounding (point/digits/tick_size). Mirrors the subset of MT5's
    symbol_info namedtuple consumed by the engine + worker."""
    point: float
    digits: int
    tick_size: float
    volume_step: float
    volume_min: float
    volume_max: float
