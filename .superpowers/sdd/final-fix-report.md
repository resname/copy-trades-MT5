# MT5 Local Trade Copier Final Fix Report

## Fixes Applied

### 1. Slave startup position scan (Critical)
**File:** `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`  
**Location:** `CSlaveSubscriber::Init`, after ZMQ subscribe (lines 111-148)

After connecting to ZeroMQ, the slave now scans `PositionsTotal()` open positions and rebuilds `m_records` for any copied positions:
- Selects only positions whose magic number is `>= MAGIC_BASE`.
- Parses the master ticket from the position comment using the `CPY#<ticket>` prefix.
- Adds a record with `magic`, `slave_ticket`, `master_open_volume` (current volume) and `slave_open_volume` (current volume).
- Prints `SlaveSubscriber: rebuilt N copied position record(s) from open positions` when any records are rebuilt.

This prevents duplicate trades if the slave EA is restarted while copied positions remain open.

### 2. JSON string escaping / unescaping (Important)
**File:** `MQL5/Include/TradeCopier/TradeMessage.mqh`

- `CTradeMessage::JsonString` (lines 201-207) now escapes `\` → `\\` and `"` → `\"` before wrapping the value in double quotes.
- `GetJsonString` (lines 54-95) now:
  - Finds the closing quote that is not escaped by backslashes.
  - Unescapes in reverse order of escaping: `\"` → `"`, then `\\` → `\`.

This makes the trade-message JSON robust for symbols and comments containing backslashes or quotes.

### 3. Use broker time for trade-age check (Important)
**File:** `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`  
**Line:** 399

Changed `datetime now = TimeLocal();` to `datetime now = TimeCurrent();` in `CSlaveSubscriber::IsTooOld` so the age check uses the broker/server time instead of the local PC clock.

### 4. Check `RoundToTickSize` return value (Important)
**File:** `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`

- `OpenTrade` (lines 249-253): after rounding SL/TP, aborts the open and prints a warning if either `RoundToTickSize` call returns `false`.
- `ModifyTrade` (lines 310-314): same check and abort before calling `PositionModify`.

### 5. Fix `CopierConfig.mqh` include guard (Important)
**File:** `MQL5/Include/TradeCopier/CopierConfig.mqh`  
**Lines:** 5-6

Changed `#ifndef COPPER_CONFIG_MQH` / `#define COPPER_CONFIG_MQH` to `#ifndef COPIER_CONFIG_MQH` / `#define COPIER_CONFIG_MQH`.

### 6. Fix README `MAGIC_BASE` wording (Important)
**File:** `README.md`  
**Line:** 52

Changed the description from "slave ticket is computed as" to "copied position's magic number is computed as". Removed the stray duplicate sentence after the inputs table so the Configuration section stays scoped.

### 7. Reorder `TradeMessage.mqh` helpers (Minor)
**File:** `MQL5/Include/TradeCopier/TradeMessage.mqh`

Moved the free helper function declarations above the `CTradeMessage` class (lines 31-36) and their definitions above `CTradeMessage::JsonToEvent`, so all helpers are defined before use.

## Verification

### Syntax / structure checks
- Verified brace, parenthesis and bracket balance for all three modified `.mqh` files with a state-machine parser that correctly handles strings, line comments and block comments:
  - `SlaveSubscriber.mqh`: balanced
  - `TradeMessage.mqh`: balanced
  - `CopierConfig.mqh`: balanced

### JSON logic stub test
- Created a temporary C++ stub at `/tmp/mql5_stubs/test_trademessage.cpp` that mocks the MQL5 string helpers and compiles/runs the new `JsonString` and `GetJsonString` implementations.
- Result: roundtrip of a value containing both backslash and double-quote (`x\y"z`) succeeds. Plain strings without escapes still parse correctly.
- The stub was not committed.

### Grep checks
- `grep -rn "COPPER_CONFIG_MQH" /home/a/copy-trades-MT5/`
  - No occurrences remain in source files (`.mqh`, `.mq5`). Historical references remain only in plan/review documents under `.superpowers/sdd/` and `docs/superpowers/plans/`.
- `grep -rn "g_modeSet" /home/a/copy-trades-MT5/`
  - No occurrences remain in source files. References remain only in historical plan/review documents.

## Concerns / Remaining Issues

- **No live MQL5 compilation:** This environment does not include MetaEditor, so the headers could not be compiled in the target language. The fixes were validated structurally and with a C++ stub for the JSON logic. A final compile in MetaEditor before deployment is recommended.
- **Startup scan assumes `CPY#<ticket>` comment prefix:** If the broker truncates or overwrites position comments, the rebuild will miss those positions and a duplicate copy could still occur on restart. This matches the existing EA behavior for newly opened copied trades.
- **JSON extraction robustness:** `GetJsonString` now skips escaped quotes when locating the closing delimiter, which is a small functional extension beyond the two literal unescape replacements requested. This extension is necessary for the escaping to actually round-trip values containing quotes.
