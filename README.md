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
(TODO: fill in after Task 10)

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

## Manual Testing Checklist
(TODO: fill in after Task 11)
