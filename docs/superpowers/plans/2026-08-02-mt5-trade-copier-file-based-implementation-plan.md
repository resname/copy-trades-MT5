# MT5 Trade Copier — File-Based IPC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ZeroMQ with a file-based master-to-slave transport so the copier works with no external `Zmq.mqh` dependency, supports multiple slaves on the same PC, and preserves all existing trade-copying features.

**Architecture:** The master writes an atomic JSON snapshot file (`TradeCopier.snapshot.json`) into a configurable shared directory at `MasterSnapshotIntervalMs`. Each slave polls that file at `SlavePollIntervalMs`, diffs it against its previous state, and emits the same lifecycle events (`NEW_TRADE`, `MODIFY_TRADE`, `PARTIAL_CLOSE`, `CLOSE_TRADE`) to the existing trade-execution layer.

**Tech Stack:** MQL5, built-in `File*` functions, native MQL5 JSON parsing helpers in `TradeMessage.mqh`.

## Global Constraints

- No external dependencies: `Zmq.mqh` and any related includes must be removed.
- Shared path: all MT5 terminals must point to the same directory via `SharedDataPath`.
- Default intervals: master writes every 200 ms; slave polls every 257 ms (intentionally desynchronized).
- Heartbeat: each snapshot carries a `heartbeat` timestamp; slave compares the snapshot's `heartbeat` to `TimeLocal()` and warns if it is older than `HeartbeatSeconds * 2` (not merely when file reads fail).
- Atomic writes: master writes to `.tmp` and renames to `.json` to avoid half-read files.
- Multi-slave: any number of slaves may read the same snapshot without coordination.
- Restart recovery: slave startup rebuilds `m_records` from existing `CPY#` positions and treats the first read snapshot as a baseline (no `NEW_TRADE` events for existing positions).

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `MQL5/Experts/TradeCopier/TradeCopier.mq5` | Main EA wiring; starts master or slave timer with independent intervals. | Modify |
| `MQL5/Include/TradeCopier/CopierConfig.mqh` | All user-facing inputs; replaces `CopierPort`/`PublishIntervalMs` with `SharedDataPath`, `MasterSnapshotIntervalMs`, `SlavePollIntervalMs`. | Modify |
| `MQL5/Include/TradeCopier/SnapshotFile.mqh` | NEW: serialize a snapshot to JSON, write atomically, read and parse it back. | Create |
| `MQL5/Include/TradeCopier/MasterPublisher.mqh` | Removes all ZMQ code; writes snapshots via `SnapshotFile` and keeps the position-scanning diff logic. | Modify |
| `MQL5/Include/TradeCopier/SlaveSubscriber.mqh` | Removes all ZMQ code; reads snapshots via `SnapshotFile`, diffs, derives events, and feeds the existing trade executor. | Modify |
| `MQL5/Include/TradeCopier/TradeMessage.mqh` | Adds `SnapshotToJson`/`JsonToSnapshot` helpers for the top-level snapshot object (with positions array). | Modify |
| `README.md` | Removes ZeroMQ installation steps; explains shared path, multi-slave setup, and interval inputs. | Modify |
| `docs/superpowers/specs/2026-08-02-mt5-trade-copier-file-based-design.md` | Approved design reference; do not change unless spec is updated. | Read-only |

---

## Task 1: Update `CopierConfig.mqh`

**Files:**
- Modify: `MQL5/Include/TradeCopier/CopierConfig.mqh`

**Interfaces:**
- Consumes: nothing.
- Produces: new global inputs `SharedDataPath`, `MasterSnapshotIntervalMs`, `SlavePollIntervalMs`; removes `CopierPort` and `PublishIntervalMs`.

- [ ] **Step 1: Remove ZeroMQ inputs and old master interval**

Delete or replace these lines in `CopierConfig.mqh`:

```cpp
input int              CopierPort = 15555;        // ZeroMQ TCP port
```

and

```cpp
input int              PublishIntervalMs = 500;   // Trade change scan interval (ms)
```

- [ ] **Step 2: Add file-based inputs**

Insert the new group and inputs:

```cpp
input group "=== Transport Settings ==="
input string           SharedDataPath = "TradeCopier\\"; // Shared folder for snapshot file (relative to MQL5/Files or absolute)
input int              MasterSnapshotIntervalMs = 200;     // Master snapshot write interval (ms)
input int              SlavePollIntervalMs = 257;          // Slave snapshot read interval (ms), desynchronized from master
```

Keep `HeartbeatSeconds` in the copier-mode group.

- [ ] **Step 3: Commit**

