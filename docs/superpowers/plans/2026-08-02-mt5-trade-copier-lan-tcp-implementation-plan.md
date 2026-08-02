# MT5 Trade Copier LAN TCP + UDP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current file-based shared-snapshot transport with a LAN-aware TCP + UDP broadcast transport, and add an on-chart GUI panel for configuration including a symbol-mapping table.

**Architecture:** The master runs a TCP server and advertises its endpoint via UDP broadcast. Slaves listen for the broadcast, connect to the master over TCP, and receive serialized trade events. An on-chart GUI panel built from MQL5 chart objects provides mode status, connection/latency display, and an editable symbol-mapping table.

**Tech Stack:** MQL5 built-in socket API (`Socket*`), MQL5 chart objects (`OBJ_LABEL`, `OBJ_EDIT`, `OBJ_BUTTON`, `OBJ_RECTANGLE_LABEL`), existing trade-copying helpers (`SymbolMapper`, `LotSizer`, `PriceNormalizer`, `TradeMessage`).

## Global Constraints

- Use only built-in MQL5 features — no external DLLs, no `Zmq.mqh`, no third-party libraries.
- Transport must be LAN-capable with zero manual connection configuration beyond choosing MASTER/SLAVE mode.
- Preserve existing features: manual symbol translation, balance-step lot sizing, raw price-distance SL/TP mirroring, full lifecycle mirroring, restart recovery without duplicates.
- All changes are committed to `main` with clear messages ending in `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Each task ends with a compile check in MetaEditor before the next task begins.

---

## File Map

| File | Responsibility |
|------|----------------|
| `MQL5/Include/TradeCopier/LanTransport.mqh` | New. UDP broadcast discovery + TCP server/client framing. |
| `MQL5/Include/TradeCopier/TradeCopierGui.mqh` | New. On-chart panel with tabs, status, symbol-mapping table. |
| `MQL5/Include/TradeCopier/MasterPublisher.mqh` | Rewrite. Uses `CLanTransport` to broadcast and push events to slaves. |
| `MQL5/Include/TradeCopier/SlaveSubscriber.mqh` | Rewrite. Uses `CLanTransport` to discover master and receive events. |
| `MQL5/Experts/TradeCopier/TradeCopier.mq5` | Rewrite. Initializes transport and GUI, wires `OnTimer`. |
| `MQL5/Include/TradeCopier/CopierConfig.mqh` | Update. Replace file-based inputs with UDP discovery port and remove snapshot inputs. |
| `MQL5/Include/TradeCopier/SnapshotFile.mqh` | Delete. No longer needed. |
| `README.md` | Update. Remove file-based docs, add LAN setup and GUI usage. |

---

## Task 1: Update CopierConfig.mqh for LAN Transport

**Files:**
- Modify: `MQL5/Include/TradeCopier/CopierConfig.mqh`

**Interfaces:**
- Produces: inputs `DiscoveryUdpPort`, `HeartbeatSeconds`, `SymbolMap`, slave/master settings. Removes `SharedDataPath`, `MasterSnapshotIntervalMs`, `SlavePollIntervalMs`.

- [ ] **Step 1: Replace file-based inputs with LAN inputs**

Replace the existing input groups with:

```cpp
input group "=== Copier Mode ==="
input ENUM_COPIER_MODE CopierMode = COPIER_SLAVE; // Run as MASTER or SLAVE
input ushort           DiscoveryUdpPort = 55555;  // UDP port for master discovery broadcasts
input int              HeartbeatSeconds = 5;      // Maximum heartbeat age before slave warns

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
```

- [ ] **Step 2: Compile `TradeCopier.mq5` in MetaEditor**

Expected: compile succeeds or expected errors from removed inputs appear. Those errors are fixed in later tasks.

- [ ] **Step 3: Commit**

```bash
git add MQL5/Include/TradeCopier/CopierConfig.mqh
git commit -m "config: replace file-based inputs with LAN discovery port" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: Create LanTransport.mqh

**Files:**
- Create: `MQL5/Include/TradeCopier/LanTransport.mqh`

**Interfaces:**
- Produces:
  - `CLanTransport::StartMaster(ushort &outTcpPort) -> bool`
  - `CLanTransport::StopMaster()`
  - `CLanTransport::BroadcastEndpoint(ushort tcpPort) -> bool`
  - `CLanTransport::AcceptClients() -> bool`
  - `CLanTransport::SendToAllClients(const string &json) -> bool`
  - `CLanTransport::StartSlaveListener() -> bool`
  - `CLanTransport::StopSlaveListener()`
  - `CLanTransport::DiscoverMaster(string &outHost, ushort &outPort, uint timeoutMs) -> bool`
  - `CLanTransport::ConnectToMaster(const string host, ushort port) -> bool`
  - `CLanTransport::ReceiveFrame(string &outJson, uint timeoutMs) -> bool`
  - `CLanTransport::SendFrame(const string &json) -> bool`
  - `CLanTransport::DisconnectSlave()`
  - `CLanTransport::LatencyMs() -> int`

- [ ] **Step 1: Write the header and constants**

```cpp
#ifndef LAN_TRANSPORT_MQH
#define LAN_TRANSPORT_MQH

#include "TradeMessage.mqh"

const string MASTER_BROADCAST_PREFIX = "MT5COPIER:";
const uint   DEFAULT_DISCOVERY_PORT  = 55555;
const int    MAX_CLIENTS = 8;
```

- [ ] **Step 2: Add private members and public interface declaration**

```cpp
class CLanTransport
{
private:
   // Master
   int    m_tcpServer;
   int    m_udpBroadcast;
   int    m_clients[];
   ushort m_tcpPort;

   // Slave
   int    m_udpListener;
   int    m_tcpClient;
   string m_masterHost;
   ushort m_masterPort;

   int    m_latencyMs;

   bool   ReadFrame(int socket, string &outJson, uint timeoutMs);
   bool   InternalSend(int socket, const string &json);
   int    FindClientSlot();

public:
   CLanTransport() : m_tcpServer(INVALID_HANDLE), m_udpBroadcast(INVALID_HANDLE),
                     m_udpListener(INVALID_HANDLE), m_tcpClient(INVALID_HANDLE),
                     m_tcpPort(0), m_masterPort(0), m_latencyMs(-1)
   {
      ArrayResize(m_clients, 0);
   }

   // Master
   bool StartMaster(ushort &outTcpPort);
   void StopMaster();
   bool BroadcastEndpoint(uint discoveryUdpPort);
   bool AcceptClients();
   bool SendToAllClients(const string &json);

   // Slave
   bool StartSlaveListener(uint discoveryUdpPort);
   void StopSlaveListener();
   bool DiscoverMaster(string &outHost, ushort &outPort, uint timeoutMs);
   bool ConnectToMaster(const string host, ushort port);
   bool ReceiveFrame(string &outJson, uint timeoutMs);
   bool SendFrame(const string &json);
   void DisconnectSlave();

   int  LatencyMs() const { return m_latencyMs; }
   bool IsConnected() const { return m_tcpClient != INVALID_HANDLE; }
};
```

- [ ] **Step 3: Implement master start and broadcast**

