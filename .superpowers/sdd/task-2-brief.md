# Task 2: Configuration header

**Files:**
- Create: `MQL5/Include/TradeCopier/CopierConfig.mqh`
- Modify: `README.md` (fill Configuration section with inputs)

**Interfaces:**
- Produces: input enums and extern variables used by the main EA and modules.

- [ ] **Step 1: Write configuration header**

```cpp
//+------------------------------------------------------------------+
//|                                           CopierConfig.mqh       |
//|                        MT5 Local Trade Copier Configuration      |
//+------------------------------------------------------------------+
#ifndef COPPER_CONFIG_MQH
#define COPPER_CONFIG_MQH

enum ENUM_COPIER_MODE
{
   COPIER_MASTER,   // publish trades
   COPIER_SLAVE     // subscribe and copy trades
};

input group "=== Copier Mode ==="
input ENUM_COPIER_MODE CopierMode = COPIER_SLAVE; // Run as MASTER or SLAVE
input int              CopierPort = 15555;        // ZeroMQ TCP port
input int              HeartbeatSeconds = 5;      // Master heartbeat interval

input group "=== Master Settings ==="
input int              PublishIntervalMs = 500;   // Trade change scan interval (ms)

input group "=== Slave Settings ==="
input string           SymbolMap = "";            // Symbol mappings: US30=WS30, XAUUSD=GOLD
input double           BalanceStepAmount = 100.0; // Account-currency units per lot step
input double           BalanceStepSize   = 0.01;  // Lot size added per balance step
input double           MaxLotSize        = 10.0;  // Hard lot-size cap
input int              MaxTradeAgeMinutes = 30;   // Ignore master trades older than this on sync
input bool             NormalizeSLTPUsingPoints = true; // Convert SL/TP via point distances
input int              RetryCount = 3;            // Order-send retries on temporary failure
input int              RetryDelayMs = 500;        // Delay between retries (ms)

// Magic number base for copied trades. Slave ticket = base + (master_ticket % 900000)
const int MAGIC_BASE = 1000000;

#endif
```

- [ ] **Step 2: Update README Configuration section**

Add the full inputs table (copy exact names/defaults from the header) under `## Configuration` in `README.md`.

- [ ] **Step 3: Commit**

```bash
git add MQL5/Include/TradeCopier/CopierConfig.mqh README.md
git commit -m "feat: add copier configuration header and README inputs table

Co-Authored-By: Claude <noreply@anthropic.com>"
```
