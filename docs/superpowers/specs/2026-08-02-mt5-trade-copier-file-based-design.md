# MT5 Trade Copier — File-Based Design

## Context

The existing MT5 Trade Copier uses ZeroMQ (`<Zmq\Zmq.mqh>`) for local master-to-slave communication. That include is not part of the standard MQL5 distribution and can be hard to obtain. This design replaces ZeroMQ with a simple, file-based IPC mechanism that works with multiple slaves on the same machine without external dependencies.

## Goals

1. Remove every dependency on `Zmq.mqh` / external DLLs.
2. Copy trades from one master MT5 terminal to one or more slave terminals on the same PC.
3. Preserve existing features: symbol mapping, balance-step lot sizing, raw price-distance SL/TP mirroring, full lifecycle mirroring (open, modify, partial close, close), and restart recovery.
4. Keep latency low enough for trade copying (target < 1 s end-to-end).
5. Make the shared data path configurable so multiple MT5 installations can point to the same folder.

## Non-Goals

- Cross-device / network copying. The file-based transport is explicitly localhost-only.
- Replacing the trade-logic modules. `SymbolMapper`, `LotSizer`, `PriceNormalizer`, and `TradeMessage` keep their responsibilities; only the transport changes.

## Architecture

```
+--------+     writes      +-----------------------------+
| Master | --------------> | TradeCopier.snapshot.json   |
|  EA    |                 | (atomic rename via .tmp)    |
+--------+                 +-----------------------------+
                                  ^
                                  | reads
                    +-------------+-------------+
                    |           |             |
                 +-------+   +-------+     +-------+
                 | Slave |   | Slave | ... | Slave |
                 |  EA   |   |  EA   |     |  EA   |
                 +-------+   +-------+     +-------+
```

- The **master** scans open positions and writes a JSON snapshot file on every master timer tick (configurable, default 200 ms).
- The **slave(s)** read the same snapshot file on every slave timer tick (configurable, default 257 ms), compare it with the previous known state, and derive lifecycle events from the diff.
- Master and slave intervals are independent and intentionally desynchronized by default (200 ms vs. 257 ms) so that repeated slave polls do not consistently collide with master writes.
- Reading and writing happen in a configurable shared directory, e.g. `C:\TradeCopier\Shared\`.

## File Format

Single file: `TradeCopier.snapshot.json`

```json
{
  "timestamp": 1722600000,
  "heartbeat": 1722600000,
  "positions": [
    {
      "ticket": 123456789,
      "symbol": "US30",
      "side": 0,
      "open_price": 52444.31,
      "volume": 0.5,
      "sl": 51444.31,
      "tp": 53444.31,
      "open_time": 1722599900,
      "point": 0.01,
      "comment": ""
    }
  ]
}
```

- `timestamp`: epoch seconds (UTC) of the snapshot generation.
- `heartbeat`: same as `timestamp`; used by slaves to detect a stalled master.
- `positions`: array of all open positions the master currently holds. Each position carries the full trade-event payload (`ticket`, `symbol`, `side`, `open_price`, `volume`, `sl`, `tp`, `open_time`, `point`, `comment`) so the slave can reconstruct `STradeEvent` objects without accessing the master account.
- `side`: `0` = buy, `1` = sell (`ENUM_POSITION_TYPE` values).
- `comment`: optional, kept for debugging and future restart recovery.

## Atomic Writes

To prevent slaves from reading a half-written file, the master writes to a temporary file first, then renames it:

1. Open `TradeCopier.snapshot.json.tmp` with `FILE_WRITE|FILE_TXT|FILE_COMMON`.
2. Write the complete JSON payload.
3. Close the file.
4. Move/rename the `.tmp` file to `TradeCopier.snapshot.json`.

MQL5 has no native atomic rename, but `FileMove` can move a file within the same filesystem. If atomicity is not guaranteed by the terminal, the small file size and fast write make partial reads extremely unlikely. Slaves that fail to parse the file retry on the next timer tick.

## Slave Diff Logic

The slave maintains a local copy of the previous snapshot (`m_prevSnapshots`). On each poll:

1. Read `TradeCopier.snapshot.json`.
2. If the file cannot be opened or parsed, log once and retry next tick.
3. Check the snapshot's own `heartbeat` timestamp. If it has not changed for longer than `HeartbeatSeconds * 2`, log a warning. (Do not rely only on file-read success, because a stale but still-readable file would not reveal a dead master.)
4. Compare the new `positions` array with `m_prevSnapshots`:
   - **NEW_TRADE:** a ticket exists in the new snapshot but not in `m_prevSnapshots`.
   - **MODIFY_TRADE:** a ticket exists in both, but `sl` or `tp` differ.
   - **PARTIAL_CLOSE:** a ticket exists in both, but `volume` decreased.
   - **CLOSE_TRADE:** a ticket exists in `m_prevSnapshots` but not in the new snapshot.
5. Update `m_prevSnapshots` to the new state.
6. Process events through the existing trade-execution layer.

## Startup Behavior

When a slave starts:

1. It reads the current snapshot.
2. It treats the first snapshot as the baseline: existing master positions are loaded into `m_prevSnapshots` but do **not** generate `NEW_TRADE` events.
3. Positions older than `MaxTradeAgeMinutes` are excluded from the baseline using each position's `open_time` field.
4. The slave scans its own open positions for any with a `CPY#<ticket>` comment and rebuilds `m_records` so restarts do not create duplicates.