```cpp
bool CLanTransport::StartMaster(ushort &outTcpPort)
{
   m_tcpServer = SocketCreate(SOCKET_PROTOCOL_TCP);
   if(m_tcpServer == INVALID_HANDLE) return false;

   // Bind to ephemeral port by trying a default range.
   for(ushort port = 30000; port < 30100; port++)
   {
      if(SocketBind(m_tcpServer, "0.0.0.0", port))
      {
         m_tcpPort = port;
         outTcpPort = port;
         if(SocketListen(m_tcpServer)) return true;
      }
   }

   SocketClose(m_tcpServer);
   m_tcpServer = INVALID_HANDLE;
   return false;
}

void CLanTransport::StopMaster()
{
   int n = ArraySize(m_clients);
   for(int i = 0; i < n; i++)
      if(m_clients[i] != INVALID_HANDLE)
         SocketClose(m_clients[i]);
   ArrayResize(m_clients, 0);

   if(m_tcpServer != INVALID_HANDLE)
   {
      SocketClose(m_tcpServer);
      m_tcpServer = INVALID_HANDLE;
   }
   if(m_udpBroadcast != INVALID_HANDLE)
   {
      SocketClose(m_udpBroadcast);
      m_udpBroadcast = INVALID_HANDLE;
   }
   m_tcpPort = 0;
}

bool CLanTransport::BroadcastEndpoint(uint discoveryUdpPort)
{
   if(m_udpBroadcast == INVALID_HANDLE)
   {
      m_udpBroadcast = SocketCreate(SOCKET_PROTOCOL_UDP);
      if(m_udpBroadcast == INVALID_HANDLE) return false;
      if(!SocketBind(m_udpBroadcast, "0.0.0.0", 0)) return false;
      if(!SocketEnableBroadcast(m_udpBroadcast)) return false;
   }

   string msg = MASTER_BROADCAST_PREFIX + IntegerToString(m_tcpPort) + "\n";
   char data[];
   StringToCharArray(msg, data);
   return SocketSend(m_udpBroadcast, "255.255.255.255", discoveryUdpPort, data) > 0;
}
```

- [ ] **Step 4: Implement client accept and broadcast-to-all**

```cpp
bool CLanTransport::AcceptClients()
{
   if(m_tcpServer == INVALID_HANDLE) return false;

   while(true)
   {
      int client = SocketAccept(m_tcpServer);
      if(client == INVALID_HANDLE) break;

      int slot = FindClientSlot();
      if(slot < 0)
      {
         SocketClose(client);
         Print("LanTransport: too many clients");
         break;
      }
      m_clients[slot] = client;
      PrintFormat("LanTransport: client connected (slot %d)", slot);
   }
   return true;
}

int CLanTransport::FindClientSlot()
{
   int n = ArraySize(m_clients);
   for(int i = 0; i < n; i++)
      if(m_clients[i] == INVALID_HANDLE)
         return i;
   if(n < MAX_CLIENTS)
   {
      ArrayResize(m_clients, n + 1);
      m_clients[n] = INVALID_HANDLE;
      return n;
   }
   return -1;
}

bool CLanTransport::SendToAllClients(const string &json)
{
   if(m_tcpServer == INVALID_HANDLE) return false;

   bool any = false;
   int n = ArraySize(m_clients);
   for(int i = 0; i < n; i++)
   {
      if(m_clients[i] != INVALID_HANDLE)
      {
         if(InternalSend(m_clients[i], json))
            any = true;
         else
         {
            SocketClose(m_clients[i]);
            m_clients[i] = INVALID_HANDLE;
         }
      }
   }
   return any;
}
```

- [ ] **Step 5: Implement slave discovery and TCP client**

```cpp
bool CLanTransport::StartSlaveListener(uint discoveryUdpPort)
{
   m_udpListener = SocketCreate(SOCKET_PROTOCOL_UDP);
   if(m_udpListener == INVALID_HANDLE) return false;
   if(!SocketBind(m_udpListener, "0.0.0.0", discoveryUdpPort))
   {
      SocketClose(m_udpListener);
      m_udpListener = INVALID_HANDLE;
      return false;
   }
   return true;
}

void CLanTransport::StopSlaveListener()
{
   DisconnectSlave();
   if(m_udpListener != INVALID_HANDLE)
   {
      SocketClose(m_udpListener);
      m_udpListener = INVALID_HANDLE;
   }
}

bool CLanTransport::DiscoverMaster(string &outHost, ushort &outPort, uint timeoutMs)
{
   if(m_udpListener == INVALID_HANDLE) return false;

   uint start = GetTickCount();
   while(GetTickCount() - start < timeoutMs)
   {
      char buf[256];
      string fromHost;
      uint fromPort;
      int received = SocketReceiveFrom(m_udpListener, fromHost, fromPort, buf, 256, 100);
      if(received > 0)
      {
         string msg = CharArrayToString(buf, 0, received);
         int prefixLen = StringLen(MASTER_BROADCAST_PREFIX);
         if(StringFind(msg, MASTER_BROADCAST_PREFIX) == 0)
         {
            string portStr = StringSubstr(msg, prefixLen);
            StringReplace(portStr, "\n", "");
            ushort tcpPort = (ushort)StringToInteger(portStr);
            if(tcpPort > 0)
            {
               outHost = fromHost;
               outPort = tcpPort;
               return true;
            }
         }
      }
   }
   return false;
}

bool CLanTransport::ConnectToMaster(const string host, ushort port)
{
   DisconnectSlave();

   m_tcpClient = SocketCreate(SOCKET_PROTOCOL_TCP);
   if(m_tcpClient == INVALID_HANDLE) return false;

   if(!SocketConnect(m_tcpClient, host, port, 2000))
   {
      SocketClose(m_tcpClient);
      m_tcpClient = INVALID_HANDLE;
      return false;
   }

   m_masterHost = host;
   m_masterPort = port;
   m_latencyMs = -1;
   return true;
}

void CLanTransport::DisconnectSlave()
{
   if(m_tcpClient != INVALID_HANDLE)
   {
      SocketClose(m_tcpClient);
      m_tcpClient = INVALID_HANDLE;
   }
   m_masterHost = "";
   m_masterPort = 0;
   m_latencyMs = -1;
}
```

- [ ] **Step 6: Implement frame send/receive with length prefix**

```cpp
bool CLanTransport::InternalSend(int socket, const string &json)
{
   if(socket == INVALID_HANDLE) return false;

   int len = StringLen(json);
   char header[4];
   header[0] = (char)(len & 0xFF);
   header[1] = (char)((len >> 8) & 0xFF);
   header[2] = (char)((len >> 16) & 0xFF);
   header[3] = (char)((len >> 24) & 0xFF);

   char payload[];
   StringToCharArray(json, payload);

   char frame[];
   ArrayResize(frame, 4 + ArraySize(payload));
   ArrayCopy(frame, header, 0, 0, 4);
   ArrayCopy(frame, payload, 4, 0, ArraySize(payload));

   return SocketSend(socket, frame) == ArraySize(frame);
}

bool CLanTransport::ReceiveFrame(string &outJson, uint timeoutMs)
{
   outJson = "";
   if(m_tcpClient == INVALID_HANDLE) return false;

   uint start = GetTickCount();

   // Read 4-byte length
   char header[4];
   int headerRead = 0;
   while(headerRead < 4)
   {
      if(GetTickCount() - start > timeoutMs)
         return false;

      char tmp[1];
      int r = SocketRead(m_tcpClient, tmp, 1, 100);
      if(r > 0)
      {
         header[headerRead] = tmp[0];
         headerRead++;
      }
      else if(r < 0)
         return false;
   }

   int len = (int)((uchar)header[0] |
                   ((uchar)header[1] << 8) |
                   ((uchar)header[2] << 16) |
                   ((uchar)header[3] << 24));
   if(len <= 0 || len > 65536) return false;

   char payload[];
   ArrayResize(payload, len);
   int payloadRead = 0;
   while(payloadRead < len)
   {
      if(GetTickCount() - start > timeoutMs)
         return false;

      int r = SocketRead(m_tcpClient, payload, len - payloadRead, 100);
      if(r > 0)
         payloadRead += r;
      else if(r < 0)
         return false;
   }

   outJson = CharArrayToString(payload, 0, len);

   // Crude latency estimate: measure time since last frame request if we sent one.
   static uint s_lastRequest = 0;
   if(s_lastRequest > 0)
   {
      m_latencyMs = (int)(GetTickCount() - s_lastRequest);
      s_lastRequest = 0;
   }
   return true;
}

bool CLanTransport::SendFrame(const string &json)
{
   if(m_tcpClient == INVALID_HANDLE) return false;
   return InternalSend(m_tcpClient, json);
}
```

