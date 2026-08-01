# MT5 Local Trade Copier

A single MetaTrader 5 Expert Advisor that copies trades from a master MT5 account to a slave MT5 account running on the same machine.

## Features
- Master/Slave dual mode
- ZeroMQ-based local communication
- Manual symbol translation
- Balance-step lot sizing
- Point-normalized SL/TP mirroring
- Full trade lifecycle mirroring (open, modify SL/TP, partial close, close)

## Installation

1. Copy `MQL5/Experts/TradeCopier/TradeCopier.mq5` and the `MQL5/Include/TradeCopier/*.mqh` files into your MetaTrader 5 data folder.
2. Make sure the MQL5 ZeroMQ binding (`MQL5/Include/Zmq/Zmq.mqh`) is installed.
   - If missing, install the "ZeroMQ" library from the MetaTrader Market or copy a known-good ZMQ include set.
3. Open `TradeCopier.mq5` in MetaEditor and compile (F7).
4. Attach the EA to a chart on the master account; set `CopierMode` to `MASTER`.
5. Attach the EA to a chart on the slave account; set `CopierMode` to `SLAVE` and configure symbol mapping / lot sizing.
6. Both MT5 terminals must be running on the same machine.

## Configuration

### Copier Mode

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| CopierMode | ENUM_COPIER_MODE | COPIER_SLAVE | Run as MASTER or SLAVE |
| CopierPort | int | 15555 | ZeroMQ TCP port |
| HeartbeatSeconds | int | 5 | Master heartbeat interval |

### Master Settings

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| PublishIntervalMs | int | 500 | Trade change scan interval (ms) |

### Slave Settings

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| SymbolMap | string | "" | Symbol mappings: US30=WS30, XAUUSD=GOLD |
| BalanceStepAmount | double | 100.0 | Account-currency units per lot step |
| BalanceStepSize | double | 0.01 | Lot size added per balance step |
| MaxLotSize | double | 10.0 | Hard lot-size cap |
| MaxTradeAgeMinutes | int | 30 | Ignore master trades older than this on sync |
| NormalizeSLTPUsingPoints | bool | true | Convert SL/TP via point distances |
| RetryCount | int | 3 | Order-send retries on temporary failure |
| RetryDelayMs | int | 500 | Delay between retries (ms) |

MAGIC_BASE is fixed at `1000000`. The slave ticket is computed as `MAGIC_BASE + (master_ticket % 900000)`.

### Example slave configuration

| Input | Value | Result |
|-------|-------|--------|
| `SymbolMap` | `US30=WS30, XAUUSD=GOLD` | master US30 -> slave WS30 |
| `BalanceStepAmount` | `100.0` | one lot step per €100 balance |
| `BalanceStepSize` | `0.01` | each step adds 0.01 lots |
| `MaxLotSize` | `10.0` | never exceed 10 lots |
| `MaxTradeAgeMinutes` | `30` | ignore trades older than 30 min on startup |
| `NormalizeSLTPUsingPoints` | `true` | convert SL/TP using point distances |

With a €5,000 balance and the values above, the slave lot size will be `floor(5000 / 100) * 0.01 = 0.5` lots.

## Manual Testing Checklist
(TODO: fill in after Task 11)