Because the snapshot is refreshed at least every `MasterSnapshotIntervalMs` (default 200 ms), no explicit sync request is required.

## Multi-Slave Behavior

Multiple slaves can read the same snapshot file concurrently. The master only writes; it never blocks on readers. Slaves do not write the snapshot, so there is no writer contention. Slaves are independent: each has its own symbol mapping, balance-step lot sizing, and broker settings.

## Error Handling

- **Master write failure:** log the error, do not update `m_prevSnapshots`; retry on next timer tick.
- **Slave read failure:** log once, keep the previous state, retry on next timer tick.
- **Parse failure:** log the malformed payload, ignore the tick.
- **Missing shared directory:** create it on `Init` if possible; if creation fails, return `INIT_FAILED`.

## Configuration Inputs

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `SharedDataPath` | string | `MQL5/Files/TradeCopier/` | Directory where the snapshot file is written/read. |
| `MasterSnapshotIntervalMs` | int | 200 | Interval at which the master writes the snapshot file. |
| `SlavePollIntervalMs` | int | 257 | Interval at which each slave reads the snapshot file. Desynchronized from the master by default to reduce collision likelihood. |
| `HeartbeatSeconds` | int | 5 | Maximum expected age of `heartbeat` before the slave warns. |

Removed inputs:
- `CopierPort`
- All ZeroMQ-related settings (none existed beyond the port).

## Modules to Change

- `MQL5/Experts/TradeCopier/TradeCopier.mq5`: remove any ZMQ-specific init/teardown.
- `MQL5/Include/TradeCopier/CopierConfig.mqh`: replace `CopierPort` with `SharedDataPath`, adjust defaults.
- `MQL5/Include/TradeCopier/MasterPublisher.mqh`: replace `Context`/`Socket` with file-writing logic.
- `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`: replace `Context`/`Socket` with file-reading/diffing logic.
- `README.md`: update installation/usage instructions, remove ZeroMQ requirement.

## Modules That Stay Unchanged

- `SymbolMapper.mqh`
- `LotSizer.mqh`
- `PriceNormalizer.mqh`
- `TradeMessage.mqh` (event structure and JSON helpers)

## Trade-offs

| Aspect | ZeroMQ (old) | File-based (new) |
|--------|--------------|--------------------|
| External dependency | `Zmq.mqh` required | None |
| Latency | ~millisecond push | ~`SlavePollIntervalMs` poll |
| Multi-slave | Native PUB/SUB | Native shared file |
| Cross-device | Possible with TCP | Not supported |
| Debuggability | Binary wire protocol | Human-readable JSON file |
| Setup complexity | Install/include ZMQ | Configure shared path |

The 250 ms poll latency is acceptable for trade copying because trades do not require microsecond precision.

## Future Considerations

If cross-device copying is needed later, a separate lightweight bridge could be added (e.g. a Python TCP relay), but that is out of scope for this design.