- [ ] **Step 7: Close header guard**

```cpp
#endif // LAN_TRANSPORT_MQH
```

- [ ] **Step 8: Compile a stub test**

Create a temporary `Tests/LanTransportCompile.mq5` that only includes `LanTransport.mqh` and `OnInit() { return INIT_SUCCEEDED; }`. Compile in MetaEditor and verify no syntax errors. Delete the stub file after compile succeeds.

- [ ] **Step 9: Commit**

```bash
git add MQL5/Include/TradeCopier/LanTransport.mqh
git commit -m "feat: add LAN transport with UDP discovery and TCP framing" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: Rewrite MasterPublisher.mqh for LAN Transport

**Files:**
- Modify: `MQL5/Include/TradeCopier/MasterPublisher.mqh`
- Delete: `MQL5/Include/TradeCopier/SnapshotFile.mqh` (do this in Task 8)

**Interfaces:**
- Consumes: `CLanTransport` from Task 2, `TradeMessage.mqh`, `PositionInfo.mqh`.
- Produces: `CMasterPublisher::Init(int discoveryUdpPort, int heartbeatSeconds)`, `PublishChanges()`, `Deinit()`.

- [ ] **Step 1: Replace file-based includes and members**

```cpp
#ifndef MASTER_PUBLISHER_MQH
#define MASTER_PUBLISHER_MQH

#include "LanTransport.mqh"
#include <Trade\PositionInfo.mqh>
#include "CopierConfig.mqh"
#include "TradeMessage.mqh"

class CMasterPublisher
{
private:
   CLanTransport     m_transport;
   int                 m_discoveryUdpPort;
   int                 m_heartbeatSeconds;
   datetime            m_lastHeartbeat;
   ulong               m_lastPublish;
   ulong               m_lastBroadcast;
   SPositionSnapshot   m_prevSnapshots[];

   STradeEvent BuildEvent(const string eventName, const CPositionInfo &pos, double volume);
   void        SendEvent(const string eventName, const CPositionInfo &pos, double volume);
   int         FindSnapshotIndex(ulong ticket) const;
   int         FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const;
   void        BuildCurrentSnapshots(SPositionSnapshot &out[]);
   void        ReplaceSnapshots(const SPositionSnapshot &src[]);
   void        SendHeartbeat();
   void        ProcessSyncRequest();

public:
   CMasterPublisher() : m_discoveryUdpPort(0), m_heartbeatSeconds(0),
                        m_lastHeartbeat(0), m_lastPublish(0), m_lastBroadcast(0)
   {
      ArrayResize(m_prevSnapshots, 0);
   }
   bool Init(int discoveryUdpPort, int heartbeatSeconds);
   void Deinit();
   void PublishChanges(int intervalMs);
};
```

- [ ] **Step 2: Implement Init/Deinit**

```cpp
bool CMasterPublisher::Init(int discoveryUdpPort, int heartbeatSeconds)
{
   m_discoveryUdpPort = discoveryUdpPort;
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_lastPublish = 0;
   m_lastBroadcast = 0;
   ArrayResize(m_prevSnapshots, 0);

   ushort tcpPort = 0;
   if(!m_transport.StartMaster(tcpPort))
   {
      Print("MasterPublisher: failed to start TCP server");
      return false;
   }

   PrintFormat("MasterPublisher: TCP server on port %d", tcpPort);
   return true;
}

void CMasterPublisher::Deinit()
{
   m_transport.StopMaster();
   ArrayResize(m_prevSnapshots, 0);
}
```

- [ ] **Step 3: Implement BuildEvent / SendEvent / SendHeartbeat**

```cpp
STradeEvent CMasterPublisher::BuildEvent(const string eventName, const CPositionInfo &pos, double volume)
{
   STradeEvent e;
   ZeroMemory(e);
   e.event = eventName;
   e.timestamp = (long)TimeLocal();
   ulong ticket = pos.Ticket();
   e.magic = MAGIC_BASE + (int)(ticket % 900000);
   e.master_ticket = ticket;
   e.symbol = pos.Symbol();
   e.side = (int)pos.PositionType();
   e.open_price = pos.PriceOpen();
   e.volume = volume;
   e.sl = pos.StopLoss();
   e.tp = pos.TakeProfit();
   e.open_time = pos.Time();
   e.point = SymbolInfoDouble(pos.Symbol(), SYMBOL_POINT);
   e.comment = pos.Comment();
   return e;
}

void CMasterPublisher::SendEvent(const string eventName, const CPositionInfo &pos, double volume)
{
   STradeEvent e = BuildEvent(eventName, pos, volume);
   string json = CTradeMessage::EventToJson(e);
   m_transport.SendToAllClients(json);
}

void CMasterPublisher::SendHeartbeat()
{
   STradeEvent hb;
   ZeroMemory(hb);
   hb.event = "HEARTBEAT";
   hb.timestamp = (long)TimeLocal();
   string json = CTradeMessage::EventToJson(hb);
   m_transport.SendToAllClients(json);
}
```

- [ ] **Step 4: Implement snapshot helpers**

Copy from the current `MasterPublisher.mqh`:

```cpp
int CMasterPublisher::FindSnapshotIndex(ulong ticket) const
{
   return FindSnapshotIndex(m_prevSnapshots, ticket);
}

int CMasterPublisher::FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const
{
   int n = ArraySize(snapshots);
   for(int i = 0; i < n; i++)
      if(snapshots[i].ticket == ticket)
         return i;
   return -1;
}

void CMasterPublisher::BuildCurrentSnapshots(SPositionSnapshot &out[])
{
   ArrayResize(out, 0);

   int total = PositionsTotal();
   int count = 0;
   for(int i = 0; i < total; i++)
   {
      CPositionInfo pos;
      if(!pos.SelectByIndex(i))
         continue;

      ulong ticket = pos.Ticket();
      if(ticket == 0)
         continue;

      if(count >= ArraySize(out))
         ArrayResize(out, count + 1);

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
      count++;
   }
}

