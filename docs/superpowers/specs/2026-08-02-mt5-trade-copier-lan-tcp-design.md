# MT5 Trade Copier — LAN TCP + UDP Broadcast Design

## Context

The existing MT5 Trade Copier was briefly converted to a file-based shared-snapshot transport. The user has decided that a file-based transport is unsuitable and wants a replacement that:

1. Requires **no manual connection configuration** between MetaTrader terminals.
2. Works across the **local area network** (LAN), not only on the same machine.
3. Still works when both terminals are on **localhost** as a special case.
4. Provides an **on-chart GUI panel** for configuration, including a symbol-mapping table.

## Goals

1. Replace the current file-based IPC with a LAN-aware transport.
2. Keep **zero-connection configuration**: a slave must automatically find a master on the same LAN.
3. Preserve all existing trade-copying features:
   - Manual symbol translation
   - Balance-step lot sizing
   - Raw price-distance SL/TP mirroring
   - Full lifecycle mirroring (open, modify SL/TP, partial close, close)
   - Restart recovery without duplicates
4. Add an on-chart GUI panel with:
   - Mode selection (MASTER / SLAVE)
   - Connection status and latency display
   - Editable symbol-mapping table
   - Transport settings (optional advanced section)
5. Use only built-in MQL5 features (no external DLLs or libraries).

## Non-Goals

- Cross-Internet / cross-NAT copying. The transport is LAN-only.
- Separate native Windows window. The UI is rendered as chart objects.
- Persisting GUI layout across terminals. Only settings such as `SymbolMap` are persisted via inputs.

## Architecture

### Transport: UDP Broadcast Discovery + TCP Data Channel

```
+--------+   UDP broadcast   +-------------+
| Master |  "MASTER:<tcp-port>"  |  Slave(s)   |
|        |  every ~1 s       |  (listen on  |
| TCP    |<------------------>|  UDP 55555)   |
| server |   TCP reliable    |               |
+--------+                   +---------------+
```

1. **Master**
   - Opens a TCP server socket on an **ephemeral port**.
   - Sends a UDP broadcast packet every second on the LAN broadcast address (e.g. `255.255.255.255`) containing its TCP endpoint: `MT5COPIER:<tcp-port>`.
   - Accepts incoming TCP connections from slaves.
   - Pushes serialized trade events (`NEW_TRADE`, `MODIFY_TRADE`, `PARTIAL_CLOSE`, `CLOSE_TRADE`, `HEARTBEAT`) over each TCP connection.

2. **Slave**
   - Listens on a fixed UDP port (`55555`) for master broadcasts.
   - On receiving a valid broadcast, connects to the advertised TCP port.
   - Receives events, derives state diffs locally, and executes trades.
   - Also tries `127.0.0.1` on the default discovery port so localhost works even if broadcast is blocked.

### Why not pure UDP?

Trade-copying events must not be lost. UDP discovery is fine for discovery, but the actual trade stream goes over TCP so every event arrives exactly once and in order.

### Why not file-based?

File-based IPC required the same filesystem / same MT5 installation. The user wants LAN support without shared folders.

## Protocol Details

### UDP Discovery

- Fixed UDP discovery port: `55555`.
- Broadcast address: `255.255.255.255`.
- Master packet format (UTF-8 string, newline terminated):
  ```
  MT5COPIER:<tcp-port>\n
  ```
  Example: `MT5COPIER:23456`
- Slave listens on UDP `55555`. When a packet is received, parses `tcp-port` and attempts a TCP connection.

### TCP Messages

Each TCP message is a length-prefixed JSON frame:

```
<4-byte little-endian length><JSON payload>
```

JSON payload is the existing `STradeEvent` serialized by `CTradeMessage::EventToJson`.

Events:
- `NEW_TRADE`
- `MODIFY_TRADE`
- `PARTIAL_CLOSE`
- `CLOSE_TRADE`
- `HEARTBEAT`
- `SYNC_REQUEST` (slave → master)
- `SYNC_RESPONSE` (master → slave, burst of all current positions)

## Modules to Change / Create

### 1. `MQL5/Include/TradeCopier/LanTransport.mqh` (new)

