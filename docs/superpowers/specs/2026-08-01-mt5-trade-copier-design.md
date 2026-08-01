# MT5 Local Trade Copier — Design Spec

**Date:** 2026-08-01  
**Status:** Approved for implementation  
**Project:** `/home/a/copy-trades-MT5`

## 1. Overview

Build a single MetaTrader 5 Expert Advisor (EA) that copies trades from one MT5 account (master) to another MT5 account (slave) running on the same machine. The EA runs in one of two modes selected by an input parameter:

- `MASTER` — publishes trade events to a ZeroMQ socket.
- `SLAVE` — subscribes to the trade stream, translates symbols, recalculates lot sizes, and mirrors the trades.

Key capabilities:
- Manual symbol translation via comma-separated pairs (e.g. `US30=WS30, XAUUSD=GOLD`).
- Balance-step lot sizing: `floor(balance / BalanceStepAmount) * BalanceStepSize`, rounded down to the broker's lot step and capped at `MaxLotSize`.
- SL/TP normalization using point distances so different decimal precisions (e.g. `US30` 2-decimal vs `WS30` 0-decimal) do not corrupt copied levels.
- Full trade lifecycle mirroring: new trades, SL/TP modifications, partial closes, and full closes.
- Startup resync with `MaxTradeAgeMinutes` filter to avoid copying legacy long-term positions.

## 2. Architecture

```
+---------------+       ZeroMQ TCP        +---------------+
|  MT5 Master   |  <trade events>  ----->  |  MT5 Slave    |
|  (CopierMode=  |      heartbeat          |  (CopierMode= |
|   MASTER)     |                         |   SLAVE)      |
+---------------+                         +---------------+
       ^                                           ^
       | monitors open positions                   | maps symbols
       | publishes changes                         | sizes lots
       | heartbeat                                 | executes orders
```

### Master side
- Dumb publisher: only reports what happened on the master account.
- No symbol mapping, no risk logic.
- Sends `NEW_TRADE`, `MODIFY_TRADE`, `PARTIAL_CLOSE`, `CLOSE_TRADE`, and periodic `HEARTBEAT` messages.

### Slave side
- Subscribes to master events and maps `master_ticket -> slave_position_ticket`.
- Applies symbol translation and balance-step lot sizing before placing orders.
- Normalizes SL/TP using per-symbol point sizes.
- Requests a full sync on startup and filters out trades older than `MaxTradeAgeMinutes`.

## 3. Components

### 3.1. ZeroMQ transport
- Library: standard MQL5 ZMQ binding (`<zmq\zmq.mqh>`).
- Master binds to `tcp://127.0.0.1:<CopierPort>`.
- Slave connects to the same address.
- Messages are JSON strings containing event type and payload fields.

### 3.2. Event schema
All events contain at minimum:
- `event`: string — one of `NEW_TRADE`, `MODIFY_TRADE`, `PARTIAL_CLOSE`, `CLOSE_TRADE`, `HEARTBEAT`, `SYNC_REQUEST`, `SYNC_RESPONSE`
- `timestamp`: integer — milliseconds since epoch (MQL5 `TimeLocal()` style)
- `magic`: integer — derived from master ticket for slave-side identification

Trade events additionally carry:
- `master_ticket`: ulong
- `symbol`: string
- `side`: integer (`POSITION_TYPE_BUY` / `POSITION_TYPE_SELL`)
- `open_price`: double
- `volume`: double
- `sl`: double (or `null` if none)
- `tp`: double (or `null` if none)
- `open_time`: datetime
- `point`: double — master symbol's `SYMBOL_POINT`
- `comment`: string — original master position comment

### 3.3. Symbol resolver
1. Parse `SymbolMap` into a `CArrayString` / dictionary.
2. For an incoming master symbol:
   - If an explicit mapping exists, use the mapped slave symbol.
   - Else try the master symbol name directly.
   - If neither exists on the slave account, log error and skip.

### 3.4. Lot sizer
Formula:
```
steps = floor(AccountInfoDouble(ACCOUNT_BALANCE) / BalanceStepAmount)
lots  = steps * BalanceStepSize
lots  = NormalizeLots(lots, symbol)  // round DOWN to SYMBOL_VOLUME_STEP
lots  = min(lots, MaxLotSize)
lots  = max(lots, SYMBOL_VOLUME_MIN)
```

### 3.5. SL/TP normalizer
Only applied when `NormalizeSLTPUsingPoints = true`.

For buy positions:
```
sl_distance_points = (master_open - master_sl) / master_point
tp_distance_points = (master_tp - master_open) / master_point
slave_sl = slave_open - (sl_distance_points * slave_point)
slave_tp = slave_open + (tp_distance_points * slave_point)
```

For sell positions the signs are reversed. If master `sl` or `tp` is zero/`null`, slave gets none.