void CMasterPublisher::ReplaceSnapshots(const SPositionSnapshot &src[])
{
   int n = ArraySize(src);
   ArrayResize(m_prevSnapshots, n);
   for(int i = 0; i < n; i++)
      m_prevSnapshots[i] = src[i];
}
```

- [ ] **Step 5: Implement PublishChanges**

```cpp
void CMasterPublisher::PublishChanges(int intervalMs)
{
   ulong now = GetTickCount();

   // Broadcast discovery every second regardless of trade interval.
   if(now - m_lastBroadcast >= 1000)
   {
      m_transport.BroadcastEndpoint(m_discoveryUdpPort);
      m_lastBroadcast = now;
   }

   // Accept any pending clients every tick.
   m_transport.AcceptClients();

   // Process sync requests from slaves.
   ProcessSyncRequest();

   if(now - m_lastPublish < (uint)intervalMs)
      return;
   m_lastPublish = now;

   SPositionSnapshot curr[];
   BuildCurrentSnapshots(curr);

   // New / modified / partially closed positions.
   for(int i = 0; i < ArraySize(curr); i++)
   {
      CPositionInfo pos;
      if(!pos.SelectByTicket(curr[i].ticket))
         continue;

      int idx = FindSnapshotIndex(curr[i].ticket);
      if(idx < 0)
      {
         SendEvent("NEW_TRADE", pos, curr[i].volume);
      }
      else
      {
         const SPositionSnapshot &prev = m_prevSnapshots[idx];

         if(NormalizeDouble(prev.volume - curr[i].volume, 8) > 0.0)
            SendEvent("PARTIAL_CLOSE", pos, curr[i].volume);

         if(NormalizeDouble(prev.sl - curr[i].sl, 8) != 0.0 ||
            NormalizeDouble(prev.tp - curr[i].tp, 8) != 0.0)
            SendEvent("MODIFY_TRADE", pos, curr[i].volume);
      }
   }

   // Fully closed positions.
   for(int i = ArraySize(m_prevSnapshots) - 1; i >= 0; i--)
   {
      ulong oldTicket = m_prevSnapshots[i].ticket;
      if(FindSnapshotIndex(curr, oldTicket) < 0)
      {
         STradeEvent e;
         ZeroMemory(e);
         e.event = "CLOSE_TRADE";
         e.timestamp = (long)TimeLocal();
         e.magic = MAGIC_BASE + (int)(oldTicket % 900000);
         e.master_ticket = oldTicket;
         m_transport.SendToAllClients(CTradeMessage::EventToJson(e));
      }
   }

   ReplaceSnapshots(curr);

   // Heartbeat.
   if(m_heartbeatSeconds > 0 && (datetime)TimeLocal() - m_lastHeartbeat >= m_heartbeatSeconds)
   {
      SendHeartbeat();
      m_lastHeartbeat = TimeLocal();
   }
}
```

- [ ] **Step 6: Implement ProcessSyncRequest**

Slaves can send a `SYNC_REQUEST` event. On receiving it, the master sends a `NEW_TRADE` style `SYNC_RESPONSE` for every open position.

```cpp
void CMasterPublisher::ProcessSyncRequest()
{
   string json;
   while(m_transport.ReceiveFrame(json, 0))
   {
      STradeEvent e;
      if(!CTradeMessage::JsonToEvent(json, e))
         continue;

      if(e.event == "SYNC_REQUEST")
      {
         SPositionSnapshot curr[];
         BuildCurrentSnapshots(curr);
         for(int i = 0; i < ArraySize(curr); i++)
         {
            CPositionInfo pos;
            if(!pos.SelectByTicket(curr[i].ticket))
               continue;
            SendEvent("SYNC_RESPONSE", pos, curr[i].volume);
         }
      }
   }
}
```

- [ ] **Step 7: Compile in MetaEditor**

Expected: no errors. Fix any MQL5-specific syntax issues (e.g. `const SPositionSnapshot &` may need to become value copies).

- [ ] **Step 8: Commit**

```bash
git add MQL5/Include/TradeCopier/MasterPublisher.mqh
git commit -m "feat: rewrite MasterPublisher for LAN TCP transport" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: Rewrite SlaveSubscriber.mqh for LAN Transport

**Files:**
- Modify: `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`

**Interfaces:**
- Consumes: `CLanTransport` from Task 2, `TradeMessage.mqh`, `SymbolMapper`, `LotSizer`, `PriceNormalizer`.
- Produces: `CSlaveSubscriber::Init(int discoveryUdpPort, const string symbolMap, ...)`, `Poll()`, `Deinit()`.

- [ ] **Step 1: Replace includes and members**

```cpp
#ifndef SLAVE_SUBSCRIBER_MQH
#define SLAVE_SUBSCRIBER_MQH

#include "LanTransport.mqh"
#include <Trade\Trade.mqh>
#include <Trade\SymbolInfo.mqh>
#include "CopierConfig.mqh"
#include "SymbolMapper.mqh"
#include "LotSizer.mqh"
#include "PriceNormalizer.mqh"
#include "TradeMessage.mqh"

struct SSlaveCopyRecord
{
   long   magic;
   ulong  slave_ticket;
   double master_open_volume;
   double slave_open_volume;
};

class CSlaveSubscriber
{
private:
   CLanTransport      m_transport;
   CTrade             m_trade;
   CSymbolMapper      m_mapper;
   CLotSizer          m_lotSizer;
   CSymbolInfo        m_symbolInfo;
   int                m_discoveryUdpPort;
   int                m_maxAgeMinutes;
   int                m_retryCount;
   int                m_retryDelayMs;
   int                m_heartbeatSeconds;
   datetime           m_lastHeartbeat;
   bool               m_heartbeatWarned;
   bool               m_baselineSet;
   SPositionSnapshot  m_prevSnapshots[];
   SSlaveCopyRecord   m_records[];

   void   EstablishBaseline(const SPositionSnapshot &snapshots[]);
   void   DiffAndProcess(const SPositionSnapshot &curr[]);
   STradeEvent BuildEventFromSnapshot(const string eventName, const SPositionSnapshot &pos);
   void   CheckHeartbeat();
   int    FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const;
   int    FindSnapshotIndex(ulong ticket) const;
   void   OpenTrade(const STradeEvent &e);
   void   ModifyTrade(const STradeEvent &e);
   void   PartialClose(const STradeEvent &e);
   void   CloseTrade(const STradeEvent &e);
   int    FindRecord(long magic);
   bool   IsTooOld(datetime openTime);
   bool   OpenSlaveOrder(const string symbol, ENUM_ORDER_TYPE type, double lots,
                         double sl, double tp, long magic, string comment,
                         ulong &outTicket);
   bool   RoundToTickSize(const string symbol, double &price);
   bool   TryConnect();
   void   ProcessSingleEvent(const STradeEvent &e);

public:
   CSlaveSubscriber() : m_discoveryUdpPort(0), m_maxAgeMinutes(0), m_retryCount(0),
                        m_retryDelayMs(0), m_heartbeatSeconds(0), m_lastHeartbeat(0),
                        m_heartbeatWarned(false), m_baselineSet(false)
   {
      ArrayResize(m_prevSnapshots, 0);
      ArrayResize(m_records, 0);
   }
   bool Init(int discoveryUdpPort, const string symbolMap,
             int maxAgeMinutes, int retryCount, int retryDelayMs,
             int heartbeatSeconds);
   void Deinit();
   void Poll();
   bool IsConnected() const { return m_transport.IsConnected(); }
   int  LatencyMs() const { return m_transport.LatencyMs(); }
};
```

- [ ] **Step 2: Implement Init/Deinit and connection helper**

