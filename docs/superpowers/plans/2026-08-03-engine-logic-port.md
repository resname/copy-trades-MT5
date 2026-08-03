# Engine Logic Port — Implementation Plan (Plan 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the MT5 trade-copier's core logic (magic/comment linkage, symbol mapping, lot sizing, SL/TP normalization, snapshot diffing, record table, baseline/age filter) from the MQL5 EA into a pure-Python library that is fully unit-testable with no MT5 terminal, no GUI, and no IPC.

**Architecture:** A `manager/engine/` package of small, single-responsibility modules ported faithfully from `MQL5/Include/TradeCopier/*.mqh`. Each module is pure functions/dataclasses with no external dependencies (no `MetaTrader5`, no `PySide6`, no `pywin32`). Terminal-dependent behavior (symbol existence, lot step, tick size) is injected as callables/values so the logic is deterministic in tests. This is the foundation; later plans wire it into workers, IPC, terminal management, and the GUI.

**Tech Stack:** Python 3.11+, pytest. No other dependencies for this plan.

## Global Constraints

- Python 3.11+ (the full project targets Windows + the `MetaTrader5` package, but this plan needs only Python + pytest).
- Demo and real accounts are both in scope for the product; this plan's logic is account-agnostic.
- Magic-number + comment linkage scheme is reused **verbatim** from the EA: `MAGIC_BASE = 1000000`, slave magic = `MAGIC_BASE + (master_ticket % 900000)`, copied-position comment = `CPY#<master_ticket>|MV<master_vol 8dp>|SV<slave_vol 8dp>`. A slave migrated from the old EA to this manager must not confuse state.
- `POSITION_TYPE_BUY = 0`, `POSITION_TYPE_SELL = 1` (MT5 constants) — the Python `side` field uses these int values.
- Lot-sizing clamps **up** to `min_lot` (faithful to the EA: `lots = max(lots, min_lot)`); a calculated lot of 0 only occurs on invalid inputs (step ≤ 0, lot step ≤ 0, symbol unavailable), not from being under `min_lot`.
- Baseline behavior is **copy-recent-opens-at-start** (user-confirmed): on Start, master positions opened within per-slave `maxTradeAge` are copied; older ones are skipped. This emerges naturally because the manager has no "previous" snapshot on Start, so the first diff emits `NEW` for every current position and the `maxTradeAge` + `RecordTable` filters decide what to copy. (The EA's `EstablishBaseline` suppresses recent opens; the manager does NOT replicate that suppression.)
- TDD: every task writes the failing test first, runs it to confirm failure, implements minimal code, runs to confirm pass, then commits. Commit only when tests pass.
- One commit per task (or per logical step where noted). Run the **whole** test suite before each commit, not just the new test.

---

## File Structure (this plan creates)

```
manager/
  __init__.py
  engine/
    __init__.py
    models.py          # Position, Snapshot, Event, Record dataclasses + BUY/SELL constants
    linkage.py         # MAGIC_BASE, magic_for, encode_comment, decode_comment
    transform.py       # parse_symbol_map, SymbolMapper, calculate_lots, normalize_sltp, round_to_tick
    snapshot_diff.py   # diff(prev, curr) -> list[Event]
    record_table.py    # RecordTable (master_ticket -> Record)
    baseline.py        # is_too_old, seed_from_recovery
  tests/
    __init__.py
    test_models.py
    test_linkage.py
    test_transform.py
    test_snapshot_diff.py
    test_record_table.py
    test_baseline.py
    test_engine_integration.py
pyproject.toml
```

Each module has one responsibility. `models.py` holds the shared domain types so every other module imports from one place. Tests mirror the modules one-to-one, plus one integration test that composes them.

---

## Task 1: Scaffold package + domain models

**Files:**
- Create: `pyproject.toml`
- Create: `manager/__init__.py`
- Create: `manager/engine/__init__.py`
- Create: `manager/engine/models.py`
- Create: `manager/tests/__init__.py`
- Create: `manager/tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `manager.engine.models.Position`, `Snapshot`, `Event`, `Record` (dataclasses); constants `BUY = 0`, `SELL = 1`. Later tasks import these.

- [ ] **Step 1: Write the failing test**

`manager/tests/test_models.py`:
```python
from manager.engine.models import Position, Snapshot, Event, Record, BUY, SELL


def test_buy_sell_constants_match_mt5():
    assert BUY == 0
    assert SELL == 1


def test_position_construction_and_defaults():
    p = Position(
        ticket=12345, symbol="EURUSD", side=BUY, open_price=1.10,
        volume=0.5, sl=1.09, tp=1.11, open_time=1700000000, point=0.00001,
    )
    assert p.ticket == 12345
    assert p.comment == ""  # default
    assert p.side == BUY


def test_snapshot_holds_tuple_of_positions():
    p = Position(ticket=1, symbol="X", side=BUY, open_price=1.0,
                 volume=0.1, sl=0.0, tp=0.0, open_time=0, point=0.0001)
    s = Snapshot(timestamp=1700000000, heartbeat=1, positions=(p,))
    assert s.positions == (p,)
    assert len(s.positions) == 1


def test_event_kinds_and_record_fields():
    p = Position(ticket=1, symbol="X", side=SELL, open_price=1.0,
                 volume=0.1, sl=0.0, tp=0.0, open_time=0, point=0.0001)
    e = Event(kind="NEW", position=p)
    assert e.kind == "NEW"
    assert e.position == p
    r = Record(master_ticket=1, magic=1000001, slave_ticket=99,
               master_open_volume=0.5, slave_open_volume=0.05)
    assert r.master_ticket == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager'` (or import error).

- [ ] **Step 3: Write minimal implementation**

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "copy-trades-mt5-manager"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["manager/tests"]

[tool.setuptools.packages.find]
include = ["manager*"]
```

`manager/__init__.py` and `manager/engine/__init__.py` and `manager/tests/__init__.py`: empty files.

`manager/engine/models.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_models.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml manager/__init__.py manager/engine/__init__.py manager/engine/models.py manager/tests/__init__.py manager/tests/test_models.py
git commit -m "feat(manager): scaffold package + engine domain models"
```

---

## Task 2: Magic-number + comment linkage

Ported from `CopierConfig.mqh` (`MAGIC_BASE`) and `SlaveSubscriber.mqh` (comment format `CPY#%I64u|MV%.8f|SV%.8f` + the comment parser).

**Files:**
- Create: `manager/engine/linkage.py`
- Create: `manager/tests/test_linkage.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `MAGIC_BASE = 1000000`, `MAGIC_MOD = 900000`; `magic_for(master_ticket: int) -> int`; `encode_comment(master_ticket: int, master_volume: float, slave_volume: float) -> str`; `decode_comment(comment: str) -> tuple[int, float | None, float | None] | None` (returns `(master_ticket, master_volume, slave_volume)` or `None` if no `CPY#` prefix / no ticket digits; volumes are `None` when absent/unparseable).

- [ ] **Step 1: Write the failing test**

`manager/tests/test_linkage.py`:
```python
from manager.engine.linkage import (
    MAGIC_BASE, MAGIC_MOD, magic_for, encode_comment, decode_comment,
)


def test_magic_base_and_mod_match_ea():
    assert MAGIC_BASE == 1000000
    assert MAGIC_MOD == 900000


def test_magic_for_basic():
    assert magic_for(12345) == 1012345


def test_magic_for_wraps_modulo():
    # 912345 % 900000 == 12345 -> same magic as 12345 (EA collision behavior preserved)
    assert magic_for(912345) == 1012345
    # large ticket wraps
    assert magic_for(123456789) == MAGIC_BASE + (123456789 % MAGIC_MOD)


def test_encode_comment_format():
    assert encode_comment(12345, 0.5, 0.05) == "CPY#12345|MV0.50000000|SV0.05000000"


def test_decode_comment_full():
    assert decode_comment("CPY#12345|MV0.50000000|SV0.05000000") == (12345, 0.5, 0.05)


def test_decode_comment_ticket_only_no_pipe():
    assert decode_comment("CPY#12345") == (12345, None, None)


def test_decode_comment_missing_sv():
    assert decode_comment("CPY#12345|MV0.50000000") == (12345, 0.5, None)


def test_decode_comment_no_prefix():
    assert decode_comment("manual trade") is None


def test_decode_comment_no_digits_after_prefix():
    assert decode_comment("CPY#abc") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_linkage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.engine.linkage'`.

- [ ] **Step 3: Write minimal implementation**

`manager/engine/linkage.py`:
```python
from __future__ import annotations

import re

# Verbatim from CopierConfig.mqh. Slave magic = base + (master_ticket % mod).
MAGIC_BASE = 1000000
MAGIC_MOD = 900000

# Comment format from SlaveSubscriber.mqh: CPY#<ticket>|MV<vol 8dp>|SV<vol 8dp>.
_PREFIX = "CPY#"
_TICKET_RE = re.compile(r"CPY#(\d+)")
_MV_RE = re.compile(r"\|MV([0-9.]+)")
_SV_RE = re.compile(r"\|SV([0-9.]+)")


def magic_for(master_ticket: int) -> int:
    return MAGIC_BASE + (master_ticket % MAGIC_MOD)


def encode_comment(master_ticket: int, master_volume: float, slave_volume: float) -> str:
    return f"CPY#{master_ticket}|MV{master_volume:.8f}|SV{slave_volume:.8f}"


def decode_comment(comment: str) -> tuple[int, float | None, float | None] | None:
    """Parse a copied-position comment.

    Returns (master_ticket, master_volume, slave_volume) where volumes are
    None when absent or not positive. Returns None if there is no CPY# prefix
    or no ticket digits. Mirrors SlaveSubscriber.mqh's comment parser.
    """
    if not comment or _PREFIX not in comment:
        return None
    ticket_match = _TICKET_RE.search(comment)
    if not ticket_match:
        return None
    master_ticket = int(ticket_match.group(1))

    mv_match = _MV_RE.search(comment)
    sv_match = _SV_RE.search(comment)
    master_volume: float | None = None
    slave_volume: float | None = None
    if mv_match and sv_match:
        mv = float(mv_match.group(1))
        sv = float(sv_match.group(1))
        if mv > 0.0 and sv > 0.0:
            master_volume = mv
            slave_volume = sv
    return (master_ticket, master_volume, slave_volume)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_linkage.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/linkage.py manager/tests/test_linkage.py
git commit -m "feat(engine): magic-number + CPY comment linkage"
```

---

## Task 3: Symbol mapping

Ported from `SymbolMapper.mqh` (`Init` CSV parse + `Resolve` explicit-map → same-name-fallback → empty). Symbol existence is injected (the EA checks `CSymbolInfo.Name+Select`; the Python version takes a callable so it is testable without a terminal).

**Files:**
- Create: `manager/engine/transform.py` (created here, extended in Tasks 4–5)
- Create: `manager/tests/test_transform.py` (created here, extended in Tasks 4–5)

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_symbol_map(map_csv: str) -> dict[str, str]`; `SymbolMapper(map_csv: str, exists_check: Callable[[str], bool])` with `.resolve(master_symbol: str) -> str` (empty string means "not mappable, skip").

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_transform.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_transform.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.engine.transform'`.

- [ ] **Step 3: Write minimal implementation**

`manager/engine/transform.py` (initial content):
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_transform.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/transform.py manager/tests/test_transform.py
git commit -m "feat(engine): symbol mapper with injected existence check"
```

---

## Task 4: Lot sizing

Ported from `LotSizer.mqh::CalculateLots`. Symbol lot params (lot_step, min_lot, max_lot) are passed in (the EA reads them from `CSymbolInfo`; the Python version takes them as args so it is deterministic in tests).

**Files:**
- Modify: `manager/engine/transform.py` (append `calculate_lots`)
- Modify: `manager/tests/test_transform.py` (append lot tests)

**Interfaces:**
- Consumes: nothing.
- Produces: `calculate_lots(balance, step_amount, step_size, max_lot, lot_step, min_lot, max_lot_symbol) -> float` in `manager.engine.transform`.

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_transform.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_transform.py -v`
Expected: FAIL with `ImportError: cannot import name 'calculate_lots'`.

- [ ] **Step 3: Write minimal implementation**

Append to `manager/engine/transform.py`:
```python
import math


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
```

(Note: `import math` is added once at the top of the file; if Task 3 already wrote the file, place `import math` with the other top imports. The integration is checked in Step 4.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_transform.py -v`
Expected: PASS (15 tests: 7 symbol + 8 lot). If `import math` placement caused a NameError, move it to the top of `transform.py` and re-run.

- [ ] **Step 5: Commit**

```bash
git add manager/engine/transform.py manager/tests/test_transform.py
git commit -m "feat(engine): balance-step lot sizing ported from EA"
```

---

## Task 5: SL/TP normalization + tick rounding

Ported from `PriceNormalizer.mqh::NormalizeSLTP` (raw price-distance reproduction) and `SlaveSubscriber.mqh::RoundToTickSize` (round to the slave symbol's tick size / digits).

**Files:**
- Modify: `manager/engine/transform.py` (append `normalize_sltp`, `round_to_tick`)
- Modify: `manager/tests/test_transform.py` (append tests)

**Interfaces:**
- Consumes: `BUY`, `SELL` from `manager.engine.models`.
- Produces: `normalize_sltp(master_open, master_sl, master_tp, slave_open, side) -> tuple[float, float]`; `round_to_tick(price, tick_size, digits) -> float | None` (`None` = failure when `tick_size <= 0`; returns `0.0` unchanged when `price <= 0`).

- [ ] **Step 1: Write the failing test**

Append to `manager/tests/test_transform.py`:
```python
from manager.engine.transform import normalize_sltp, round_to_tick
from manager.engine.models import BUY, SELL


def test_normalize_buy_sl_tp():
    sl, tp = normalize_sltp(master_open=1.10000, master_sl=1.09500,
                           master_tp=1.10500, slave_open=1.20000, side=BUY)
    assert sl == 1.19500
    assert tp == 1.20500


def test_normalize_buy_no_sl_no_tp():
    sl, tp = normalize_sltp(1.10000, 0.0, 0.0, 1.20000, BUY)
    assert sl == 0.0
    assert tp == 0.0


def test_normalize_sell_sl_tp():
    sl, tp = normalize_sltp(master_open=1.10000, master_sl=1.10500,
                           master_tp=1.09500, slave_open=1.20000, side=SELL)
    assert sl == 1.20500
    assert tp == 1.19500


def test_round_to_tick_basic():
    assert round_to_tick(1.19457, 0.00001, 5) == 1.19457


def test_round_to_tick_rounds_to_nearest():
    # 1.194576 / 0.0001 = 11945.76 -> round to 11946 -> 1.1946
    assert round_to_tick(1.194576, 0.0001, 4) == 1.1946


def test_round_to_tick_zero_price_unchanged():
    assert round_to_tick(0.0, 0.00001, 5) == 0.0


def test_round_to_tick_invalid_tick_size_returns_none():
    assert round_to_tick(1.19457, 0.0, 5) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_transform.py -v`
Expected: FAIL with `ImportError: cannot import name 'normalize_sltp'`.

- [ ] **Step 3: Write minimal implementation**

Append to `manager/engine/transform.py`:
```python
from manager.engine.models import BUY


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
```

(If `from manager.engine.models import BUY` would clash with a top-of-file import added in Task 3, keep a single import line at the top: `from manager.engine.models import BUY`. Adjust in Step 4 if needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_transform.py -v`
Expected: PASS (22 tests: 7 symbol + 8 lot + 7 sltp/tick).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/transform.py manager/tests/test_transform.py
git commit -m "feat(engine): SL/TP raw-distance normalization + tick rounding"
```

---

## Task 6: Snapshot diff

Ported from `SlaveSubscriber.mqh::DiffAndProcess`. Emits NEW / MODIFY / PARTIAL / CLOSE events by comparing consecutive snapshots keyed by master ticket. A position whose volume decreased AND whose SL/TP changed emits both PARTIAL and MODIFY (the EA fires both independently).

**Files:**
- Create: `manager/engine/snapshot_diff.py`
- Create: `manager/tests/test_snapshot_diff.py`

**Interfaces:**
- Consumes: `Position`, `Event` from `manager.engine.models`.
- Produces: `diff(prev: Sequence[Position], curr: Sequence[Position]) -> list[Event]` in `manager.engine.snapshot_diff`.

- [ ] **Step 1: Write the failing test**

`manager/tests/test_snapshot_diff.py`:
```python
from manager.engine.snapshot_diff import diff
from manager.engine.models import Position, BUY


def _pos(ticket, volume=0.5, sl=0.0, tp=0.0, symbol="EURUSD", side=BUY,
          open_price=1.10, open_time=1700000000, point=0.00001):
    return Position(ticket=ticket, symbol=symbol, side=side,
                    open_price=open_price, volume=volume, sl=sl, tp=tp,
                    open_time=open_time, point=point)


def test_new_position():
    events = diff(prev=[], curr=[_pos(1)])
    assert [e.kind for e in events] == ["NEW"]
    assert events[0].position.ticket == 1


def test_no_change_emits_nothing():
    p = _pos(1)
    assert diff(prev=[p], curr=[_pos(1)]) == []


def test_partial_close_when_volume_decreases():
    events = diff(prev=[_pos(1, volume=0.5)], curr=[_pos(1, volume=0.3)])
    assert [e.kind for e in events] == ["PARTIAL"]
    assert events[0].position.volume == 0.3


def test_modify_when_sl_changes():
    events = diff(prev=[_pos(1, sl=1.09)], curr=[_pos(1, sl=1.08)])
    assert [e.kind for e in events] == ["MODIFY"]


def test_modify_when_tp_changes():
    events = diff(prev=[_pos(1, tp=1.11)], curr=[_pos(1, tp=1.12)])
    assert [e.kind for e in events] == ["MODIFY"]


def test_partial_and_modify_both_emitted():
    events = diff(prev=[_pos(1, volume=0.5, tp=1.11)],
                  curr=[_pos(1, volume=0.3, tp=1.12)])
    assert [e.kind for e in events] == ["PARTIAL", "MODIFY"]


def test_close_when_ticket_gone():
    p = _pos(1)
    events = diff(prev=[p], curr=[])
    assert [e.kind for e in events] == ["CLOSE"]
    assert events[0].position.ticket == 1  # CLOSE carries the previous position


def test_mixed_new_modify_close():
    p1, p2 = _pos(1, sl=1.09), _pos(2)
    events = diff(prev=[p1, p2], curr=[_pos(1, sl=1.08), _pos(3)])
    kinds = [e.kind for e in events]
    assert kinds == ["MODIFY", "NEW", "CLOSE"]
    assert events[0].position.ticket == 1   # MODIFY of p1
    assert events[1].position.ticket == 3   # NEW p3
    assert events[2].position.ticket == 2   # CLOSE p2


def test_volume_increase_not_partial():
    # volume increasing is not a partial close (no event for volume up alone)
    events = diff(prev=[_pos(1, volume=0.3)], curr=[_pos(1, volume=0.5)])
    assert events == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_snapshot_diff.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.engine.snapshot_diff'`.

- [ ] **Step 3: Write minimal implementation**

`manager/engine/snapshot_diff.py`:
```python
from __future__ import annotations

from collections.abc import Sequence

from manager.engine.models import Event, Position

_EPS = 1e-8  # mirrors the EA's NormalizeDouble(..., 8) comparison tolerance


def diff(prev: Sequence[Position], curr: Sequence[Position]) -> list[Event]:
    """Diff two snapshots keyed by master ticket.

    - ticket in curr but not prev -> NEW
    - ticket in both, volume decreased -> PARTIAL (current position)
    - ticket in both, sl or tp changed -> MODIFY (current position)
      (PARTIAL and MODIFY can both fire for one position)
    - ticket in prev but not curr -> CLOSE (previous position)

    Events are emitted in curr order (NEW/PARTIAL/MODIFY) then prev order
    (CLOSE), matching SlaveSubscriber::DiffAndProcess.
    """
    prev_by_ticket = {p.ticket: p for p in prev}
    curr_by_ticket = {p.ticket: p for p in curr}
    events: list[Event] = []

    for c in curr:
        p = prev_by_ticket.get(c.ticket)
        if p is None:
            events.append(Event(kind="NEW", position=c))
            continue
        if round(p.volume - c.volume, 8) > _EPS:
            events.append(Event(kind="PARTIAL", position=c))
        if abs(round(p.sl - c.sl, 8)) > _EPS or abs(round(p.tp - c.tp, 8)) > _EPS:
            events.append(Event(kind="MODIFY", position=c))

    for p in prev:
        if p.ticket not in curr_by_ticket:
            events.append(Event(kind="CLOSE", position=p))

    return events
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_snapshot_diff.py -v`
Expected: PASS (9 tests). If a collection error occurs, check for a typo in a test name and re-run.

- [ ] **Step 5: Commit**

```bash
git add manager/engine/snapshot_diff.py manager/tests/test_snapshot_diff.py
git commit -m "feat(engine): snapshot diff (NEW/MODIFY/PARTIAL/CLOSE)"
```

---

## Task 7: Record table (master→slave linkage state)

Ported from `SlaveSubscriber.mqh`'s `m_records[]` + `FindRecord`/add/remove. Keyed by `master_ticket` (cleaner than the EA's magic-keyed linear search, and avoids the `ticket % 900000` collision edge case).

**Files:**
- Create: `manager/engine/record_table.py`
- Create: `manager/tests/test_record_table.py`

**Interfaces:**
- Consumes: `Record` from `manager.engine.models`.
- Produces: `RecordTable` in `manager.engine.record_table` with `.has(master_ticket) -> bool`, `.get(master_ticket) -> Record | None`, `.add(record: Record) -> None`, `.remove(master_ticket) -> None`, `.all() -> list[Record]`, `.__len__()`.

- [ ] **Step 1: Write the failing test**

`manager/tests/test_record_table.py`:
```python
from manager.engine.models import Record
from manager.engine.record_table import RecordTable


def _rec(ticket=1, slave_ticket=99):
    return Record(master_ticket=ticket, magic=1000000 + ticket,
                  slave_ticket=slave_ticket, master_open_volume=0.5,
                  slave_open_volume=0.05)


def test_add_and_get():
    table = RecordTable()
    table.add(_rec(1, 99))
    assert table.has(1)
    assert table.get(1).slave_ticket == 99
    assert len(table) == 1


def test_get_missing_returns_none():
    table = RecordTable()
    assert table.get(123) is None
    assert not table.has(123)


def test_remove():
    table = RecordTable()
    table.add(_rec(1, 99))
    table.remove(1)
    assert not table.has(1)
    assert len(table) == 0


def test_remove_missing_is_noop():
    table = RecordTable()
    table.remove(999)  # must not raise
    assert len(table) == 0


def test_all_returns_every_record():
    table = RecordTable()
    table.add(_rec(1, 99))
    table.add(_rec(2, 100))
    records = table.all()
    assert {r.master_ticket for r in records} == {1, 2}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_record_table.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.engine.record_table'`.

- [ ] **Step 3: Write minimal implementation**

`manager/engine/record_table.py`:
```python
from __future__ import annotations

from manager.engine.models import Record


class RecordTable:
    """The master->slave linkage state for one slave.

    Keyed by master_ticket (the EA keyed by magic and searched linearly;
    keying by master_ticket avoids the ticket % 900000 collision edge case).
    """

    def __init__(self) -> None:
        self._records: dict[int, Record] = {}

    def has(self, master_ticket: int) -> bool:
        return master_ticket in self._records

    def get(self, master_ticket: int) -> Record | None:
        return self._records.get(master_ticket)

    def add(self, record: Record) -> None:
        self._records[record.master_ticket] = record

    def remove(self, master_ticket: int) -> None:
        self._records.pop(master_ticket, None)

    def all(self) -> list[Record]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_record_table.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/record_table.py manager/tests/test_record_table.py
git commit -m "feat(engine): per-slave record table"
```

---

## Task 8: Baseline age filter + recovery seeding

`is_too_old` is ported from `SlaveSubscriber.mqh::IsTooOld`. `seed_from_recovery` populates a `RecordTable` from the records a slave worker recovers on (re)start, so the first-diff `NEW` handler skips already-copied positions. Together with `is_too_old`, these implement the **copy-recent-opens-at-start** behavior: on a clean start the first diff emits `NEW` for every current master position; `is_too_old` skips old ones, `RecordTable` (seeded by recovery on restart) skips already-copied ones, and the rest are copied.

**Files:**
- Create: `manager/engine/baseline.py`
- Create: `manager/tests/test_baseline.py`

**Interfaces:**
- Consumes: `Record`, `RecordTable` (from `manager.engine.models` / `manager.engine.record_table`).
- Produces: `is_too_old(open_time: int, now: int, max_age_minutes: int) -> bool` and `seed_from_recovery(table: RecordTable, records: Iterable[Record]) -> int` (returns the number of records actually added) in `manager.engine.baseline`.

- [ ] **Step 1: Write the failing test**

`manager/tests/test_baseline.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest manager/tests/test_baseline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager.engine.baseline'`.

- [ ] **Step 3: Write minimal implementation**

`manager/engine/baseline.py`:
```python
from __future__ import annotations

from collections.abc import Iterable

from manager.engine.models import Record
from manager.engine.record_table import RecordTable


def is_too_old(open_time: int, now: int, max_age_minutes: int) -> bool:
    """True if open_time is older than max_age_minutes relative to now.
    Ported from SlaveSubscriber::IsTooOld. An open_time of 0 is never too old."""
    if open_time == 0:
        return False
    return (now - open_time) > max_age_minutes * 60


def seed_from_recovery(table: RecordTable, records: Iterable[Record]) -> int:
    """Populate a RecordTable from records recovered by a slave worker on
    (re)start. Skips master tickets already present (no overwrite). Returns the
    number of records added."""
    added = 0
    for r in records:
        if table.has(r.master_ticket):
            continue
        table.add(r)
        added += 1
    return added
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest manager/tests/test_baseline.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/baseline.py manager/tests/test_baseline.py
git commit -m "feat(engine): age filter + recovery seeding (copy-recent-opens baseline)"
```

---

## Task 9: Engine integration test (composition of all modules)

A single test that composes every Plan 1 module into the NEW → MODIFY → PARTIAL → CLOSE lifecycle, verifying they produce the exact open/modify/partial-close parameters and linkage. This uses a test-local helper that calls the modules in sequence (it is NOT the production copy-loop — that arrives in Plan 2); its purpose is to lock the cross-module behavior.

**Files:**
- Create: `manager/tests/test_engine_integration.py`

**Interfaces:**
- Consumes: `Position`, `Snapshot`, `Event`, `Record`, `BUY` (models); `magic_for`, `encode_comment` (linkage); `SymbolMapper`, `calculate_lots`, `normalize_sltp`, `round_to_tick` (transform); `diff` (snapshot_diff); `RecordTable` (record_table); `is_too_old`, `seed_from_recovery` (baseline).
- Produces: nothing (test only).

- [ ] **Step 1: Write the test**

`manager/tests/test_engine_integration.py`:
```python
"""Integration test composing all Plan 1 engine modules through the
NEW -> MODIFY -> PARTIAL -> CLOSE lifecycle. Uses a test-local helper that
calls the modules in sequence (the production copy-loop arrives in Plan 2)."""

from manager.engine.models import Position, Snapshot, BUY, Record
from manager.engine.linkage import magic_for, encode_comment
from manager.engine.transform import (
    SymbolMapper, calculate_lots, normalize_sltp, round_to_tick,
)
from manager.engine.snapshot_diff import diff
from manager.engine.record_table import RecordTable
from manager.engine.baseline import is_too_old, seed_from_recovery

NOW = 1700000000

# Slave config + slave symbol info (injected, deterministic).
SLAVE_SYMBOL_INFO = {"lot_step": 0.01, "min_lot": 0.01, "max_lot": 100,
                     "tick_size": 0.00001, "digits": 5}
SLAVE_CFG = {"balance": 1000, "step_amount": 100, "step_size": 0.01,
             "max_lot": 10, "max_age_minutes": 10, "normalize_sltp": True}
SLAVE_OPEN_PRICE = 1.20000  # the slave fill price (from the worker's Ack in production)


def _master_pos(ticket, volume=0.5, sl=1.09500, tp=1.10500, open_time=NOW):
    return Position(ticket=ticket, symbol="EURUSD", side=BUY, open_price=1.10000,
                    volume=volume, sl=sl, tp=tp, open_time=open_time, point=0.00001)


def _handle_new(event, table, mapper):
    """Returns the OPEN parameters the slave worker would receive, or None to skip."""
    pos = event.position
    if table.has(pos.ticket):
        return None  # already copied
    if is_too_old(pos.open_time, NOW, SLAVE_CFG["max_age_minutes"]):
        return None  # baseline: too old
    slave_symbol = mapper.resolve(pos.symbol)
    if not slave_symbol:
        return None  # not mappable
    lots = calculate_lots(
        SLAVE_CFG["balance"], SLAVE_CFG["step_amount"], SLAVE_CFG["step_size"],
        SLAVE_CFG["max_lot"], SLAVE_SYMBOL_INFO["lot_step"],
        SLAVE_SYMBOL_INFO["min_lot"], SLAVE_SYMBOL_INFO["max_lot"],
    )
    assert lots > 0.0
    if SLAVE_CFG["normalize_sltp"]:
        slave_sl, slave_tp = normalize_sltp(pos.open_price, pos.sl, pos.tp,
                                            SLAVE_OPEN_PRICE, pos.side)
    else:
        slave_sl, slave_tp = pos.sl, pos.tp
    slave_sl = round_to_tick(slave_sl, SLAVE_SYMBOL_INFO["tick_size"],
                             SLAVE_SYMBOL_INFO["digits"])
    slave_tp = round_to_tick(slave_tp, SLAVE_SYMBOL_INFO["tick_size"],
                             SLAVE_SYMBOL_INFO["digits"])
    comment = encode_comment(pos.ticket, pos.volume, lots)
    magic = magic_for(pos.ticket)
    # simulate the worker's Ack: slave ticket 777, fill at SLAVE_OPEN_PRICE
    table.add(Record(master_ticket=pos.ticket, magic=magic, slave_ticket=777,
                     master_open_volume=pos.volume, slave_open_volume=lots))
    return {"symbol": slave_symbol, "lots": lots, "sl": slave_sl, "tp": slave_tp,
            "magic": magic, "comment": comment}


def test_clean_start_copies_recent_open():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    events = diff(prev=[], curr=[_master_pos(12345)])
    assert [e.kind for e in events] == ["NEW"]
    params = _handle_new(events[0], table, mapper)
    assert params == {
        "symbol": "EURUSD", "lots": 0.10, "sl": 1.19500, "tp": 1.20500,
        "magic": 1012345, "comment": "CPY#12345|MV0.50000000|SV0.10000000",
    }
    assert table.has(12345)


def test_old_position_at_start_is_skipped():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    # 30 minutes old, max_age 10 -> skipped
    events = diff(prev=[], curr=[_master_pos(12345, open_time=NOW - 30 * 60)])
    assert _handle_new(events[0], table, mapper) is None
    assert not table.has(12345)


def test_restart_recovery_prevents_duplicate_open():
    table = RecordTable()
    recovered = [Record(master_ticket=12345, magic=1012345, slave_ticket=777,
                        master_open_volume=0.5, slave_open_volume=0.10)]
    seed_from_recovery(table, recovered)
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    events = diff(prev=[], curr=[_master_pos(12345)])
    assert _handle_new(events[0], table, mapper) is None  # already in table
    assert len(table) == 1


def test_modify_renormalizes_to_slave_open():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    # establish the position first
    _handle_new(diff(prev=[], curr=[_master_pos(12345)])[0], table, mapper)
    # master moves TP from 1.10500 to 1.11000
    events = diff(prev=[_master_pos(12345)], curr=[_master_pos(12345, tp=1.11000)])
    assert [e.kind for e in events] == ["MODIFY"]
    pos = events[0].position
    slave_sl, slave_tp = normalize_sltp(pos.open_price, pos.sl, pos.tp,
                                        SLAVE_OPEN_PRICE, pos.side)
    slave_tp = round_to_tick(slave_tp, SLAVE_SYMBOL_INFO["tick_size"],
                             SLAVE_SYMBOL_INFO["digits"])
    assert slave_tp == 1.21000  # 1.20000 + (1.11000 - 1.10000)


def test_partial_close_math():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    _handle_new(diff(prev=[], curr=[_master_pos(12345)])[0], table, mapper)
    rec = table.get(12345)
    # master volume 0.5 -> 0.3
    events = diff(prev=[_master_pos(12345, volume=0.5)],
                  curr=[_master_pos(12345, volume=0.3)])
    assert [e.kind for e in events] == ["PARTIAL"]
    fraction = 0.3 / rec.master_open_volume       # 0.3 / 0.5 = 0.6
    target = rec.slave_open_volume * fraction      # 0.10 * 0.6 = 0.06
    current_slave_volume = 0.10                     # slave still holds full size
    vol_to_close = current_slave_volume - target    # 0.04
    import math
    vol_to_close = math.floor(vol_to_close / SLAVE_SYMBOL_INFO["lot_step"]) \
        * SLAVE_SYMBOL_INFO["lot_step"]
    assert vol_to_close == 0.04


def test_close_removes_record():
    table = RecordTable()
    mapper = SymbolMapper("EURUSD=EURUSD", exists_check=lambda s: False)
    _handle_new(diff(prev=[], curr=[_master_pos(12345)])[0], table, mapper)
    events = diff(prev=[_master_pos(12345)], curr=[])
    assert [e.kind for e in events] == ["CLOSE"]
    assert events[0].position.ticket == 12345
    # the production copy-loop would call table.remove(12345) after a successful close
    table.remove(12345)
    assert not table.has(12345)
```

- [ ] **Step 2: Run test to verify it fails (or passes if modules are correct)**

Run: `python -m pytest manager/tests/test_engine_integration.py -v`
Expected: PASS (6 tests). (This is a composition test; if any module has a bug surfaced by composition, it will FAIL here — fix the offending module and re-run. If it passes immediately, that is the intended outcome: it confirms the modules compose correctly.)

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest manager/tests -v`
Expected: PASS — all tests across `test_models`, `test_linkage`, `test_transform`, `test_snapshot_diff`, `test_record_table`, `test_baseline`, `test_engine_integration`.

- [ ] **Step 4: Commit**

```bash
git add manager/tests/test_engine_integration.py
git commit -m "test(engine): integration test for NEW/MODIFY/PARTIAL/CLOSE lifecycle"
```

---

## Plan 1 completion check

After Task 9:

- [ ] **Run the full test suite one final time:** `python -m pytest manager/tests -v` — all green.
- [ ] **Confirm no terminal/GUI/IPC imports:** `grep -r "MetaTrader5\|PySide6\|pywin32\|multiprocessing" manager/engine manager/tests` should return nothing. The engine library is pure Python.
- [ ] **Commit the plan-completion marker** (optional): `git commit --allow-empty -m "chore(manager): Plan 1 (engine logic port) complete"`.

## What Plan 1 delivers

A fully unit-tested pure-Python `manager.engine` library: magic/comment linkage, symbol mapping, balance-step lot sizing, raw-distance SL/TP normalization + tick rounding, snapshot diffing, per-slave record table, and the age filter + recovery seeding that implement copy-recent-opens-at-start. No external dependencies beyond Python + pytest. This is the foundation for Plan 2 (IPC + workers + supervisor + copy-loop wiring), which will be written against this real code.

## Self-review (run before handing off)

**Spec coverage (Plan 1 scope):** magic/comment linkage (Task 2 ✓), SymbolMapper (Task 3 ✓), LotSizer (Task 4 ✓), PriceNormalizer + tick rounding (Task 5 ✓), SnapshotDiff NEW/MODIFY/PARTIAL/CLOSE (Task 6 ✓), RecordTable (Task 7 ✓), is_too_old + seed_from_recovery (Task 8 ✓), composition (Task 9 ✓). All engine modules in the spec's project structure that have no terminal/IPC/GUI dependency are covered. `copy_loop.py` is deliberately deferred to Plan 2 (it wires these modules to IPC and the worker adapter).

**Placeholder scan:** none — every step has real test code and real implementation code.

**Type consistency:** `Position`/`Snapshot`/`Event`/`Record` fields are identical across all tasks. `RecordTable` methods (`has`, `get`, `add`, `remove`, `all`, `__len__`) are used consistently in Tasks 7, 8, 9. `diff` returns `list[Event]` with `kind` in {"NEW","MODIFY","PARTIAL","CLOSE"} used consistently in Tasks 6 and 9. `magic_for`/`encode_comment`/`decode_comment` signatures match between Task 2 and Task 9. `calculate_lots`/`normalize_sltp`/`round_to_tick` signatures match between Tasks 4–5 and Task 9.

**Open items deferred to Plan 2:** `engine/copy_loop.py` (the engine thread that consumes `Snapshot` messages, calls `diff`, applies per-slave `RecordTable` + transform + baseline to produce `Command` messages, and updates the `RecordTable` from worker `Ack`s); the `Command`/`Ack`/`Snapshot`/`Status`/`Error`/`RecoveryRecords` IPC message schemas; the `mt5_adapter` interface; the worker subprocess; the supervisor.