//+------------------------------------------------------------------+
//|                                       MasterPublisher.mqh        |
//+------------------------------------------------------------------+
#ifndef MASTER_PUBLISHER_MQH
#define MASTER_PUBLISHER_MQH

#include <Zmq\Zmq.mqh>
#include <Trade\PositionInfo.mqh>
#include "CopierConfig.mqh"
#include "TradeMessage.mqh"

struct SPositionSnapshot
{
   ulong  ticket;
   double volume;
   double sl;
   double tp;
};

class CMasterPublisher
{
private:
   Context           *m_context;
   Socket            *m_socket;
   int               m_heartbeatSeconds;
   datetime          m_lastHeartbeat;
   ulong             m_lastPublish;
   SPositionSnapshot m_prevSnapshots[];

   STradeEvent BuildEvent(const string eventName, const PositionInfo &pos);
   void        SendEvent(const string eventName, const PositionInfo &pos, double volume);
   void        Send(const STradeEvent &e);
   int         FindSnapshotIndex(ulong ticket) const;
   int         FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const;
   void        BuildCurrentSnapshots(SPositionSnapshot &out[]);
   void        ReplaceSnapshots(const SPositionSnapshot &src[]);

public:
   CMasterPublisher() : m_context(NULL), m_socket(NULL), m_heartbeatSeconds(0), m_lastHeartbeat(0), m_lastPublish(0) {}
   bool Init(int port, int heartbeatSeconds);
   void Deinit();
   void PublishChanges(int intervalMs);
};

bool CMasterPublisher::Init(int port, int heartbeatSeconds)
{
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_lastPublish = 0;
   ArrayResize(m_prevSnapshots, 0);

   string address = StringFormat("tcp://127.0.0.1:%d", port);

   m_context = new Context();
   if(m_context == NULL)
   {
      Print("MasterPublisher: failed to create ZMQ context");
      return false;
   }

   m_socket = new Socket(m_context, ZMQ_PUB);
   if(m_socket == NULL)
   {
      Print("MasterPublisher: failed to create ZMQ socket");
      delete m_context;
      m_context = NULL;
      return false;
   }

   if(!m_socket.bind(address))
   {
      PrintFormat("MasterPublisher: failed to bind to %s", address);
      delete m_socket;
      delete m_context;
      m_socket = NULL;
      m_context = NULL;
      return false;
   }

   PrintFormat("MasterPublisher: bound to %s", address);
   return true;
}

void CMasterPublisher::Deinit()
{
   if(m_socket != NULL)
   {
      delete m_socket;
      m_socket = NULL;
   }
   if(m_context != NULL)
   {
      delete m_context;
      m_context = NULL;
   }
   ArrayResize(m_prevSnapshots, 0);
}

void CMasterPublisher::PublishChanges(int intervalMs)
{
   ulong now = GetTickCount();
   if(now - m_lastPublish < (uint)intervalMs)
      return;
   m_lastPublish = now;

   SPositionSnapshot curr[];
   BuildCurrentSnapshots(curr);

   // New / modified / partially closed positions.
   for(int i = 0; i < ArraySize(curr); i++)
   {
      PositionInfo pos;
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
         {
            SendEvent("PARTIAL_CLOSE", pos, curr[i].volume);
         }
         else if(NormalizeDouble(prev.sl - curr[i].sl, 8) != 0.0 ||
                 NormalizeDouble(prev.tp - curr[i].tp, 8) != 0.0)
         {
            SendEvent("MODIFY_TRADE", pos, curr[i].volume);
         }
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
         e.timestamp = TimeLocal();
         e.magic = MAGIC_BASE + (int)(oldTicket % 900000);
         e.master_ticket = oldTicket;
         Send(e);
      }
   }

   ReplaceSnapshots(curr);

   // Heartbeat.
   datetime nowTime = TimeLocal();
   if(m_heartbeatSeconds > 0 && nowTime - m_lastHeartbeat >= m_heartbeatSeconds)
   {
      STradeEvent hb;
      ZeroMemory(hb);
      hb.event = "HEARTBEAT";
      hb.timestamp = nowTime;
      Send(hb);
      m_lastHeartbeat = nowTime;
   }
}

STradeEvent CMasterPublisher::BuildEvent(const string eventName, const PositionInfo &pos)
{
   STradeEvent e;
   ZeroMemory(e);
   e.event = eventName;
   e.timestamp = TimeLocal();
   e.master_ticket = pos.Ticket();
   e.magic = MAGIC_BASE + (int)(pos.Ticket() % 900000);
   e.symbol = pos.Symbol();
   e.side = (int)pos.PositionType();
   e.open_price = pos.PriceOpen();
   e.volume = pos.Volume();
   e.sl = pos.StopLoss();
   e.tp = pos.TakeProfit();
   e.open_time = pos.Time();
   e.point = SymbolInfoDouble(pos.Symbol(), SYMBOL_POINT);
   e.comment = pos.Comment();
   return e;
}

void CMasterPublisher::SendEvent(const string eventName, const PositionInfo &pos, double volume)
{
   STradeEvent e = BuildEvent(eventName, pos);
   e.volume = volume;
   Send(e);
}

void CMasterPublisher::Send(const STradeEvent &e)
{
   if(m_socket == NULL)
   {
      Print("MasterPublisher: Send called with no socket");
      return;
   }

   string msg = CTradeMessage::EventToJson(e);
   ZmqMsg zmsg(msg);
   if(!m_socket.send(zmsg))
      PrintFormat("MasterPublisher: failed to send event %s for ticket %I64u", e.event, e.master_ticket);
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
      PositionInfo pos;
      if(!pos.SelectByIndex(i))
         continue;

      ulong ticket = pos.Ticket();
      if(ticket == 0)
         continue;

      if(count >= ArraySize(out))
         ArrayResize(out, count + 1);

      out[count].ticket = ticket;
      out[count].volume = pos.Volume();
      out[count].sl = pos.StopLoss();
      out[count].tp = pos.TakeProfit();
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

#endif