Responsibilities:
- Open/close UDP socket for broadcast (master) and listening (slave).
- Open/close TCP server (master) and TCP client (slave).
- Send discovery broadcasts (master).
- Receive discovery broadcasts (slave) and return the master endpoint.
- Send/receive length-prefixed JSON frames over TCP.

Interface (draft):
```cpp
class CLanTransport
{
public:
   // Master API
   bool StartMaster(ushort &outTcpPort);
   void StopMaster();
   bool BroadcastEndpoint(uint udpPort, ushort tcpPort);
   bool AcceptClients();
   bool SendToAllClients(const string &json);

   // Slave API
   bool StartSlaveListener(uint udpPort);
   void StopSlaveListener();
   bool DiscoverMaster(string &outHost, ushort &outPort, uint timeoutMs);
   bool ConnectToMaster(const string host, ushort port);
   bool ReceiveFrame(string &outJson, uint timeoutMs);
   bool SendFrame(const string &json);
};
```

Implementation uses MQL5 built-in `Socket*` functions (`SocketCreate`, `SocketBind`, `SocketConnect`, `SocketSend`, `SocketRead`, `SocketAccept`, `SocketClose`, etc.) and UDP sockets (`SOCKET_UDP`).

### 2. `MQL5/Include/TradeCopier/MasterPublisher.mqh` (rewrite)

Responsibilities:
- Start/stop `CLanTransport` master mode.
- On timer: scan positions, derive events, broadcast discovery, send events to all connected slaves.
- Handle `SYNC_REQUEST` by sending `SYNC_RESPONSE` bursts.
- Maintain a client list (accepted TCP sockets) inside `CLanTransport`.

### 3. `MQL5/Include/TradeCopier/SlaveSubscriber.mqh` (rewrite)

Responsibilities:
- Start/stop `CLanTransport` slave mode.
- On timer: discover master if not connected, receive frames, process events.
- Maintain previous state for diffing and restart recovery.

### 4. `MQL5/Experts/TradeCopier/TradeCopier.mq5` (rewrite)

Responsibilities:
- Remove file-based timer wiring.
- Initialize transport in MASTER or SLAVE mode.
- Create/manage the chart GUI panel.
- Pass GUI-updated `SymbolMap` to the slave subscriber.
- Handle `OnDeinit` cleanup of sockets + GUI.

### 5. `MQL5/Include/TradeCopier/CopierConfig.mqh` (update)

Replace file-based inputs with transport inputs:

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `CopierMode` | ENUM_COPIER_MODE | `COPIER_SLAVE` | MASTER or SLAVE |
| `DiscoveryUdpPort` | ushort | `55555` | UDP port for master discovery broadcasts |
| `HeartbeatSeconds` | int | `5` | Max heartbeat age before slave warns |
| `SymbolMap` | string | `""` | Symbol mappings: `US30=WS30, XAUUSD=GOLD` |
| `BalanceStepAmount` | double | `100.0` | Account-currency units per lot step |
| `BalanceStepSize` | double | `0.01` | Lot size added per balance step |
| `MaxLotSize` | double | `10.0` | Hard lot-size cap |
| `MaxTradeAgeMinutes` | int | `30` | Ignore master trades older than this on sync |
| `NormalizeSLTPByPriceDistance` | bool | `true` | Convert SL/TP via raw price distance |
| `RetryCount` | int | `3` | Order-send attempts |
| `RetryDelayMs` | int | `500` | Delay between retries (ms) |

Remove:
- `SharedDataPath`
- `MasterSnapshotIntervalMs`
- `SlavePollIntervalMs`

### 6. `MQL5/Include/TradeCopier/TradeCopierGui.mqh` (new)

Responsibilities:
- Create an on-chart panel using chart objects (labels, rectangles, edit boxes, buttons).
- Render tabs: General, Symbols, Trades.
- General tab: mode indicator, connection status, latency, start/stop button.
- Symbols tab: editable two-column table for master → slave symbol mapping.
- Trades tab: list of currently copied positions (slave ticket, symbol, volume, SL/TP).
- Convert the GUI symbol table into the `SymbolMap` string format and vice versa.
- Persist mapping changes back to the input by rewriting the `SymbolMap` input on chart property save (MQL5 cannot truly rewrite inputs, but we can document that the user copies the generated string).