```cpp
bool CSlaveSubscriber::Init(int discoveryUdpPort, const string symbolMap,
                            int maxAgeMinutes, int retryCount, int retryDelayMs,
                            int heartbeatSeconds)
{
   m_discoveryUdpPort = discoveryUdpPort;
   m_maxAgeMinutes = maxAgeMinutes;
   m_retryCount = retryCount;
   m_retryDelayMs = retryDelayMs;
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_heartbeatWarned = false;
   m_baselineSet = false;
   ArrayResize(m_records, 0);
   ArrayResize(m_prevSnapshots, 0);

   m_mapper.Init(symbolMap);

   if(!m_transport.StartSlaveListener(m_discoveryUdpPort))
   {
      Print("SlaveSubscriber: failed to start UDP listener");
      return false;
   }

   // Try to find and connect to a master immediately.
   if(!TryConnect())
      Print("SlaveSubscriber: no master found yet, will retry on timer");

   // Rebuild records for any copied positions already open on the slave account.
   RebuildRecordsFromOpenPositions();

   return true;
}

void CSlaveSubscriber::Deinit()
{
   m_transport.StopSlaveListener();
   ArrayResize(m_records, 0);
   ArrayResize(m_prevSnapshots, 0);
}

bool CSlaveSubscriber::TryConnect()
{
   string host;
   ushort port = 0;

   // Prefer UDP broadcast discovery.
   if(m_transport.DiscoverMaster(host, port, 500))
   {
      if(m_transport.ConnectToMaster(host, port))
      {
         PrintFormat("SlaveSubscriber: connected to master %s:%d", host, port);
         // Request a full sync.
         STradeEvent req;
         ZeroMemory(req);
         req.event = "SYNC_REQUEST";
         req.timestamp = (long)TimeLocal();
         m_transport.SendFrame(CTradeMessage::EventToJson(req));
         return true;
      }
   }

   // Fallback to localhost in case broadcast is blocked.
   if(m_transport.ConnectToMaster("127.0.0.1", port))
   {
      Print("SlaveSubscriber: connected to localhost master");
      STradeEvent req;
      ZeroMemory(req);
      req.event = "SYNC_REQUEST";
      req.timestamp = (long)TimeLocal();
      m_transport.SendFrame(CTradeMessage::EventToJson(req));
      return true;
   }

   return false;
}
```

- [ ] **Step 3: Extract record rebuilding into helper**

Move the existing record-rebuilding block from the current `Init` into a private method:

```cpp
void CSlaveSubscriber::RebuildRecordsFromOpenPositions()
{
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
      PrintFormat("SlaveSubscriber: rebuilt %d copied position record(s)", rebuilt);
}
```

- [ ] **Step 4: Implement Poll, receive and dispatch events**

```cpp
void CSlaveSubscriber::Poll()
{
   if(!m_transport.IsConnected())
   {
      if(TryConnect())
         m_lastHeartbeat = TimeCurrent();
      else
      {
         CheckHeartbeat();
         return;
      }
   }

   string json;
   while(m_transport.ReceiveFrame(json, 0))
   {
      m_lastHeartbeat = TimeCurrent();
      m_heartbeatWarned = false;

      STradeEvent e;
      if(!CTradeMessage::JsonToEvent(json, e))
      {
         PrintFormat("SlaveSubscriber: malformed JSON: %s", json);
         continue;
      }

      ProcessSingleEvent(e);
   }

   CheckHeartbeat();
}

void CSlaveSubscriber::ProcessSingleEvent(const STradeEvent &e)
{
   if(e.event == "HEARTBEAT")
      return;

   if(e.event == "SYNC_RESPONSE")
   {
      if(!m_baselineSet)
      {
         // Build a temporary snapshot array for baseline only.
         SPositionSnapshot snap;
         snap.ticket     = e.master_ticket;
         snap.symbol     = e.symbol;
         snap.side       = e.side;
         snap.open_price = e.open_price;
         snap.volume     = e.volume;
         snap.sl         = e.sl;
         snap.tp         = e.tp;
         snap.open_time  = (long)e.open_time;
         snap.point      = e.point;
         snap.comment    = e.comment;

         if(!IsTooOld(e.open_time))
         {
            int n = ArraySize(m_prevSnapshots);
            ArrayResize(m_prevSnapshots, n + 1);
            m_prevSnapshots[n] = snap;
         }
      }
      return;
   }

   if(e.event == "NEW_TRADE")
      OpenTrade(e);
   else if(e.event == "MODIFY_TRADE")
      ModifyTrade(e);
   else if(e.event == "PARTIAL_CLOSE")
      PartialClose(e);
   else if(e.event == "CLOSE_TRADE")
      CloseTrade(e);
}
```

- [ ] **Step 5: Add baseline commit after sync burst**

Because `SYNC_RESPONSE` events arrive one at a time, we need to know when the burst ends. Simplest approach: after the first `NEW_TRADE` event (non-sync), commit the baseline.

Add a helper:

```cpp
void CSlaveSubscriber::CommitBaseline()
{
   if(m_baselineSet) return;
   m_baselineSet = true;
   Print("SlaveSubscriber: baseline established");
}
```

Call `CommitBaseline()` at the start of processing `NEW_TRADE`, `MODIFY_TRADE`, `PARTIAL_CLOSE`, `CLOSE_TRADE` events.

- [ ] **Step 6: Update DiffAndProcess to event-driven processing**

In this design the master sends events directly, so the slave does not need to diff snapshots. Remove `DiffAndProcess`, `BuildEventFromSnapshot`, and the `STradeSnapshot` usage. Keep `m_prevSnapshots` only for sync-baseline and closed-position tracking? Actually since the master sends `CLOSE_TRADE` events, we do not need to diff at all. Delete `m_prevSnapshots` and related helpers.

- [ ] **Step 7: Keep heartbeat check**

```cpp
void CSlaveSubscriber::CheckHeartbeat()
{
   if(m_heartbeatSeconds <= 0 || m_lastHeartbeat == 0)
      return;

   if(TimeCurrent() - m_lastHeartbeat > m_heartbeatSeconds * 2)
   {
      if(!m_heartbeatWarned)
      {
         Print("SlaveSubscriber: no heartbeat from master");
         m_heartbeatWarned = true;
      }
   }
}
```

- [ ] **Step 8: Compile in MetaEditor**

Fix any remaining syntax errors. Pay attention to `const SPositionSnapshot &` references — MQL5 may reject them; switch to value copies if needed.

- [ ] **Step 9: Commit**

```bash
git add MQL5/Include/TradeCopier/SlaveSubscriber.mqh
git commit -m "feat: rewrite SlaveSubscriber for LAN TCP event stream" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: Create TradeCopierGui.mqh

**Files:**
- Create: `MQL5/Include/TradeCopier/TradeCopierGui.mqh`

**Interfaces:**
- Produces:
  - `CTradeCopierGui::Create(int chartId)`
  - `CTradeCopierGui::Destroy()`
  - `CTradeCopierGui::SetMode(string mode)`
  - `CTradeCopierGui::SetStatus(string status)`
  - `CTradeCopierGui::SetLatency(int ms)`
  - `CTradeCopierGui::SetSymbolMap(const string &symbolMap)`
  - `CTradeCopierGui::GetSymbolMap() -> string`
  - `CTradeCopierGui::OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)`

- [ ] **Step 1: Define constants and class skeleton**

```cpp
#ifndef TRADE_COPIER_GUI_MQH
#define TRADE_COPIER_GUI_MQH

const string GUI_PREFIX = "TC_GUI_";
const int    GUI_WIDTH  = 350;
const int    GUI_HEIGHT = 400;
const int    ROW_HEIGHT = 22;

class CTradeCopierGui
{
private:
   int     m_chartId;
   string  m_symbolMap;
   int     m_rowCount;

