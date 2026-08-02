//+------------------------------------------------------------------+
//|                                           CopierConfig.mqh       |
//|                        MT5 Local Trade Copier Configuration      |
//+------------------------------------------------------------------+
#ifndef COPIER_CONFIG_MQH
#define COPIER_CONFIG_MQH

enum ENUM_COPIER_MODE
{
   COPIER_MASTER,   // publish trades
   COPIER_SLAVE     // subscribe and copy trades
};

input group "=== Copier Mode ==="
input ENUM_COPIER_MODE CopierMode = COPIER_SLAVE; // Run as MASTER or SLAVE
input int              HeartbeatSeconds = 5;      // Master heartbeat interval

input group "=== Transport Settings ==="
input string           SharedDataPath = "TradeCopier\\"; // Shared folder for snapshot file (relative to MQL5/Files or absolute)
input int              MasterSnapshotIntervalMs = 200;     // Master snapshot write interval (ms)
input int              SlavePollIntervalMs = 257;          // Slave snapshot read interval (ms), desynchronized from master

input group "=== Slave Settings ==="
input string           SymbolMap = "";            // Symbol mappings: US30=WS30, XAUUSD=GOLD
input double           BalanceStepAmount = 100.0; // Account-currency units per lot step
input double           BalanceStepSize   = 0.01;  // Lot size added per balance step
input double           MaxLotSize        = 10.0;  // Hard lot-size cap
input int              MaxTradeAgeMinutes = 30;   // Ignore master trades older than this on sync
input bool             NormalizeSLTPByPriceDistance = true; // Convert SL/TP via raw price distance
input int              RetryCount = 3;            // Total order-send attempts (including the first attempt)
input int              RetryDelayMs = 500;        // Delay between retries (ms)

// Magic number base for copied trades. Slave ticket = base + (master_ticket % 900000)
const int MAGIC_BASE = 1000000;

#endif