Because MQL5 cannot persist inputs programmatically, the GUI will provide a **Copy mapping string** button that places the current table as a `US30=WS30, ...` string on the clipboard (or prints it so the user can paste it into the EA inputs). Alternatively, the EA reads `SymbolMap` at startup and writes updates into chart comments for manual copy-back.

### 7. `MQL5/Include/TradeCopier/SnapshotFile.mqh` (delete)

No longer needed.

## GUI Layout Sketch

Panel size: ~350 x 400 pixels, anchored top-left of the chart.

### General Tab

```
+----------------------------------+
|  X1 Copy MT5  | [General] [Symbols] [Trades]
+----------------------------------+
| Mode: SLAVE / MASTER             |
| Status: CONNECTED / SEARCHING    |
| Latency: 12 ms                   |
| Master: 192.168.1.42:23456       |
| [Reconnect] [Copy mapping string]|
+----------------------------------+
```

### Symbols Tab

```
+----------------------------------+
| Master Symbol | Slave Symbol | [+]
+----------------------------------+
| US30          | WS30         | [x] |
| XAUUSD        | GOLD         | [x] |
+----------------------------------+
```

### Trades Tab

```
+----------------------------------+
| Slave Ticket | Symbol | Vol | SL | TP |
+----------------------------------+
| 1234567      | WS30   | 0.5 | ...| ...|
+----------------------------------+
```

All UI elements are standard MQL5 chart objects (`OBJ_LABEL`, `OBJ_EDIT`, `OBJ_BUTTON`, `OBJ_RECTANGLE_LABEL`) updated on timer.

## Startup / Discovery Flow

### Master

1. `OnInit`:
   - Start TCP server on an ephemeral port.
   - Start broadcasting `MT5COPIER:<port>` on UDP `DiscoveryUdpPort`.
2. `OnTimer`:
   - Accept any pending TCP client connections.
   - Send a discovery broadcast.
   - Scan positions and push events to all connected clients.

### Slave

1. `OnInit`:
   - Start UDP listener on `DiscoveryUdpPort`.
   - Try to discover a master within a short timeout.
2. `OnTimer`:
   - If not connected, try discovery again.
   - If connected, receive and process events.
   - Update GUI status/latency.

## Error Handling

- **No master found:** slave shows `SEARCHING…` and retries every second.
- **TCP disconnect:** slave marks `DISCONNECTED`, returns to discovery.
- **Duplicate master broadcasts:** slave uses the first discovered master; if multiple masters are detected, log a warning and use the most recently heard one.
- **Firewall blocks UDP broadcast:** slave falls back to `127.0.0.1:<DiscoveryUdpPort>` for localhost scenarios.

## Restart Recovery

- Slave rebuilds `m_records` from open positions on startup to avoid duplicates, identical to the existing logic.
- On first connection, the slave sends a `SYNC_REQUEST` to the master. The master replies with `SYNC_RESPONSE` events for every open position. The slave treats the sync burst as a baseline: existing positions are recorded but do not generate `NEW_TRADE` events.

## Testing Checklist

- [ ] Two terminals on the same machine: master and slave connect automatically.
- [ ] Two terminals on two PCs in the same LAN: slave finds master via UDP broadcast.
- [ ] Disconnect and reconnect master: slave recovers and syncs.
- [ ] Open/modify/partial close/close trades mirror correctly.
- [ ] Symbol mapping table in GUI updates `SymbolMap` string correctly.
- [ ] Latency display shows plausible round-trip time.
- [ ] No duplicate trades after EA restart.

## Trade-offs

| Aspect | File-based (old) | LAN TCP + UDP (new) |
|--------|------------------|---------------------|
| External dependency | None | None |
| Same-machine | Yes | Yes |
| Same-LAN | No | Yes |
| Zero config | Path only | Yes (no IP/port config needed) |
| Latency | Poll ~250 ms | TCP push, usually < 50 ms LAN |
| Complexity | Simple | Moderate (sockets + GUI) |
| Firewall sensitivity | None | UDP broadcast + TCP port |

## Future Considerations

- Optional password / shared secret per master to avoid accidental cross-copying when multiple masters exist on the LAN.
- mDNS/Bonjour discovery as an alternative to UDP broadcast for networks that block broadcast.
- Cross-Internet support would require NAT traversal (out of scope).

## Next Step

Write implementation plan after user approves this design.