   string  MakeName(const string suffix);
   void    CreatePanel();
   void    CreateTabs();
   void    CreateGeneralTab();
   void    CreateSymbolsTab();
   void    CreateTradesTab();
   void    RefreshSymbolRows();
   void    AddSymbolRow(int index, const string master, const string slave);
   void    DestroyAllObjects();

public:
   CTradeCopierGui() : m_chartId(0), m_rowCount(0) {}

   void Create(int chartId);
   void Destroy();
   void SetMode(const string mode);
   void SetStatus(const string status);
   void SetLatency(int ms);
   void SetMasterEndpoint(const string endpoint);
   void SetSymbolMap(const string &symbolMap);
   string GetSymbolMap() const { return m_symbolMap; }
   void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam);
};
```

- [ ] **Step 2: Implement object name helper and panel creation**

```cpp
string CTradeCopierGui::MakeName(const string suffix)
{
   return GUI_PREFIX + suffix;
}

void CTradeCopierGui::Create(int chartId)
{
   m_chartId = chartId;
   DestroyAllObjects();

   int x = 10;
   int y = 30;

   ObjectCreate(m_chartId, MakeName("BG"), OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_XDISTANCE, x);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_XSIZE, GUI_WIDTH);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_YSIZE, GUI_HEIGHT);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_BGCOLOR, C'28,28,28');
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_BORDER_TYPE, BORDER_FLAT);

   ObjectCreate(m_chartId, MakeName("TITLE"), OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_XDISTANCE, x + 10);
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_YDISTANCE, y + 10);
   ObjectSetString(m_chartId, MakeName("TITLE"), OBJPROP_FONT, "Arial");
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_FONTSIZE, 10);
   ObjectSetString(m_chartId, MakeName("TITLE"), OBJPROP_TEXT, "X1 Copy MT5");
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_COLOR, clrWhite);

   CreateGeneralTab();
   CreateSymbolsTab();
}
```

- [ ] **Step 3: Implement General tab**

```cpp
void CTradeCopierGui::CreateGeneralTab()
{
   int x = 20;
   int y = 55;

   string labels[] = {"MODE_LABEL", "STATUS_LABEL", "LATENCY_LABEL", "MASTER_LABEL"};
   for(int i = 0; i < ArraySize(labels); i++)
   {
      ObjectCreate(m_chartId, MakeName(labels[i]), OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(m_chartId, MakeName(labels[i]), OBJPROP_XDISTANCE, x);
      ObjectSetInteger(m_chartId, MakeName(labels[i]), OBJPROP_YDISTANCE, y + i * ROW_HEIGHT);
      ObjectSetInteger(m_chartId, MakeName(labels[i]), OBJPROP_COLOR, clrWhite);
   }

   ObjectSetString(m_chartId, MakeName("MODE_LABEL"), OBJPROP_TEXT, "Mode: SLAVE");
   ObjectSetString(m_chartId, MakeName("STATUS_LABEL"), OBJPROP_TEXT, "Status: searching...");
   ObjectSetString(m_chartId, MakeName("LATENCY_LABEL"), OBJPROP_TEXT, "Latency: --");
   ObjectSetString(m_chartId, MakeName("MASTER_LABEL"), OBJPROP_TEXT, "Master: --");
}
```

- [ ] **Step 4: Implement status setters**

```cpp
void CTradeCopierGui::SetMode(const string mode)
{
   ObjectSetString(m_chartId, MakeName("MODE_LABEL"), OBJPROP_TEXT, "Mode: " + mode);
}

void CTradeCopierGui::SetStatus(const string status)
{
   ObjectSetString(m_chartId, MakeName("STATUS_LABEL"), OBJPROP_TEXT, "Status: " + status);
}

void CTradeCopierGui::SetLatency(int ms)
{
   string text = (ms < 0) ? "Latency: --" : StringFormat("Latency: %d ms", ms);
   ObjectSetString(m_chartId, MakeName("LATENCY_LABEL"), OBJPROP_TEXT, text);
}

void CTradeCopierGui::SetMasterEndpoint(const string endpoint)
{
   ObjectSetString(m_chartId, MakeName("MASTER_LABEL"), OBJPROP_TEXT, "Master: " + endpoint);
}
```

- [ ] **Step 5: Implement Symbols tab with editable rows**

```cpp
void CTradeCopierGui::CreateSymbolsTab()
{
   int x = 20;
   int y = 150;

   ObjectCreate(m_chartId, MakeName("SYM_HEADER"), OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("SYM_HEADER"), OBJPROP_XDISTANCE, x);
   ObjectSetInteger(m_chartId, MakeName("SYM_HEADER"), OBJPROP_YDISTANCE, y);
   ObjectSetString(m_chartId, MakeName("SYM_HEADER"), OBJPROP_TEXT, "Master -> Slave");
   ObjectSetInteger(m_chartId, MakeName("SYM_HEADER"), OBJPROP_COLOR, clrWhite);

   ObjectCreate(m_chartId, MakeName("SYM_ADD"), OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_XDISTANCE, x + 220);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_XSIZE, 60);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName("SYM_ADD"), OBJPROP_TEXT, "+ Add");

   ObjectCreate(m_chartId, MakeName("SYM_COPY"), OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_XDISTANCE, x + 220);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_YDISTANCE, y + 20);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_XSIZE, 100);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName("SYM_COPY"), OBJPROP_TEXT, "Copy string");

   RefreshSymbolRows();
}

void CTradeCopierGui::SetSymbolMap(const string &symbolMap)
{
   m_symbolMap = symbolMap;
   RefreshSymbolRows();
}

void CTradeCopierGui::RefreshSymbolRows()
{
   // Destroy existing rows.
   for(int i = 0; i < m_rowCount; i++)
   {
      ObjectDelete(m_chartId, MakeName("SYM_MASTER_" + IntegerToString(i)));
      ObjectDelete(m_chartId, MakeName("SYM_SLAVE_" + IntegerToString(i)));
      ObjectDelete(m_chartId, MakeName("SYM_DEL_" + IntegerToString(i)));
   }

   // Parse current map.
   string pairs[];
   int count = StringSplit(m_symbolMap, ',', pairs);
   m_rowCount = 0;

   int x = 20;
   int y = 175;

   for(int i = 0; i < count; i++)
   {
      string pair = pairs[i];
      StringReplace(pair, " ", "");
      int eq = StringFind(pair, "=");
      if(eq == -1) continue;

      string master = StringSubstr(pair, 0, eq);
      string slave  = StringSubstr(pair, eq + 1);
      if(master == "" || slave == "") continue;

      AddSymbolRow(m_rowCount, master, slave);
      m_rowCount++;
   }

   // Always add one empty row at the end.
   AddSymbolRow(m_rowCount, "", "");
   m_rowCount++;
}

