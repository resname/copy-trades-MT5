#ifndef LAN_TRANSPORT_MQH
#define LAN_TRANSPORT_MQH

#include "TradeMessage.mqh"

const string MASTER_BROADCAST_PREFIX = "MT5COPIER:";
const uint   DEFAULT_DISCOVERY_PORT  = 55555;
const int    MAX_CLIENTS = 8;

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

   bool   InternalSend(int socket, const string &json);
   bool   InternalReceive(int socket, string &outJson, uint timeoutMs);
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
   bool ReceiveFromClient(string &outJson, uint timeoutMs);

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
   int msgLen = StringLen(msg);
   uchar data[];
   ArrayResize(data, msgLen);
   StringToCharArray(msg, data);
   return SocketSend(m_udpBroadcast, "255.255.255.255", discoveryUdpPort, data, msgLen) > 0;
}

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
      uchar buf[256];
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

bool CLanTransport::InternalSend(int socket, const string &json)
{
   if(socket == INVALID_HANDLE) return false;

   uchar payload[];
   StringToCharArray(json, payload);
   int payloadLen = ArraySize(payload) - 1; // exclude null terminator
   if(payloadLen < 0) payloadLen = 0;

   int frameLen = 4 + payloadLen;
   uchar frame[];
   ArrayResize(frame, frameLen);
   frame[0] = (uchar)(payloadLen & 0xFF);
   frame[1] = (uchar)((payloadLen >> 8) & 0xFF);
   frame[2] = (uchar)((payloadLen >> 16) & 0xFF);
   frame[3] = (uchar)((payloadLen >> 24) & 0xFF);
   ArrayCopy(frame, payload, 4, 0, payloadLen);

   int totalSent = 0;
   uint start = GetTickCount();
   while(totalSent < frameLen)
   {
      if(GetTickCount() - start > 5000)
         return false;

      int remaining = frameLen - totalSent;
      uchar tail[];
      ArrayResize(tail, remaining);
      ArrayCopy(tail, frame, 0, totalSent, remaining);

      int sent = SocketSend(socket, tail, remaining);
      if(sent < 0)
         return false;
      if(sent == 0)
      {
         Sleep(1);
         continue;
      }
      totalSent += sent;
   }
   return true;
}

bool CLanTransport::InternalReceive(int socket, string &outJson, uint timeoutMs)
{
   outJson = "";
   if(socket == INVALID_HANDLE) return false;

   uint start = GetTickCount();

   // Read 4-byte length
   uchar header[4];
   int headerRead = 0;
   while(headerRead < 4)
   {
      if(GetTickCount() - start > timeoutMs)
         return false;

      uchar tmp[1];
      int r = SocketRead(socket, tmp, 1, 100);
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

   uchar payload[];
   ArrayResize(payload, len);
   int payloadRead = 0;
   while(payloadRead < len)
   {
      if(GetTickCount() - start > timeoutMs)
         return false;

      int remaining = len - payloadRead;
      uchar tmp[];
      ArrayResize(tmp, remaining);
      int r = SocketRead(socket, tmp, remaining, 100);
      if(r > 0)
      {
         ArrayCopy(payload, tmp, payloadRead, 0, r);
         payloadRead += r;
      }
      else if(r < 0)
         return false;
   }

   outJson = CharArrayToString(payload, 0, len);
   return true;
}

bool CLanTransport::ReceiveFrame(string &outJson, uint timeoutMs)
{
   outJson = "";
   if(m_tcpClient == INVALID_HANDLE) return false;

   uint start = GetTickCount();
   bool ok = InternalReceive(m_tcpClient, outJson, timeoutMs);
   if(ok && m_latencyMs < 0)
      m_latencyMs = (int)(GetTickCount() - start);
   return ok;
}

bool CLanTransport::ReceiveFromClient(string &outJson, uint timeoutMs)
{
   outJson = "";
   if(m_tcpServer == INVALID_HANDLE) return false;

   int n = ArraySize(m_clients);
   for(int i = 0; i < n; i++)
   {
      if(m_clients[i] == INVALID_HANDLE) continue;
      if(InternalReceive(m_clients[i], outJson, timeoutMs))
         return true;
      // Receive failed: close and clear this client slot.
      SocketClose(m_clients[i]);
      m_clients[i] = INVALID_HANDLE;
   }
   return false;
}

bool CLanTransport::SendFrame(const string &json)
{
   if(m_tcpClient == INVALID_HANDLE) return false;
   return InternalSend(m_tcpClient, json);
}

#endif // LAN_TRANSPORT_MQH