This makes the copier robust against broker-specific decimal precision changes because it never copies absolute prices — it copies point distances using each broker's live `SYMBOL_POINT`.

### 3.6. Position mapper
- Each copied trade receives a unique `magic` number derived deterministically from the master ticket (e.g. `1000000 + master_ticket % 900000`).
- Slave position comment is set to `CPY#<master_ticket>` for human-readable matching.
- On startup the slave scans its open positions by magic number (and comment fallback) to rebuild the map.

## 4. Inputs / Configuration

### Shared
| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `CopierMode` | enum | `SLAVE` | `MASTER` or `SLAVE` |
| `CopierPort` | int | `15555` | ZeroMQ TCP port |
| `HeartbeatSeconds` | int | `5` | Master heartbeat interval |

### Master only
| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `PublishIntervalMs` | int | `500` | How often master scans positions for changes |

### Slave only
| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `SymbolMap` | string | `""` | Comma-separated pairs: `US30=WS30, XAUUSD=GOLD` |
| `BalanceStepAmount` | double | `100.0` | Account-currency units per lot step |
| `BalanceStepSize` | double | `0.01` | Lot size added per balance step |
| `MaxLotSize` | double | `10.0` | Hard lot-size cap |
| `MaxTradeAgeMinutes` | int | `30` | Ignore master trades older than this on startup sync |
| `NormalizeSLTPUsingPoints` | bool | `true` | Convert SL/TP via point distances |
| `RetryCount` | int | `3` | Order-send retries on temporary failure |
| `RetryDelayMs` | int | `500` | Delay between retries |

## 5. Error Handling & Safety

- **Lost connection**: slave logs warning and leaves existing positions untouched; it does not auto-close them.
- **Unknown master ticket**: event is logged and ignored.
- **Missing symbol mapping**: slave first tries the master symbol name; if it does not exist, the trade is skipped and an alert is raised.
- **Invalid lot size**: rounded down to lot step, clamped between `SYMBOL_VOLUME_MIN` and `SYMBOL_VOLUME_MAX`, capped at `MaxLotSize`.
- **Insufficient margin / order failure**: retried up to `RetryCount` times; if still failing, logged as an error.
- **All actions are logged** to the MT5 Experts log for auditability.

## 6. Testing Strategy

### 6.1. Single-terminal smoke tests
- EA compiles cleanly on both master and slave mode selections.
- Input parsing: `SymbolMap` correctly builds the dictionary (edge cases: spaces, empty pairs, duplicates).
- Lot-sizing helper returns expected values for sample balances.
- Point-normalization helper returns expected slave SL/TP for sample price precisions.
- Symbol resolver falls back to master name when no mapping exists.

### 6.2. Two-terminal manual tests (demo accounts)
A checklist to be added to `README.md`:
1. Install the same EA on two demo charts, one in `MASTER` mode and one in `SLAVE` mode.
2. Open a market order on the master; verify the slave opens the corresponding position within ~1 second.
3. Modify SL/TP on the master; verify the slave position's SL/TP update.
4. Partially close the master position; verify the slave closes the same fraction.
5. Fully close the master position; verify the slave position closes.
6. Restart the slave EA with an open master position older than `MaxTradeAgeMinutes`; verify it is **not** copied.
7. Restart the slave EA with an open master position newer than `MaxTradeAgeMinutes`; verify it is copied/resynced.
8. Use a mapped symbol (e.g. `US30=WS30`) and confirm the slave uses `WS30`.
9. Use an unmapped symbol that exists on both accounts; confirm the slave uses the same name.
10. Use an unmapped symbol that does **not** exist on the slave; confirm the trade is skipped with an error log.
11. Verify lot sizing changes when the slave account balance changes.
12. Verify `MaxLotSize` cap is respected on large balances.
13. Verify SL/TP point normalization works when master and slave quote different decimal precisions.

## 7. Open Questions / Future Enhancements

- GUI symbol mapper (v2).
- Equity-based lot sizing option (currently uses balance).
- Multiple slaves from one master (already supported by ZeroMQ pub/sub, but not explicitly tested).
- Copying pending orders (limit/stop) — currently out of scope; only market positions.

## 8. Design Decisions Log

| Decision | Chosen Option | Rationale |
|----------|---------------|-----------|
| Packaging | Single dual-mode EA | Easier to maintain and deploy |
| Communication | ZeroMQ localhost | Fast, pub/sub, low latency |
| Symbol mapping | Comma-separated pairs | Simple, saved with chart template |
| Lot sizing | Balance-step (`floor(balance/step) * stepsize`) | Exact user requirement |
| Lot rounding | Round down | Avoids exceeding implied size |
| SL/TP copying | Point-distance normalization | Robust against broker precision changes |
| Trade tracking | Magic number + comment | Fast programmatic + human-readable fallback |
| Startup sync | Resync with age filter | Survives restarts without copying legacy trades |
| Retry logic | Configurable count/delay | Handles transient order-send failures |