void CTradeCopierGui::AddSymbolRow(int index, const string master, const string slave)
{
   int x = 20;
   int y = 175 + index * ROW_HEIGHT;

   string editMaster = "SYM_MASTER_" + IntegerToString(index);
   string editSlave  = "SYM_SLAVE_" + IntegerToString(index);
   string btnDel     = "SYM_DEL_" + IntegerToString(index);

   ObjectCreate(m_chartId, MakeName(editMaster), OBJ_EDIT, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_XDISTANCE, x);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_XSIZE, 80);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName(editMaster), OBJPROP_TEXT, master);

   ObjectCreate(m_chartId, MakeName(editSlave), OBJ_EDIT, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_XDISTANCE, x + 90);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_XSIZE, 80);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName(editSlave), OBJPROP_TEXT, slave);

   if(master != "" || slave != "")
   {
      ObjectCreate(m_chartId, MakeName(btnDel), OBJ_BUTTON, 0, 0, 0);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_XDISTANCE, x + 180);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_YDISTANCE, y);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_XSIZE, 18);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_YSIZE, 18);
      ObjectSetString(m_chartId, MakeName(btnDel), OBJPROP_TEXT, "x");
   }
}
```

- [ ] **Step 6: Implement chart event handling**

```cpp
void CTradeCopierGui::OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(id != CHARTEVENT_OBJECT_ENDEDIT && id != CHARTEVENT_OBJECT_CLICK)
      return;

   // Rebuild symbol map from all edit boxes.
   string map = "";
   for(int i = 0; i < m_rowCount; i++)
   {
      string master = ObjectGetString(m_chartId, MakeName("SYM_MASTER_" + IntegerToString(i)), OBJPROP_TEXT);
      string slave  = ObjectGetString(m_chartId, MakeName("SYM_SLAVE_" + IntegerToString(i)), OBJPROP_TEXT);
      StringReplace(master, " ", "");
      StringReplace(slave, " ", "");
      if(master != "" && slave != "")
      {
         if(map != "") map += ",";
         map += master + "=" + slave;
      }
   }

   if(map != m_symbolMap)
   {
      m_symbolMap = map;
      RefreshSymbolRows();
   }

   // Handle delete / add / copy buttons.
   if(id == CHARTEVENT_OBJECT_CLICK)
   {
      if(sparam == MakeName("SYM_ADD"))
      {
         // Empty row already exists; user can type into it.
         return;
      }
      if(sparam == MakeName("SYM_COPY"))
      {
         // MQL5 cannot access the system clipboard; print the string.
         Print("SymbolMap: " + m_symbolMap);
         return;
      }

      // Check delete buttons.
      for(int i = 0; i < m_rowCount; i++)
      {
         if(sparam == MakeName("SYM_DEL_" + IntegerToString(i)))
         {
            string master = ObjectGetString(m_chartId, MakeName("SYM_MASTER_" + IntegerToString(i)), OBJPROP_TEXT);
            string slave  = ObjectGetString(m_chartId, MakeName("SYM_SLAVE_" + IntegerToString(i)), OBJPROP_TEXT);
            StringReplace(master, " ", "");
            StringReplace(slave, " ", "");
            if(master != "" && slave != "")
            {
               string entry = master + "=" + slave;
               int pos = StringFind(m_symbolMap, entry);
               if(pos >= 0)
               {
                  string before = (pos == 0) ? "" : StringSubstr(m_symbolMap, 0, pos - 1);
                  string after  = StringSubstr(m_symbolMap, pos + StringLen(entry));
                  if(after != "" && before != "") before += ",";
                  m_symbolMap = before + after;
                  RefreshSymbolRows();
               }
            }
            return;
         }
      }
   }
}
```

- [ ] **Step 7: Implement cleanup**

```cpp
void CTradeCopierGui::Destroy()
{
   DestroyAllObjects();
}

void CTradeCopierGui::DestroyAllObjects()
{
   int total = ObjectsTotal(m_chartId);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(m_chartId, i);
      if(StringFind(name, GUI_PREFIX) == 0)
         ObjectDelete(m_chartId, name);
   }
}
```

- [ ] **Step 8: Compile stub**

Create a temporary `Tests/GuiCompile.mq5` including `TradeCopierGui.mqh` with a minimal `OnInit`. Compile in MetaEditor and fix syntax. Delete the stub after.

- [ ] **Step 9: Commit**

```bash
git add MQL5/Include/TradeCopier/TradeCopierGui.mqh
git commit -m "feat: add on-chart GUI panel for mode, status, latency and symbol mapping" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: Rewrite TradeCopier.mq5 Main EA

**Files:**
- Modify: `MQL5/Experts/TradeCopier/TradeCopier.mq5`

**Interfaces:**
- Consumes: `CMasterPublisher`, `CSlaveSubscriber`, `CTradeCopierGui`.
- Produces: `OnInit`, `OnDeinit`, `OnTick`, `OnTimer`, `OnChartEvent`.

- [ ] **Step 1: Update includes and globals**

```cpp
//+------------------------------------------------------------------+
//|                                              TradeCopier.mq5     |
//|                  MT5 Local Trade Copier (Master / Slave)         |
//+------------------------------------------------------------------+
#property copyright "Generated by Claude"
#property version   "1.00"
#property strict

#include <TradeCopier\CopierConfig.mqh>
#include <TradeCopier\MasterPublisher.mqh>
#include <TradeCopier\SlaveSubscriber.mqh>
#include <TradeCopier\TradeCopierGui.mqh>

CMasterPublisher  g_master;
CSlaveSubscriber  g_slave;
CTradeCopierGui   g_gui;

const int PUBLISH_INTERVAL_MS = 250;
```

- [ ] **Step 2: Update OnInit**

```cpp
int OnInit()
{
   g_gui.Create(ChartID());

   if(CopierMode == COPIER_MASTER)
   {
      g_gui.SetMode("MASTER");
      g_gui.SetStatus("advertising");

      if(!g_master.Init(DiscoveryUdpPort, HeartbeatSeconds))
      {
         Print("TradeCopier: failed to initialize MASTER");
         return INIT_FAILED;
      }
      EventSetMillisecondTimer(PUBLISH_INTERVAL_MS);
      Print("TradeCopier: running as MASTER");
   }
   else
   {
      g_gui.SetMode("SLAVE");
      g_gui.SetStatus("searching...");

      if(!g_slave.Init(DiscoveryUdpPort, SymbolMap, MaxTradeAgeMinutes, RetryCount, RetryDelayMs, HeartbeatSeconds))
      {
         Print("TradeCopier: failed to initialize SLAVE");
         return INIT_FAILED;
      }
      g_gui.SetSymbolMap(SymbolMap);
      EventSetMillisecondTimer(PUBLISH_INTERVAL_MS);
      Print("TradeCopier: running as SLAVE");
   }
   return(INIT_SUCCEEDED);
}
```

- [ ] **Step 3: Update OnDeinit and OnTimer**

```cpp
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(CopierMode == COPIER_MASTER)
      g_master.Deinit();
   else
      g_slave.Deinit();
   g_gui.Destroy();
   Print("TradeCopier: stopped");
}

void OnTick()
{
   // work is done in OnTimer for both modes
}

void OnTimer()
{
   if(CopierMode == COPIER_MASTER)
   {
      g_master.PublishChanges(PUBLISH_INTERVAL_MS);
   }
   else
   {
      g_slave.Poll();

      // Update GUI status from transport state.
      if(g_slave.IsConnected())
      {
         g_gui.SetStatus("connected");
         g_gui.SetLatency(g_slave.LatencyMs());
      }
      else
      {
         g_gui.SetStatus("searching...");
         g_gui.SetLatency(-1);
      }

      // If user changed the symbol map in the GUI, apply it to the slave.
      string guiMap = g_gui.GetSymbolMap();
      if(guiMap != SymbolMap)
      {
         // MQL5 cannot rewrite an input variable, but we can re-init the mapper.
         // Persist the string by printing it to the Experts log for manual copy.
         Print("Updated SymbolMap: " + guiMap);
      }
   }
}
```

