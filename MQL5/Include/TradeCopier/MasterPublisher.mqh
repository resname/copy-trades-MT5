//+------------------------------------------------------------------+
//|                                       MasterPublisher.mqh        |
//+------------------------------------------------------------------+
#ifndef MASTER_PUBLISHER_MQH
#define MASTER_PUBLISHER_MQH

#include "LanTransport.mqh"
#include <Trade\PositionInfo.mqh>
#include "CopierConfig.mqh"
#include "TradeMessage.mqh"

class CMasterPublisher
{
private:
   CLanTransport       m_transport;
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

void CMasterPublisher::ProcessSyncRequest()
{
   string json;
   while(m_transport.ReceiveFromClient(json, 0))
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

#endif