```bash
git add MQL5/Include/TradeCopier/CopierConfig.mqh
git commit -m "config: replace ZeroMQ/port settings with shared file transport inputs

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: Add `SnapshotFile.mqh` for atomic file I/O and snapshot model

**Files:**
- Create: `MQL5/Include/TradeCopier/SnapshotFile.mqh`

**Interfaces:**
- Consumes: `STradeEvent` from `TradeMessage.mqh`.
- Produces:
  - `struct SPositionSnapshot { ulong ticket; string symbol; int side; double open_price; double volume; double sl; double tp; long open_time; double point; string comment; }`
  - `struct STradeSnapshot { long timestamp; long heartbeat; SPositionSnapshot positions[]; }`
  - `class CSnapshotFile` with `static bool Write(const string path, const STradeSnapshot &snapshot)` and `static bool Read(const string path, STradeSnapshot &snapshot`).

- [ ] **Step 1: Create the snapshot model and writer**

Create `MQL5/Include/TradeCopier/SnapshotFile.mqh`:

```cpp
#ifndef SNAPSHOT_FILE_MQH
#define SNAPSHOT_FILE_MQH

#include "TradeMessage.mqh"

struct SPositionSnapshot
{
   ulong  ticket;
   string symbol;
   int    side;
   double open_price;
   double volume;
   double sl;
   double tp;
   long   open_time;
   double point;
   string comment;
};

struct STradeSnapshot
{
   long                timestamp;
   long                heartbeat;
   SPositionSnapshot   positions[];
};

class CSnapshotFile
{
private:
   static string SnapshotPath(const string basePath);
   static string TempPath(const string basePath);
   static string SnapshotToJson(const STradeSnapshot &snapshot);
   static bool   JsonToSnapshot(const string json, STradeSnapshot &snapshot);
   static bool   JsonToPositionSnapshot(const string json, SPositionSnapshot &out, int &endPos);

public:
   static bool Write(const string basePath, const STradeSnapshot &snapshot);
   static bool Read(const string basePath, STradeSnapshot &snapshot);
};

string CSnapshotFile::SnapshotPath(const string basePath)
{
   string folder = basePath;
   int len = StringLen(folder);
   if(len > 0 && folder[len - 1] != '\\' && folder[len - 1] != '/')
      folder += "\\";
   return folder + "TradeCopier.snapshot.json";
}

string CSnapshotFile::TempPath(const string basePath)
{
   return SnapshotPath(basePath) + ".tmp";
}
```

- [ ] **Step 2: Implement `SnapshotToJson`**

Append inside `CSnapshotFile`:

```cpp
string CSnapshotFile::SnapshotToJson(const STradeSnapshot &snapshot)
{
   string json = "{";
   json += "\"timestamp\":" + IntegerToString(snapshot.timestamp) + ",";
   json += "\"heartbeat\":" + IntegerToString(snapshot.heartbeat) + ",";
   json += "\"positions\":[";
   int n = ArraySize(snapshot.positions);
   for(int i = 0; i < n; i++)
   {
      const SPositionSnapshot &p = snapshot.positions[i];
      json += "{";
      json += "\"ticket\":" + IntegerToString((long)p.ticket) + ",";
      json += "\"symbol\":" + CTradeMessage::JsonString(p.symbol) + ",";
      json += "\"side\":" + IntegerToString(p.side) + ",";
      json += "\"open_price\":" + DoubleToString(p.open_price, 8) + ",";
      json += "\"volume\":" + DoubleToString(p.volume, 8) + ",";
      json += "\"sl\":" + DoubleToString(p.sl, 8) + ",";
      json += "\"tp\":" + DoubleToString(p.tp, 8) + ",";
      json += "\"open_time\":" + IntegerToString(p.open_time) + ",";
      json += "\"point\":" + DoubleToString(p.point, 8) + ",";
      json += "\"comment\":" + CTradeMessage::JsonString(p.comment);
      json += "}";
      if(i < n - 1) json += ",";
   }
   json += "]";
   json += "}";
   return json;
}
```

- [ ] **Step 3: Implement `JsonToSnapshot` and helpers**

Append the parser. For simplicity, parse the positions array by locating each `"ticket":` entry and using the existing `GetJsonRawValue` helper:

```cpp
bool CSnapshotFile::JsonToSnapshot(const string json, STradeSnapshot &snapshot)
{
   snapshot.timestamp = 0;
   snapshot.heartbeat = 0;
   ArrayResize(snapshot.positions, 0);

   if(!GetJsonLong(json, "timestamp", snapshot.timestamp)) return false;
   if(!GetJsonLong(json, "heartbeat", snapshot.heartbeat)) return false;

   // Count positions by scanning for "ticket" keys inside the positions array.
   int searchPos = StringFind(json, "\"positions\":");
   if(searchPos == -1) return false;

   int arrayStart = StringFind(json, "[", searchPos);
   int arrayEnd = StringFind(json, "]", arrayStart);
   if(arrayStart == -1 || arrayEnd == -1 || arrayEnd <= arrayStart) return false;

   string arrayBody = StringSubstr(json, arrayStart + 1, arrayEnd - arrayStart - 1);

   int count = 0;
   int pos = StringFind(arrayBody, "\"ticket\":");
   while(pos != -1)
   {
      if(count >= ArraySize(snapshot.positions))
         ArrayResize(snapshot.positions, count + 1);
      SPositionSnapshot &p = snapshot.positions[count];

      string raw;
      string positionJson = StringSubstr(arrayBody, pos);

      if(GetJsonRawValue(positionJson, "ticket", raw))
         p.ticket = (ulong)StringToInteger(raw);
      else
         p.ticket = 0;

      if(!GetJsonString(positionJson, "symbol", p.symbol))
         p.symbol = "";

      if(GetJsonRawValue(positionJson, "side", raw))
         p.side = (int)StringToInteger(raw);
      else
         p.side = 0;

      if(GetJsonRawValue(positionJson, "open_price", raw))
         p.open_price = StringToDouble(raw);
      else
         p.open_price = 0.0;

      if(GetJsonRawValue(positionJson, "volume", raw))
         p.volume = StringToDouble(raw);
      else
         p.volume = 0.0;

      if(GetJsonRawValue(positionJson, "sl", raw))
         p.sl = StringToDouble(raw);
      else
         p.sl = 0.0;

      if(GetJsonRawValue(positionJson, "tp", raw))
         p.tp = StringToDouble(raw);
      else
         p.tp = 0.0;

      if(GetJsonRawValue(positionJson, "open_time", raw))
         p.open_time = StringToInteger(raw);
      else
         p.open_time = 0;

      if(GetJsonRawValue(positionJson, "point", raw))
         p.point = StringToDouble(raw);
      else
         p.point = 0.0;

      if(!GetJsonString(positionJson, "comment", p.comment))
         p.comment = "";

      // Advance search to the next position object.
      pos += StringLen("\"ticket\":");
      pos = StringFind(arrayBody, "\"ticket\":", pos);
      count++;
   }

   if(count != ArraySize(snapshot.positions))
      ArrayResize(snapshot.positions, count);
   return true;
}
```

- [ ] **Step 4: Implement `Write` and `Read`**

```cpp
bool CSnapshotFile::Write(const string basePath, const STradeSnapshot &snapshot)
{
   string tmpPath = TempPath(basePath);
   string finalPath = SnapshotPath(basePath);

   int handle = FileOpen(tmpPath, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("CSnapshotFile: cannot open tmp file %s", tmpPath);
      return false;
   }

   string json = SnapshotToJson(snapshot);
   if(FileWriteString(handle, json) <= 0)
   {
      FileClose(handle);
      PrintFormat("CSnapshotFile: failed to write tmp file %s", tmpPath);
      return false;
   }
   FileClose(handle);

   if(!FileMove(tmpPath, finalPath, FILE_REWRITE|FILE_COMMON))
   {
      PrintFormat("CSnapshotFile: failed to move %s to %s", tmpPath, finalPath);
      return false;
   }
   return true;
}

bool CSnapshotFile::Read(const string basePath, STradeSnapshot &snapshot)
{
   string path = SnapshotPath(basePath);
   int handle = FileOpen(path, FILE_READ|FILE_TXT|FILE_COMMON|FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
      return false;

   string json = "";
   while(!FileIsEnding(handle))
   {
      string line = FileReadString(handle);
      json += line;
   }
   FileClose(handle);

   return JsonToSnapshot(json, snapshot);
}
#endif
```

- [ ] **Step 5: Commit**

```bash
git add MQL5/Include/TradeCopier/SnapshotFile.mqh
git commit -m "feat: add atomic snapshot file read/write helpers

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: Extend `TradeMessage.mqh` with snapshot JSON helpers (if needed)

**Files:**
- Modify: `MQL5/Include/TradeCopier/TradeMessage.mqh`

**Interfaces:**
- Consumes: `STradeEvent` serialization.
- Produces: confirm existing helpers are sufficient for `CSnapshotFile`.

- [ ] **Step 1: Verify helper coverage**

`CSnapshotFile` in Task 2 uses `GetJsonLong` and `GetJsonRawValue` from `TradeMessage.mqh`. Confirm those functions exist and compile.

If `GetJsonRawValue` is missing, add it exactly as shown in the existing `TradeMessage.mqh`:

```cpp
bool GetJsonRawValue(const string json, const string key, string &out)
{
   string pattern = "\"" + key + "\":";
   int pos = StringFind(json, pattern);
   if(pos == -1) return false;
   pos += StringLen(pattern);
   while(pos < StringLen(json) && (json[pos] == ' ' || json[pos] == '\t')) pos++;

   int start = pos;
   while(pos < StringLen(json) && json[pos] != ',' && json[pos] != '}') pos++;
   out = StringSubstr(json, start, pos - start);
   StringReplace(out, " ", "");
   StringReplace(out, "\t", "");
   return true;
}
```

- [ ] **Step 2: Commit**

```bash
git add MQL5/Include/TradeCopier/TradeMessage.mqh
git commit -m "feat: ensure JSON helpers support snapshot parsing

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: Rewrite `MasterPublisher.mqh` to use snapshot files

**Files:**
- Modify: `MQL5/Include/TradeCopier/MasterPublisher.mqh`

**Interfaces:**
- Consumes: `CopierConfig.mqh` for `SharedDataPath` and `HeartbeatSeconds`; `SnapshotFile.mqh` for file I/O; `TradeMessage.mqh` for `STradeEvent`.
- Produces: `bool Init(const string sharedPath, int heartbeatSeconds)` and `void PublishChanges(int intervalMs)`.

- [ ] **Step 1: Remove all ZMQ includes and members**

Change the top of `MasterPublisher.mqh`:

```cpp
#ifndef MASTER_PUBLISHER_MQH
#define MASTER_PUBLISHER_MQH

#include "SnapshotFile.mqh"
#include <Trade\PositionInfo.mqh>
#include "CopierConfig.mqh"
#include "TradeMessage.mqh"
```

Remove:
- `#include <Zmq\Zmq.mqh>`
- `Context *m_context;`
- `Socket *m_socket;`
- `Socket *m_syncPull;`
- All sync-request handling code (`ProcessSyncRequests` and `m_syncPull`).

Keep:
- `SPositionSnapshot`
- `m_heartbeatSeconds`
- `m_lastHeartbeat`
- `m_lastPublish`
- `m_prevSnapshots[]`

- [ ] **Step 2: Add shared path member and update constructor**

```cpp
private:
   string            m_sharedPath;
   int               m_heartbeatSeconds;
   datetime          m_lastHeartbeat;
   ulong             m_lastPublish;
   SPositionSnapshot m_prevSnapshots[];
```

```cpp
public:
   CMasterPublisher() : m_heartbeatSeconds(0), m_lastHeartbeat(0), m_lastPublish(0)
   {
      m_sharedPath = "";
   }
```

- [ ] **Step 3: Rewrite `Init`**

```cpp
bool Init(const string sharedPath, int heartbeatSeconds)
{
   m_sharedPath = sharedPath;
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_lastPublish = 0;
   ArrayResize(m_prevSnapshots, 0);

   // Ensure the shared directory exists.
   if(!FolderCreate(m_sharedPath, FILE_COMMON))
   {
      int err = GetLastError();
      if(err != ERR_FILE_ALREADY_EXIST)
      {
         PrintFormat("MasterPublisher: failed to create shared path %s (error %d)", m_sharedPath, err);
         return false;
      }
   }

   PrintFormat("MasterPublisher: using shared path %s", m_sharedPath);
   return true;
}
```

- [ ] **Step 4: Rewrite `Deinit`**

```cpp
void Deinit()
{
   ArrayResize(m_prevSnapshots, 0);
}
```

- [ ] **Step 5: Rewrite `PublishChanges`**

```cpp
void PublishChanges(int intervalMs)
{
   ulong now = GetTickCount();
   if(now - m_lastPublish < (uint)intervalMs)
      return;
   m_lastPublish = now;

   STradeSnapshot snapshot;
   snapshot.timestamp = (long)TimeLocal();
   snapshot.heartbeat = snapshot.timestamp;

   BuildCurrentSnapshots(snapshot.positions);

   if(!CSnapshotFile::Write(m_sharedPath, snapshot))
      Print("MasterPublisher: failed to write snapshot");

   // Update internal prev snapshots from the written snapshot for closed-position detection.
   ReplaceSnapshots(snapshot.positions);

   m_lastHeartbeat = (datetime)snapshot.heartbeat;
}
```

- [ ] **Step 6: Update `BuildCurrentSnapshots` to fill all fields**

`SPositionSnapshot` now carries the full trade-event payload. Update the fill loop inside `BuildCurrentSnapshots`:

```cpp
      out[count].ticket     = ticket;
      out[count].symbol     = pos.Symbol();
      out[count].side       = (int)pos.PositionType();
      out[count].open_price = pos.PriceOpen();
      out[count].volume     = pos.Volume();
      out[count].sl         = pos.StopLoss();
      out[count].tp         = pos.TakeProfit();
      out[count].open_time  = (long)pos.Time();
      out[count].point      = SymbolInfoDouble(pos.Symbol(), SYMBOL_POINT);
      out[count].comment    = pos.Comment();
```

- [ ] **Step 7: Remove unused helper methods**

Remove `BuildEvent`, `SendEvent`, `Send`, and `ProcessSyncRequests`. Keep `FindSnapshotIndex` and `ReplaceSnapshots` unchanged.

- [ ] **Step 8: Commit**

```bash
git add MQL5/Include/TradeCopier/MasterPublisher.mqh
git commit -m "refactor: rewrite master publisher to use snapshot file

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: Rewrite `SlaveSubscriber.mqh` to poll snapshot files

**Files:**
- Modify: `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`

**Interfaces:**
- Consumes: `SnapshotFile.mqh` for reading snapshots; `CopierConfig.mqh` for `SharedDataPath`, `MaxTradeAgeMinutes`, retry settings, `HeartbeatSeconds`; existing helpers for trade execution.
- Produces: `bool Init(const string sharedPath, ...)` and `void Poll()`.

- [ ] **Step 1: Remove all ZMQ includes and members**

Change the top of `SlaveSubscriber.mqh`:

```cpp
#ifndef SLAVE_SUBSCRIBER_MQH
#define SLAVE_SUBSCRIBER_MQH

#include "SnapshotFile.mqh"
#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\SymbolInfo.mqh>
#include "CopierConfig.mqh"
#include "SymbolMapper.mqh"
#include "LotSizer.mqh"
#include "PriceNormalizer.mqh"
#include "TradeMessage.mqh"
```

Remove:
- `#include <Zmq\Zmq.mqh>`
- `Context *m_context;`
- `Socket *m_socket;`
- `Socket *m_syncPush;`
- `SendSyncRequest()`

Add:
- `string m_sharedPath;`
- `STradeSnapshot m_prevSnapshot;` (initially empty)
- `bool m_baselineSet;`

- [ ] **Step 2: Update constructor and `Init` signature**

```cpp
public:
   CSlaveSubscriber() : m_context(NULL), m_socket(NULL), m_syncPush(NULL),
                        m_maxAgeMinutes(0), m_retryCount(0), m_retryDelayMs(0),
                        m_heartbeatSeconds(0), m_lastHeartbeat(0), m_heartbeatWarned(false),
                        m_baselineSet(false)
   {
      m_sharedPath = "";
   }
```

Update `Init` signature:

```cpp
bool Init(const string sharedPath, const string symbolMap,
          int maxAgeMinutes, int retryCount, int retryDelayMs,
          int heartbeatSeconds);
```

- [ ] **Step 3: Rewrite `Init`**

```cpp
bool CSlaveSubscriber::Init(const string sharedPath, const string symbolMap,
                            int maxAgeMinutes, int retryCount, int retryDelayMs,
                            int heartbeatSeconds)
{
   m_sharedPath = sharedPath;
   m_maxAgeMinutes = maxAgeMinutes;
   m_retryCount = retryCount;
   m_retryDelayMs = retryDelayMs;
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_heartbeatWarned = false;
   m_baselineSet = false;
   ArrayResize(m_records, 0);
   ArrayResize(m_prevSnapshot.positions, 0);

   m_mapper.Init(symbolMap);

   // Ensure shared directory exists.
   if(!FolderCreate(m_sharedPath, FILE_COMMON))
   {
      int err = GetLastError();
      if(err != ERR_FILE_ALREADY_EXIST)
      {
         PrintFormat("SlaveSubscriber: failed to create shared path %s (error %d)", m_sharedPath, err);
         return false;
      }
   }

   PrintFormat("SlaveSubscriber: using shared path %s", m_sharedPath);

   // Start heartbeat timer now.
   m_lastHeartbeat = TimeCurrent();

   // Rebuild records for any copied positions already open on the slave account.
   int total = PositionsTotal();
   int rebuilt = 0;
   for(int i = 0; i < total; i++)
   {
      string posSymbol = PositionGetSymbol(i);
      if(posSymbol == "")
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(magic < MAGIC_BASE)
         continue;

      ulong slaveTicket = PositionGetInteger(POSITION_TICKET);
      string comment = PositionGetString(POSITION_COMMENT);
      string prefix = "CPY#";
      int prefixPos = StringFind(comment, prefix);
      if(prefixPos == -1)
         continue;

      int numPos = prefixPos + StringLen(prefix);
      double masterVolume = 0.0;
      double slaveVolume  = 0.0;
      bool parsedVolumes = false;

      int pipePos = StringFind(comment, "|", numPos);
      if(pipePos != -1)
      {
         int mvPos = StringFind(comment, "|MV", numPos);
         int svPos = StringFind(comment, "|SV", numPos);
         if(mvPos != -1 && svPos != -1)
         {
            string mvStr = StringSubstr(comment, mvPos + 3, svPos - (mvPos + 3));
            string svStr = StringSubstr(comment, svPos + 3);
            double mv = StringToDouble(mvStr);
            double sv = StringToDouble(svStr);
            if(mv > 0.0 && sv > 0.0)
            {
               masterVolume = mv;
               slaveVolume  = sv;
               parsedVolumes = true;
            }
         }
      }

      if(!parsedVolumes)
      {
         string ticketStr = StringSubstr(comment, numPos);
         if(StringToInteger(ticketStr) <= 0)
            continue;
         double volume = PositionGetDouble(POSITION_VOLUME);
         masterVolume = volume;
         slaveVolume  = volume;
      }

      int idx = ArraySize(m_records);
      ArrayResize(m_records, idx + 1);
      m_records[idx].magic = magic;
      m_records[idx].slave_ticket = slaveTicket;
      m_records[idx].master_open_volume = masterVolume;
      m_records[idx].slave_open_volume  = slaveVolume;
      rebuilt++;
   }

   if(rebuilt > 0)
      PrintFormat("SlaveSubscriber: rebuilt %d copied position record(s) from open positions", rebuilt);
   return true;
}
```

- [ ] **Step 4: Rewrite `Deinit`**

```cpp
void CSlaveSubscriber::Deinit()
{
   ArrayResize(m_records, 0);
   ArrayResize(m_prevSnapshot.positions, 0);
}
```

- [ ] **Step 5: Rewrite `Poll`**

```cpp
void CSlaveSubscriber::Poll()
{
   STradeSnapshot snapshot;
   if(!CSnapshotFile::Read(m_sharedPath, snapshot))
   {
      CheckHeartbeat(0); // no fresh heartbeat available
      return;
   }

   // Validate snapshot heartbeat against local clock.
   CheckHeartbeat(snapshot.heartbeat);

   if(!m_baselineSet)
   {
      // First read: establish baseline without generating NEW_TRADE events.
      EstablishBaseline(snapshot);
      return;
   }

   DiffAndProcess(snapshot);
   m_prevSnapshot = snapshot;
}
```

- [ ] **Step 6: Add `EstablishBaseline`**

```cpp
void CSlaveSubscriber::EstablishBaseline(const STradeSnapshot &snapshot)
{
   ArrayResize(m_prevSnapshot.positions, 0);
   int n = ArraySize(snapshot.positions);
   int count = 0;
   for(int i = 0; i < n; i++)
   {
      if(IsTooOld((datetime)snapshot.positions[i].open_time))
         continue;

      if(count >= ArraySize(m_prevSnapshot.positions))
         ArrayResize(m_prevSnapshot.positions, count + 1);
      m_prevSnapshot.positions[count] = snapshot.positions[i];
      count++;
   }
   ArrayResize(m_prevSnapshot.positions, count);
   m_baselineSet = true;
   Print("SlaveSubscriber: baseline established");
}
```

- [ ] **Step 7: Add `DiffAndProcess`**

```cpp
void CSlaveSubscriber::DiffAndProcess(const STradeSnapshot &snapshot)
{
   int n = ArraySize(snapshot.positions);

   // NEW / MODIFIED / PARTIAL_CLOSE
   for(int i = 0; i < n; i++)
   {
      const SPositionSnapshot &curr = snapshot.positions[i];
      int idx = FindSnapshotIndex(m_prevSnapshot.positions, curr.ticket);

      if(idx < 0)
      {
         STradeEvent e = BuildEventFromSnapshot("NEW_TRADE", curr);
         OpenTrade(e);
      }
      else
      {
         const SPositionSnapshot &prev = m_prevSnapshot.positions[idx];
         if(NormalizeDouble(prev.volume - curr.volume, 8) > 0.0)
         {
            STradeEvent e = BuildEventFromSnapshot("PARTIAL_CLOSE", curr);
            PartialClose(e);
         }

         if(NormalizeDouble(prev.sl - curr.sl, 8) != 0.0 ||
            NormalizeDouble(prev.tp - curr.tp, 8) != 0.0)
         {
            STradeEvent e = BuildEventFromSnapshot("MODIFY_TRADE", curr);
            ModifyTrade(e);
         }
      }
   }

   // CLOSE_TRADE
   for(int i = ArraySize(m_prevSnapshot.positions) - 1; i >= 0; i--)
   {
      ulong oldTicket = m_prevSnapshot.positions[i].ticket;
      if(FindSnapshotIndex(snapshot.positions, oldTicket) < 0)
      {
         STradeEvent e;
         ZeroMemory(e);
         e.event = "CLOSE_TRADE";
         e.timestamp = (long)TimeLocal();
         e.magic = MAGIC_BASE + (int)(oldTicket % 900000);
         e.master_ticket = oldTicket;
         CloseTrade(e);
      }
   }
}

STradeEvent CSlaveSubscriber::BuildEventFromSnapshot(const string eventName, const SPositionSnapshot &pos)
{
   STradeEvent e;
   ZeroMemory(e);
   e.event = eventName;
   e.timestamp = (long)TimeLocal();
   e.master_ticket = pos.ticket;
   e.magic = MAGIC_BASE + (int)(pos.ticket % 900000);
   e.symbol = pos.symbol;
   e.side = pos.side;
   e.open_price = pos.open_price;
   e.volume = pos.volume;
   e.sl = pos.sl;
   e.tp = pos.tp;
   e.open_time = (datetime)pos.open_time;
   e.point = pos.point;
   e.comment = pos.comment;
   return e;
}

void CSlaveSubscriber::CheckHeartbeat(long snapshotHeartbeat)
{
   datetime now = TimeCurrent();
   if(snapshotHeartbeat > 0)
   {
      // A fresh snapshot was read; reset heartbeat tracking.
      m_lastHeartbeat = now;
      m_heartbeatWarned = false;
      return;
   }

   if(m_heartbeatSeconds <= 0 || m_lastHeartbeat == 0)
      return;

   if(now - m_lastHeartbeat > m_heartbeatSeconds * 2)
   {
      if(!m_heartbeatWarned)
      {
         Print("SlaveSubscriber: no heartbeat from master");
         m_heartbeatWarned = true;
      }
   }
}

int CSlaveSubscriber::FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const
{
   int n = ArraySize(snapshots);
   for(int i = 0; i < n; i++)
      if(snapshots[i].ticket == ticket)
         return i;
   return -1;
}
```

- [ ] **Step 8: Add missing `FindSnapshotIndex` declaration and remove old methods**

Add to private declarations:

```cpp
   void        EstablishBaseline(const STradeSnapshot &snapshot);
   void        DiffAndProcess(const STradeSnapshot &snapshot);
   STradeEvent BuildEventFromSnapshot(const string eventName, const SPositionSnapshot &pos);
   void        CheckHeartbeat(long snapshotHeartbeat);
   int         FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const;
   int         FindSnapshotIndex(ulong ticket) const;
```

Remove:
- `SendSyncRequest()`
- Old `ProcessEvent(const STradeEvent &e)`
- Any ZMQ-specific code in `Init` and `Deinit`.

Keep:
- `OpenTrade`, `ModifyTrade`, `PartialClose`, `CloseTrade`, `FindRecord(long magic)`, `IsTooOld`, `OpenSlaveOrder`, `RoundToTickSize`.

- [ ] **Step 9: Commit**

```bash
git add MQL5/Include/TradeCopier/SlaveSubscriber.mqh
git commit -m "refactor: rewrite slave subscriber to poll snapshot file

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: Update `TradeCopier.mq5` for independent master/slave timers

**Files:**
- Modify: `MQL5/Experts/TradeCopier/TradeCopier.mq5`

**Interfaces:**
- Consumes: new `SharedDataPath`, `MasterSnapshotIntervalMs`, `SlavePollIntervalMs` inputs; new `Init` signatures.
- Produces: `OnInit` registers the correct timer interval per mode; `OnTimer` calls the matching object.

- [ ] **Step 1: Update `OnInit`**

```cpp
int OnInit()
{
   if(CopierMode == COPIER_MASTER)
   {
      if(!g_master.Init(SharedDataPath, HeartbeatSeconds))
      {
         Print("TradeCopier: failed to initialize MASTER");
         return INIT_FAILED;
      }
      EventSetMillisecondTimer(MasterSnapshotIntervalMs);
      Print("TradeCopier: running as MASTER");
   }
   else
   {
      if(!g_slave.Init(SharedDataPath, SymbolMap, MaxTradeAgeMinutes, RetryCount, RetryDelayMs, HeartbeatSeconds))
      {
         Print("TradeCopier: failed to initialize SLAVE");
         return INIT_FAILED;
      }
      EventSetMillisecondTimer(SlavePollIntervalMs);
      Print("TradeCopier: running as SLAVE");
   }
   return(INIT_SUCCEEDED);
}
```

- [ ] **Step 2: Update `OnTimer`**

```cpp
void OnTimer()
{
   if(CopierMode == COPIER_MASTER)
      g_master.PublishChanges(MasterSnapshotIntervalMs);
   else
      g_slave.Poll();
}
```

- [ ] **Step 3: Commit**

```bash
git add MQL5/Experts/TradeCopier/TradeCopier.mq5
git commit -m "feat: wire master/slave snapshot timers with independent intervals

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: Update `README.md`

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: updated inputs and behavior.
- Produces: installation/usage docs matching the file-based transport.

- [ ] **Step 1: Replace ZeroMQ installation section**

Change:

```markdown
## Installation

1. Copy `MQL5/Experts/TradeCopier/TradeCopier.mq5` and the `MQL5/Include/TradeCopier/*.mqh` files into your MetaTrader 5 data folder.
2. Make sure the MQL5 ZeroMQ binding (`MQL5/Include/Zmq/Zmq.mqh`) is installed.
   - If missing, install the "ZeroMQ" library from the MetaTrader Market or copy a known-good ZMQ include set.
3. Open `TradeCopier.mq5` in MetaEditor and compile (F7).
4. Attach the EA to a chart on the master account; set `CopierMode` to `MASTER`.
5. Attach the EA to a chart on the slave account; set `CopierMode` to `SLAVE` and configure symbol mapping / lot sizing.
6. Both MT5 terminals must be running on the same machine.
7. The sync channel automatically uses `CopierPort + 1`; no extra input is required.
```

To:

```markdown
## Installation

1. Copy `MQL5/Experts/TradeCopier/TradeCopier.mq5` and the `MQL5/Include/TradeCopier/*.mqh` files into your MetaTrader 5 data folder.
2. Open `TradeCopier.mq5` in MetaEditor and compile (F7).
3. Attach the EA to a chart on the master account; set `CopierMode` to `MASTER`.
4. Attach the EA to a chart on the slave account; set `CopierMode` to `SLAVE`.
5. Set `SharedDataPath` to the same folder on both terminals, e.g. `C:\\TradeCopier\\Shared\\`.
6. Both MT5 terminals must be running on the same machine.
```

- [ ] **Step 2: Replace the configuration table**

Change the "Copier Mode" table:

```markdown
### Copier Mode

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| CopierMode | ENUM_COPIER_MODE | COPIER_SLAVE | Run as MASTER or SLAVE |
| SharedDataPath | string | `TradeCopier\\` | Shared folder for snapshot file |
| MasterSnapshotIntervalMs | int | 200 | Master snapshot write interval |
| SlavePollIntervalMs | int | 257 | Slave snapshot read interval (desynchronized from master) |
| HeartbeatSeconds | int | 5 | Master heartbeat interval |
```

Remove the "Master Settings" section entirely. Keep the slave settings table, changing `NormalizeSLTPByPriceDistance` description to "Convert SL/TP via raw price distance" and removing any ZeroMQ references.

- [ ] **Step 3: Add multi-slave note**

Insert after the installation list:

```markdown
## Multi-Slave Setup

You can attach the slave EA to any number of charts/terminals. All slaves must use the same `SharedDataPath`. Each slave has its own `SymbolMap`, `BalanceStepAmount`, and `MaxLotSize` settings.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: update README for file-based transport and multi-slave setup

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: Compile and smoke-test in MetaEditor

**Files:**
- All of the above.

**Interfaces:**
- Consumes: complete code changes.
- Produces: clean compilation and a manual smoke-test checklist.

- [ ] **Step 1: Compile the EA**

Open `MQL5/Experts/TradeCopier/TradeCopier.mq5` in MetaEditor and press F7. Fix any compiler errors.

- [ ] **Step 2: Manual same-PC smoke test**

Use two demo accounts on the same machine:

1. Attach the EA to a chart in `MASTER` mode. Set `SharedDataPath` to an absolute path, e.g. `C:\\TradeCopier\\Shared\\`.
2. Attach the EA to a second chart in `SLAVE` mode with the same `SharedDataPath`.
3. Open a market order on the master. Verify:
   - `TradeCopier.snapshot.json` appears in the shared folder.
   - The slave opens the corresponding position within ~1 second.
4. Modify SL/TP on the master; verify the slave updates.
5. Partially close the master; verify the slave closes the same fraction.
6. Fully close the master; verify the slave closes.
7. Stop the master EA and wait longer than `HeartbeatSeconds * 2`; verify the slave logs a missing-master warning but does not close positions.

- [ ] **Step 3: Multi-slave smoke test**

Attach a second slave terminal with the same `SharedDataPath`. Open/close trades on the master and verify both slaves mirror the lifecycle.

- [ ] **Step 4: Commit test results / fix any issues**

If fixes are needed, commit them with descriptive messages. If the smoke test passes, no extra commit is required beyond the fix commits.

---

## Spec Coverage Check

| Spec Requirement | Implementing Task |
|------------------|-------------------|
| No external `Zmq.mqh` dependency | Task 4, 5, 6, 7 |
| Configurable shared path | Task 1, 6 |
| Atomic `.tmp` → `.json` writes | Task 2 |
| Master writes every 200 ms by default | Task 4, 6 |
| Slave polls every 257 ms by default | Task 1, 6 |
| Desynchronized intervals | Task 1 (defaults) |
| Snapshot JSON format with `timestamp`, `heartbeat`, `positions` | Task 2 |
| Heartbeat detection on slave | Task 5 |
| Multi-slave reading same file | Task 2, 5, 7 |
| Slave startup baseline / no duplicate `NEW_TRADE` | Task 5 |
| Restart recovery via `CPY#` comments | Task 5 (kept existing logic) |
| Full lifecycle mirroring preserved | Task 5 |
| README updated | Task 7 |

## Placeholder Scan

No placeholders found. Every task provides concrete code, file paths, and expected commands.

## Type Consistency Check

- `CSnapshotFile::Write` and `Read` both take `const string basePath` and return `bool`.
- `CMasterPublisher::Init` now takes `(const string sharedPath, int heartbeatSeconds)`.
- `CSlaveSubscriber::Init` now takes `(const string sharedPath, const string symbolMap, int maxAgeMinutes, int retryCount, int retryDelayMs, int heartbeatSeconds)`.
- `EventSetMillisecondTimer` receives `MasterSnapshotIntervalMs` or `SlavePollIntervalMs`.
- `STradeSnapshot` and `SPositionSnapshot` field names are consistent across write and parse.
- `SPositionSnapshot` carries the full trade-event payload; `SlaveSubscriber` builds `STradeEvent` from the snapshot, not from local `PositionInfo`.