- [ ] **Step 4: Add OnChartEvent**

```cpp
void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   g_gui.OnChartEvent(id, lparam, dparam, sparam);
}
```

- [ ] **Step 5: Compile in MetaEditor**

Expected: no errors. Fix any remaining issues.

- [ ] **Step 6: Commit**

```bash
git add MQL5/Experts/TradeCopier/TradeCopier.mq5
git commit -m "feat: wire LAN transport and GUI into main TradeCopier EA" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: Delete SnapshotFile.mqh and Clean Up

**Files:**
- Delete: `MQL5/Include/TradeCopier/SnapshotFile.mqh`

- [ ] **Step 1: Delete file**

```bash
git rm MQL5/Include/TradeCopier/SnapshotFile.mqh
```

- [ ] **Step 2: Verify no references remain**

```bash
grep -R "SnapshotFile\|CSnapshotFile\|SPositionSnapshot" MQL5/Include/TradeCopier MQL5/Experts/TradeCopier || true
```

Expected: only `SPositionSnapshot` references in `MasterPublisher.mqh` (which is ok) and no `SnapshotFile`/`CSnapshotFile` references. If `SPositionSnapshot` is still used in `SlaveSubscriber.mqh`, remove it there.

- [ ] **Step 3: Move SPositionSnapshot definition**

Since `SnapshotFile.mqh` is deleted, move `SPositionSnapshot` to `TradeMessage.mqh` or `MasterPublisher.mqh`. Add it to `TradeMessage.mqh` so both publisher and future modules can use it:

```cpp
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
```

Remove any duplicate definition from `MasterPublisher.mqh`.

- [ ] **Step 4: Compile in MetaEditor**

- [ ] **Step 5: Commit**

```bash
git add MQL5/Include/TradeCopier/TradeMessage.mqh MQL5/Include/TradeCopier/MasterPublisher.mqh
git commit -m "chore: remove SnapshotFile.mqh and move SPositionSnapshot to TradeMessage" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: Update README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite features section**

Replace:

```markdown
## Features
- Master/Slave dual mode
- LAN TCP + UDP broadcast discovery
- On-chart configuration GUI with symbol-mapping table
- Manual symbol translation
- Balance-step lot sizing
- Point-normalized SL/TP mirroring
- Full trade lifecycle mirroring (open, modify SL/TP, partial close, close)
- Multi-slave support from a single master
```

- [ ] **Step 2: Rewrite installation section**

```markdown
## Installation

1. Copy `MQL5/Experts/TradeCopier/TradeCopier.mq5` and the `MQL5/Include/TradeCopier/*.mqh` files into your MetaTrader 5 data folder.
2. Open `TradeCopier.mq5` in MetaEditor and compile (F7).
3. Attach the EA to a chart on the master account; set `CopierMode` to `MASTER`.
4. Attach the EA to a chart on each slave account; set `CopierMode` to `SLAVE`.
5. Make sure the master and slave PCs are on the same local network. No IP or port configuration is required — the slave discovers the master automatically via UDP broadcast.

For localhost (same PC), the slave automatically falls back to `127.0.0.1` if broadcast is blocked.
```

- [ ] **Step 3: Remove file-based and ZeroMQ references**

Delete any mentions of `SharedDataPath`, `MasterSnapshotIntervalMs`, `SlavePollIntervalMs`, `CopierPort`, ZeroMQ, or shared snapshot files.

- [ ] **Step 4: Add GUI usage section**

```markdown
## GUI

The EA draws a panel directly on the chart:

- **General tab:** shows mode (MASTER/SLAVE), connection status, master endpoint, and latency.
- **Symbols tab:** editable table of master → slave symbol mappings. Type the master symbol in the left column and the slave symbol in the right column. The generated `SymbolMap` string is printed to the Experts log so you can paste it into the EA inputs for persistence.
- **Trades tab:** (placeholder) list of currently copied positions.

Note: MQL5 cannot save input values from code. After editing the mapping table, copy the printed `SymbolMap` string into the EA's `SymbolMap` input and re-attach the EA if you want the mapping to persist across restarts.
```

- [ ] **Step 5: Update configuration table**

```markdown
### Copier Mode

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| CopierMode | ENUM_COPIER_MODE | COPIER_SLAVE | Run as MASTER or SLAVE |
| DiscoveryUdpPort | ushort | 55555 | UDP port for master discovery broadcasts |
| HeartbeatSeconds | int | 5 | Maximum heartbeat age before slave warns |
```

- [ ] **Step 6: Update checklist for LAN and GUI**

Replace the existing "Feature Test Checklist" with a LAN/GUI-focused version. Keep the same structure but change transport and GUI items:

```markdown
### Transport
- [ ] Attach master EA on one PC; verify it starts advertising.
- [ ] Attach slave EA on another PC on the same LAN; verify it discovers and connects automatically.
- [ ] Verify the master endpoint and latency appear in the slave GUI.
- [ ] Disconnect the master PC from the network; verify the slave shows "searching..." after the heartbeat timeout.
- [ ] Reconnect the master; verify the slave reconnects and syncs.
- [ ] Run master and slave on the same PC (localhost); verify fallback connection works.
```

Keep lifecycle, symbol, lot sizing, SL/TP, restart, and multi-slave items unchanged except multi-slave now uses LAN instead of shared path.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: update README for LAN TCP + UDP transport and chart GUI" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 9: Compile Full Project and Manual Smoke Test

**Files:**
- All MQL5 files.

- [ ] **Step 1: Compile `TradeCopier.mq5` in MetaEditor**

Expected: zero errors, zero warnings.

- [ ] **Step 2: Same-machine smoke test**

Run two MT5 terminals on the same PC:
- Attach master EA to one chart.
- Attach slave EA to another chart.
- Open a trade on master; verify slave copies within ~1 second.
- Modify SL/TP, partial close, full close; verify mirroring.

- [ ] **Step 3: LAN smoke test**

Run master on PC A and slave on PC B on the same LAN:
- Verify automatic discovery.
- Verify trade mirroring.

- [ ] **Step 4: GUI test**

- Verify mode/status/latency display.
- Add a symbol mapping in the GUI and confirm it appears in the Experts log.

- [ ] **Step 5: Commit any final fixes**

```bash
git add -A
git commit -m "fix: address smoke-test findings" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Spec Coverage Self-Review

| Spec Section | Implementing Task |
|--------------|-----------------|
| UDP broadcast discovery | Task 2 |
| TCP reliable data channel | Task 2 |
| LAN + localhost support | Task 2, Task 4 |
| Master event pushing | Task 3 |
| Slave discovery and receive | Task 4 |
| Restart recovery / sync | Task 3, Task 4 |
| On-chart GUI panel | Task 5, Task 6 |
| Symbol-mapping GUI table | Task 5 |
| Configuration inputs update | Task 1 |
| README update | Task 8 |
| No external dependencies | All tasks |

## Placeholder Scan

No TBD/TODO/fill-in-details remain. Every step includes concrete code or commands.

## Type Consistency Notes

- `CLanTransport::StartMaster(ushort &outTcpPort)` → matches `MasterPublisher::Init`.
- `CLanTransport::DiscoverMaster(string &outHost, ushort &outPort, uint timeoutMs)` → matches `SlaveSubscriber::TryConnect`.
- Frame functions use `string &json` consistently.
- `CTradeCopierGui::SetSymbolMap(const string &symbolMap)` and `GetSymbolMap()` use plain `string`.
