//+------------------------------------------------------------------+
//|                                       MasterPublisher.mqh        |
//+------------------------------------------------------------------+
#ifndef MASTER_PUBLISHER_MQH
#define MASTER_PUBLISHER_MQH

#include <zmq\zmq.mqh>
#include <Arrays\ArrayLong.mqh>
#include <Trade\PositionInfo.mqh>
#include "CopierConfig.mqh"
#include "TradeMessage.mqh"

class CMasterPublisher
{
private:
   Context     *m_context;
   Publisher   *m_socket;
   int         m_heartbeatSeconds;
   datetime    m_lastHeartbeat;
   CArrayLong  m_lastTickets;
   int         m_lastTotal;

   STradeEvent BuildEvent(const string eventName, const PositionInfo &pos);
   void        Send(const STradeEvent &e);
   bool        HasTicket(ulong ticket);
   void        UpdateTicketList();

public:
   CMasterPublisher() : m_context(NULL), m_socket(NULL), m_heartbeatSeconds(0), m_lastHeartbeat(0), m_lastTotal(-1) {}
   bool Init(int port, int heartbeatSeconds);
   void Deinit();
   void PublishChanges(int intervalMs);
};

bool CMasterPublisher::Init(int port, int heartbeatSeconds)
{
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_lastTotal = -1;
   m_lastTickets.Clear();

   string address = StringFormat("tcp://127.0.0.1:%d", port);
   m_context = new Context();
   m_socket = new Publisher(m_context);
   if(!m_socket.bind(address))
   {
      PrintFormat("MasterPublisher: failed to bind to %s", address);
      return false;
   }

   PrintFormat("MasterPublisher: bound to %s", address);
   return true;
}

void CMasterPublisher::Deinit()
{
   if(CheckPointer(m_socket) != POINTER_INVALID)
   {
      delete m_socket;
      m_socket = NULL;
   }
   if(CheckPointer(m_context) != POINTER_INVALID)
   {
      delete m_context;
      m_context = NULL;
   }
}

void CMasterPublisher::PublishChanges(int intervalMs)
{
   static ulong lastPublish = 0;
   ulong now = GetTickCount();
   if(now - lastPublish < (uint)intervalMs)
      return;
   lastPublish = now;

   int total = PositionsTotal();

   // --- detect new / modified / closed positions ---
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      PositionInfo pos;
      if(!pos.SelectByIndex(i))
         continue;

      if(!HasTicket(ticket))
      {
         // brand new trade
         Send(BuildEvent("NEW_TRADE", pos));
      }
      else
      {
         // modifications: in v1 we simply re-publish MODIFY on every scan after first publish.
         // A simpler robust approach: emit MODIFY every scan for known tickets; slave ignores if unchanged.
         Send(BuildEvent("MODIFY_TRADE", pos));
      }
   }

   // detect full closes / partial closes by comparing old ticket list
   for(int i = m_lastTickets.Total() - 1; i >= 0; i--)
   {
      ulong oldTicket = m_lastTickets[i];
      bool stillOpen = false;
      for(int j = 0; j < total; j++)
      {
         if(PositionGetTicket(j) == oldTicket)
         {
            stillOpen = true;
            break;
         }
      }
      if(!stillOpen)
      {
         STradeEvent e;
         e.event = "CLOSE_TRADE";
         e.timestamp = TimeLocal();
         e.magic = MAGIC_BASE + (int)(oldTicket % 900000);
         e.master_ticket = oldTicket;
         Send(e);
      }
   }

   UpdateTicketList();

   // heartbeat
   datetime nowTime = TimeLocal();
   if(nowTime - m_lastHeartbeat >= m_heartbeatSeconds)
   {
      STradeEvent hb;
      hb.event = "HEARTBEAT";
      hb.timestamp = nowTime;
      Send(hb);
      m_lastHeartbeat = nowTime;
   }
}

STradeEvent CMasterPublisher::BuildEvent(const string eventName, const PositionInfo &pos)
{
   STradeEvent e;
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

void CMasterPublisher::Send(const STradeEvent &e)
{
   string msg = CTradeMessage::EventToJson(e);
   ZmqMsg zmsg(msg);
   m_socket.send(zmsg);
}

bool CMasterPublisher::HasTicket(ulong ticket)
{
   for(int i = 0; i < m_lastTickets.Total(); i++)
      if(m_lastTickets[i] == (long)ticket)
         return true;
   return false;
}

void CMasterPublisher::UpdateTicketList()
{
   m_lastTickets.Clear();
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket != 0)
         m_lastTickets.Add((long)ticket);
   }
}

#endif
