# Final Fix 2 Report — MT5 Local Trade Copier

## Summary

Applied the second batch of review fixes across the seven requested files and committed them to a feature branch. All changes are best-effort verified with grep checks and a custom MQL5-aware brace/paren balance script.

## Changes by Fix

### 1. Startup sync handshake (Critical)

**MasterPublisher.mqh**
- Added `Socket *m_syncPull` member (`ZMQ_PULL`) and `ProcessSyncRequests()` helper.
- `Init` now binds the PUB socket to `CopierPort` and a PULL sync socket to `CopierPort + 1`.
- `Deinit` destroys `m_syncPull` before the PUB socket and context.
- `PublishChanges` calls `ProcessSyncRequests()` before scanning positions. On a valid `SYNC_REQUEST`, it clears `m_prevSnapshots` and sends `SYNC_RESPONSE` over PUB.

**SlaveSubscriber.mqh**
- Added `Socket *m_syncPush` (`ZMQ_PUSH`), `m_heartbeatSeconds`, `m_lastHeartbeat`, and `m_heartbeatWarned`.
- `Init` signature extended to take `heartbeatSeconds`.
- After connecting SUB, it connects a PUSH socket to `port + 1`, sets a 1-second send timeout, and sends a `SYNC_REQUEST`.
- `Poll` updates `m_lastHeartbeat` on every valid JSON event and prints `SlaveSubscriber: no heartbeat from master` once when the gap exceeds `2 * heartbeatSeconds`.

**TradeCopier.mq5**
- `g_slave.Init(...)` now passes `HeartbeatSeconds`.

**README.md**
- Added installation step 7: "The sync channel automatically uses `CopierPort + 1`; no extra input is required."

### 2. Retry loop semantics (Important)

**SlaveSubscriber.mqh**
- Changed all four retry loops from `attempt <= m_retryCount` to `attempt < m_retryCount`:
  - `ModifyTrade`
  - `PartialClose`
  - `CloseTrade`
  - `OpenSlaveOrder`

**CopierConfig.mqh**
- Updated `RetryCount` comment to: `Total order-send attempts (including the first attempt)`.

**README.md**
- Updated `RetryCount` table description to match.

### 3. Lot size precision (Important)

**LotSizer.mqh**
- After clamping, `CalculateLots` now computes `lotDigits` from `-MathLog10(lotStep)` and applies `NormalizeDouble(lots, lotDigits)` to strip floating-point noise without upward rounding.

### 4. Preserve partial-close proportion across restarts (Important)

**SlaveSubscriber.mqh**
- `OpenTrade` now stores comments as `CPY#<ticket>|MV<master_volume>|SV<slave_volume>`.
- Startup record rebuild parses the new format; if `MV`/`SV` are present and positive, it restores the original volumes. Otherwise it falls back to using the current position volume for both.

### 5. Tick-size rounding precision (Important)

**SlaveSubscriber.mqh**
- `RoundToTickSize` now queries `SYMBOL_DIGITS` and normalizes to that precision instead of a hardcoded 8.

### 6. GetJsonULong signature (Minor)

**TradeMessage.mqh**
- `GetJsonULong` now takes `ulong &out`.
- `JsonToEvent` calls it directly without a cast.

### 7. Master event coalescing for partial close + modify (Minor)

**MasterPublisher.mqh**
- `PublishChanges` now emits `PARTIAL_CLOSE` if volume decreased, and independently emits `MODIFY_TRADE` if SL or TP changed. Both events can be emitted for the same position in one scan.

## Verification

- `grep` confirmed no `COPPER_CONFIG_MQH` or `g_modeSet` references in `.mqh`, `.mq5`, or `README.md` source files.
- `grep` confirmed all four retry sites use `attempt < m_retryCount` and no `<=` sites remain.
- `grep` confirmed `TradeCopier.mq5` passes `HeartbeatSeconds` to `g_slave.Init`.
- `grep` confirmed `ZMQ_PULL`, `ZMQ_PUSH`, `SYNC_REQUEST`, `SYNC_RESPONSE`, `m_syncPull`, and `m_syncPush` are present.
- A custom Python brace/paren/comment/string checker reported all modified `.mqh` and `.mq5` files balanced.

## Concerns / Remaining Issues

- The MQL5 source depends on the external `Zmq.mqh` binding, which is not part of this repo and therefore cannot be compiled in this environment. The syntax check is structural only; final validation requires MetaEditor.
- `.superpowers/sdd/` contains historical planning/review documents that still mention the old `COPPER_CONFIG_MQH` include guard and `g_modeSet` variable. These are not compiled code and were intentionally left untouched.
- The slave sync request is best-effort: if the master is not yet running when the slave starts, the request is logged as undelivered and the slave relies on normal PUB/SUB messages.
