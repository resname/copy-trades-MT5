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

## How it works

The master EA writes a JSON snapshot of its open positions to a shared folder at a configurable interval. Every slave EA polls the same snapshot file and applies the differences to its own account. Because the transport is a plain file, any number of slave terminals can read the same master snapshot simultaneously.

## Installation

1. Copy `MQL5/Experts/TradeCopier/TradeCopier.mq5` and the `MQL5/Include/TradeCopier/*.mqh` files into your MetaTrader 5 data folder.
2. Open `TradeCopier.mq5` in MetaEditor and compile (F7).
3. Attach the EA to a chart on the master account; set `CopierMode` to `MASTER`.
4. Attach the EA to a chart on each slave account; set `CopierMode` to `SLAVE` and configure symbol mapping / lot sizing.
5. All MT5 terminals must be running on the same machine and must use the same `SharedDataPath` so they read the same snapshot file.

## Multi-slave setup

1. Set the same `SharedDataPath` on the master and on every slave (for example `TradeCopier\`).
2. Keep the master `HeartbeatSeconds` lower than or equal to each slave's `HeartbeatSeconds` so the slaves do not time out the master.
3. Stagger slave poll intervals if you run many slaves on the same machine to avoid hammering the file system. The default `SlavePollIntervalMs` is already desynchronized from the master write interval.
4. Each slave uses its own account balance for lot sizing and may have its own `SymbolMap`, so the same master trade can be copied with different symbols or sizes on different accounts.

## Configuration

### Copier Mode

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| CopierMode | ENUM_COPIER_MODE | COPIER_SLAVE | Run as MASTER or SLAVE |
| HeartbeatSeconds | int | 5 | Master heartbeat interval |

### Transport Settings

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| SharedDataPath | string | `TradeCopier\` | Shared folder for the snapshot file (relative to Common/Files or absolute) |
| MasterSnapshotIntervalMs | int | 200 | Master snapshot write interval (ms) |
| SlavePollIntervalMs | int | 257 | Slave snapshot read interval (ms), desynchronized from master |

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

## Manual Testing Checklist (use two demo accounts)

- [ ] Install the same EA on two demo charts: one `MASTER`, one `SLAVE`.
- [ ] Open a market order on the master; verify the slave opens the corresponding position within ~1 second.
- [ ] Modify SL/TP on the master; verify the slave position's SL/TP update.
- [ ] Partially close the master position; verify the slave closes the same fraction.
- [ ] Fully close the master position; verify the slave position closes.
- [ ] Restart the slave EA with an open master position older than `MaxTradeAgeMinutes`; verify it is **not** copied.
- [ ] Restart the slave EA with an open master position newer than `MaxTradeAgeMinutes`; verify it is copied/resynced.
- [ ] Use a mapped symbol (e.g. `US30=WS30`) and confirm the slave uses `WS30`.
- [ ] Use an unmapped symbol that exists on both accounts; confirm the slave uses the same name.
- [ ] Use an unmapped symbol that does **not** exist on the slave; confirm the trade is skipped with an error log.
- [ ] Verify lot sizing changes when the slave account balance changes.
- [ ] Verify `MaxLotSize` cap is respected on large balances.
- [ ] Verify SL/TP price-distance normalization works when master and slave quote different decimal precisions (e.g. master `US30` at 52444.31, slave `WS30` at 52444).
