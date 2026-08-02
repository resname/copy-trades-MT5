# MT5 Local Trade Copier

A single MetaTrader 5 Expert Advisor that copies trades from a master MT5 account to one or more slave MT5 accounts running on the same machine.

## Features
- Master/Slave dual mode
- File-based local communication via shared snapshot
- Manual symbol translation
- Balance-step lot sizing
- Point-normalized SL/TP mirroring
- Full trade lifecycle mirroring (open, modify SL/TP, partial close, close)
- Multi-slave support from a single master

## Installation

1. Copy `MQL5/Experts/TradeCopier/TradeCopier.mq5` and the `MQL5/Include/TradeCopier/*.mqh` files into your MetaTrader 5 data folder.
2. Open `TradeCopier.mq5` in MetaEditor and compile (F7).
3. Attach the EA to a chart on the master account; set `CopierMode` to `MASTER`.
4. Attach the EA to a chart on each slave account; set `CopierMode` to `SLAVE` and configure symbol mapping / lot sizing.
5. Set `SharedDataPath` to the same folder on the master and on every slave, for example `TradeCopier\\Shared\\` (relative to the terminal's `MQL5/Common/Files` folder). All MT5 terminals must be running on the same machine, must use the same MetaTrader 5 installation, and must read the same snapshot file.

You can attach the slave EA to any number of charts/terminals. All slaves must use the same `SharedDataPath`. Each slave has its own `SymbolMap`, `BalanceStepAmount`, and `MaxLotSize` settings.

## Configuration

### Copier Mode

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| CopierMode | ENUM_COPIER_MODE | COPIER_SLAVE | Run as MASTER or SLAVE |
| SharedDataPath | string | `TradeCopier\\` | Shared folder for the snapshot file (relative to Common/Files or absolute) |
| MasterSnapshotIntervalMs | int | 200 | Master snapshot write interval (ms) |
| SlavePollIntervalMs | int | 257 | Slave snapshot read interval (ms), desynchronized from master |
| HeartbeatSeconds | int | 5 | Maximum age of heartbeat before slave warns |

### Slave Settings

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| SymbolMap | string | "" | Symbol mappings: US30=WS30, XAUUSD=GOLD |
| BalanceStepAmount | double | 100.0 | Account-currency units per lot step |
| BalanceStepSize | double | 0.01 | Lot size added per balance step |
| MaxLotSize | double | 10.0 | Hard lot-size cap |
| MaxTradeAgeMinutes | int | 30 | Ignore master trades older than this on sync |
| NormalizeSLTPByPriceDistance | bool | true | Convert SL/TP via raw price distance |
| RetryCount | int | 3 | Total order-send attempts (including the first attempt) |
| RetryDelayMs | int | 500 | Delay between retries (ms) |

MAGIC_BASE is fixed at `1000000`. The copied position's magic number is computed as `MAGIC_BASE + (master_ticket % 900000)`.

### Example slave configuration

| Input | Value | Result |
|-------|-------|--------|
| `SymbolMap` | `US30=WS30, XAUUSD=GOLD` | master US30 -> slave WS30 |
| `BalanceStepAmount` | `100.0` | one lot step per €100 balance |
| `BalanceStepSize` | `0.01` | each step adds 0.01 lots |
| `MaxLotSize` | `10.0` | never exceed 10 lots |
| `MaxTradeAgeMinutes` | `30` | ignore trades older than 30 min on startup |
| `NormalizeSLTPByPriceDistance` | `true` | convert SL/TP using raw price distance |

With a €5,000 balance and the values above, the slave lot size will be `floor(5000 / 100) * 0.01 = 0.5` lots.

## Feature Test Checklist

Use two demo accounts on the same PC. Check each item once it behaves as described.

### Transport & Setup
- [ ] Compile `TradeCopier.mq5` in MetaEditor without errors.
- [ ] Attach the EA to a master chart with `CopierMode = MASTER`.
- [ ] Attach the EA to a slave chart with `CopierMode = SLAVE`.
- [ ] Verify the file `TradeCopier.snapshot.json` appears in the configured `SharedDataPath` after the first master timer tick.
- [ ] Confirm both master and slave use the exact same `SharedDataPath`.

### Trade Lifecycle Mirroring
- [ ] Open a market order on the master; the slave opens the corresponding position within ~1 second.
- [ ] Modify SL/TP on the master; the slave position's SL/TP updates to the same raw price distance.
- [ ] Partially close the master position; the slave closes the same fraction of its copied position.
- [ ] Fully close the master position; the slave position closes.
- [ ] Close the slave position manually while the master position stays open; the slave does **not** re-open it.

### Symbol Translation
- [ ] Set `SymbolMap = US30=WS30` and trade `US30` on the master; the slave opens `WS30`.
- [ ] Trade a symbol that exists on both accounts and is **not** in `SymbolMap`; the slave uses the same symbol name.
- [ ] Trade a symbol that does **not** exist on the slave account; the slave skips the trade and logs an error.

### Lot Sizing
- [ ] Change the slave account balance; the copied lot size changes according to `BalanceStepAmount` and `BalanceStepSize`.
- [ ] Set the slave balance high enough to exceed `MaxLotSize`; the copied lot size is capped at `MaxLotSize`.
- [ ] Verify lot sizes are rounded down to the slave symbol's `LotsStep`.

### SL/TP Normalization
- [ ] With `NormalizeSLTPByPriceDistance = true`, master and slave symbols with different decimal precision (e.g. master `US30` at 52444.31, slave `WS30` at 52444) still get the same raw price-distance SL/TP.
- [ ] With `NormalizeSLTPByPriceDistance = false`, the slave copies the literal master SL/TP prices.

### Restart Recovery
- [ ] Restart the slave EA with an open master position newer than `MaxTradeAgeMinutes`; the slave resyncs the existing copied position without creating a duplicate.
- [ ] Restart the slave EA with an open master position older than `MaxTradeAgeMinutes`; the slave ignores it and does not copy.

### Multi-Slave
- [ ] Attach a second slave EA to another chart/terminal with the same `SharedDataPath` but a different `SymbolMap`; both slaves mirror the same master trade independently.
- [ ] Close the copied position on one slave; the other slave and the master remain unaffected.

### Heartbeat & Intervals
- [ ] Stop the master EA; after more than `HeartbeatSeconds * 2`, the slave logs a missing-heartbeat warning.
- [ ] Restart the master EA; the slave heartbeat warning stops after the next snapshot is read.
- [ ] Change `MasterSnapshotIntervalMs` and `SlavePollIntervalMs`; observe that the file write and read intervals follow the new values.
