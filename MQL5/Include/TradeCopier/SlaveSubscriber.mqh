//+------------------------------------------------------------------+
//|                                       SlaveSubscriber.mqh        |
//+------------------------------------------------------------------+
#ifndef SLAVE_SUBSCRIBER_MQH
#define SLAVE_SUBSCRIBER_MQH

#include <Zmq\Zmq.mqh>
#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
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
   Context           *m_context;
   Socket            *m_socket;
   Socket            *m_syncPush;
   CTrade             m_trade;
   CSymbolMapper      m_mapper;
   CLotSizer          m_lotSizer;
   CSymbolInfo        m_symbolInfo;
   int                m_maxAgeMinutes;
   int                m_retryCount;
   int                m_retryDelayMs;
   int                m_heartbeatSeconds;
   datetime           m_lastHeartbeat;
   bool               m_heartbeatWarned;
   SSlaveCopyRecord   m_records[];

   void   ProcessEvent(const STradeEvent &e);
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
   bool   SendSyncRequest();

public:
   CSlaveSubscriber() : m_context(NULL), m_socket(NULL), m_syncPush(NULL),
                        m_maxAgeMinutes(0), m_retryCount(0), m_retryDelayMs(0),
                        m_heartbeatSeconds(0), m_lastHeartbeat(0), m_heartbeatWarned(false) {}
   bool Init(int port, const string symbolMap, int maxAgeMinutes,
             int retryCount, int retryDelayMs, int heartbeatSeconds);
   void Deinit();
   void Poll();
};

bool CSlaveSubscriber::Init(int port, const string symbolMap,
                            int maxAgeMinutes, int retryCount, int retryDelayMs,
                            int heartbeatSeconds)
{
   m_maxAgeMinutes = maxAgeMinutes;
   m_retryCount = retryCount;
   m_retryDelayMs = retryDelayMs;
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_heartbeatWarned = false;
   ArrayResize(m_records, 0);

   m_mapper.Init(symbolMap);

   string address = StringFormat("tcp://127.0.0.1:%d", port);
   string syncAddress = StringFormat("tcp://127.0.0.1:%d", port + 1);

   m_context = new Context();
   if(CheckPointer(m_context) == POINTER_INVALID)
   {
      Print("SlaveSubscriber: failed to create ZMQ context");
      return false;
   }

   m_socket = new Socket(m_context, ZMQ_SUB);
   if(CheckPointer(m_socket) == POINTER_INVALID)
   {
      Print("SlaveSubscriber: failed to create ZMQ SUB socket");
      delete m_context;
      m_context = NULL;
      return false;
   }

   if(!m_socket.connect(address))
   {
      PrintFormat("SlaveSubscriber: failed to connect SUB to %s", address);
      delete m_socket;
      delete m_context;
      m_socket = NULL;
      m_context = NULL;
      return false;
   }

   if(!m_socket.setSubscribe(""))
   {
      Print("SlaveSubscriber: subscribe failed");
      delete m_socket;
      delete m_context;
      m_socket = NULL;
      m_context = NULL;
      return false;
   }

   m_socket.setReceiveTimeout(1);
   PrintFormat("SlaveSubscriber: connected SUB to %s", address);

   m_syncPush = new Socket(m_context, ZMQ_PUSH);
   if(CheckPointer(m_syncPush) == POINTER_INVALID)
   {
      Print("SlaveSubscriber: failed to create ZMQ PUSH socket");
      delete m_socket;
      delete m_context;
      m_socket = NULL;
      m_context = NULL;
      return false;
   }

   if(!m_syncPush.connect(syncAddress))
   {
      PrintFormat("SlaveSubscriber: failed to connect PUSH sync to %s", syncAddress);
      delete m_syncPush;
      delete m_socket;
      delete m_context;
      m_syncPush = NULL;
      m_socket = NULL;
      m_context = NULL;
      return false;
   }

   PrintFormat("SlaveSubscriber: connected PUSH sync to %s", syncAddress);

   // Set a short send timeout so starting the slave before the master does not hang init.
   m_syncPush.setSendTimeout(1000);
   if(!SendSyncRequest())
      Print("SlaveSubscriber: sync request could not be delivered; will rely on normal subscription");

   // Start heartbeat timer now that the subscriber is online.
   m_lastHeartbeat = TimeCurrent();
   m_heartbeatWarned = false;

   // Rebuild records for any copied positions already open on the slave account
   // so a restart does not create duplicate trades.
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

      // New format: CPY#<ticket>|MV<master_volume>|SV<slave_volume>
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
         // Legacy plain-ticket format: fall back to the current position volume.
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

bool CSlaveSubscriber::SendSyncRequest()
{
   if(m_syncPush == NULL)
      return false;

   STradeEvent e;
   ZeroMemory(e);
   e.event = "SYNC_REQUEST";
   e.timestamp = TimeLocal();

   string msg = CTradeMessage::EventToJson(e);
   ZmqMsg zmsg(msg);
   return m_syncPush.send(zmsg);
}

void CSlaveSubscriber::Deinit()
{
   if(CheckPointer(m_syncPush) != POINTER_INVALID)
      delete m_syncPush;
   if(CheckPointer(m_socket) != POINTER_INVALID)
      delete m_socket;
   if(CheckPointer(m_context) != POINTER_INVALID)
      delete m_context;
   m_syncPush = NULL;
   m_socket = NULL;
   m_context = NULL;
   ArrayResize(m_records, 0);
}

void CSlaveSubscriber::Poll()
{
   if(m_socket == NULL)
      return;

   ZmqMsg msg;
   while(m_socket.recv(msg, true))
   {
      string data = msg.getData();
      STradeEvent e;
      if(!CTradeMessage::JsonToEvent(data, e))
      {
         PrintFormat("SlaveSubscriber: malformed JSON: %s", data);
         continue;
      }

      // Any valid event from the master counts as a heartbeat.
      m_lastHeartbeat = TimeCurrent();
      m_heartbeatWarned = false;

      ProcessEvent(e);
   }

   if(m_heartbeatSeconds > 0 &&
      m_lastHeartbeat > 0 &&
      TimeCurrent() - m_lastHeartbeat > m_heartbeatSeconds * 2)
   {
      if(!m_heartbeatWarned)
      {
         Print("SlaveSubscriber: no heartbeat from master");
         m_heartbeatWarned = true;
      }
   }
}

void CSlaveSubscriber::ProcessEvent(const STradeEvent &e)
{
   if(e.event == "HEARTBEAT")
      return;
   if(e.event == "NEW_TRADE")
      OpenTrade(e);
   else if(e.event == "MODIFY_TRADE")
      ModifyTrade(e);
   else if(e.event == "PARTIAL_CLOSE")
      PartialClose(e);
   else if(e.event == "CLOSE_TRADE")
      CloseTrade(e);
}

void CSlaveSubscriber::OpenTrade(const STradeEvent &e)
{
   string slaveSymbol = m_mapper.Resolve(e.symbol);
   if(slaveSymbol == "")
   {
      PrintFormat("Slave: no valid slave symbol for master %s", e.symbol);
      return;
   }

   if(IsTooOld(e.open_time))
   {
      PrintFormat("Slave: ignoring old trade #%I64u opened at %s",
                  e.master_ticket, TimeToString(e.open_time));
      return;
   }

   if(FindRecord(e.magic) >= 0)
      return;

   if(!m_symbolInfo.Name(slaveSymbol) || !m_symbolInfo.Select(slaveSymbol))
   {
      PrintFormat("Slave: cannot select symbol %s", slaveSymbol);
      return;
   }

   double lots = m_lotSizer.CalculateLots(
      AccountInfoDouble(ACCOUNT_BALANCE),
      BalanceStepAmount, BalanceStepSize, MaxLotSize, slaveSymbol);
   if(lots <= 0.0)
   {
      PrintFormat("Slave: calculated lot size is zero for %s", slaveSymbol);
      return;
   }

   double slaveSL = 0.0, slaveTP = 0.0;
   if(NormalizeSLTPUsingPoints)
   {
      double slavePoint = m_symbolInfo.Point();
      double slaveAsk = m_symbolInfo.Ask();
      double slaveBid = m_symbolInfo.Bid();
      double slaveOpen = (e.side == POSITION_TYPE_BUY) ? slaveAsk : slaveBid;

      CPriceNormalizer::NormalizeSLTP(
         e.open_price, e.sl, e.tp, e.point,
         slaveOpen, slavePoint,
         (ENUM_POSITION_TYPE)e.side, slaveSL, slaveTP);
   }
   else
   {
      slaveSL = e.sl;
      slaveTP = e.tp;
   }

   if(!RoundToTickSize(slaveSymbol, slaveSL) || !RoundToTickSize(slaveSymbol, slaveTP))
   {
      PrintFormat("Slave: failed to round SL/TP to tick size for %s", slaveSymbol);
      return;
   }

   ENUM_ORDER_TYPE orderType = (e.side == POSITION_TYPE_BUY) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   string comment = StringFormat("CPY#%I64u|MV%.8f|SV%.8f", e.master_ticket, e.volume, lots);

   ulong ticket = 0;
   if(!OpenSlaveOrder(slaveSymbol, orderType, lots, slaveSL, slaveTP,
                      e.magic, comment, ticket))
   {
      PrintFormat("Slave: failed to copy trade #%I64u to %s", e.master_ticket, slaveSymbol);
      return;
   }

   int idx = ArraySize(m_records);
   ArrayResize(m_records, idx + 1);
   m_records[idx].magic = e.magic;
   m_records[idx].slave_ticket = ticket;
   m_records[idx].master_open_volume = e.volume;
   m_records[idx].slave_open_volume = lots;
}

void CSlaveSubscriber::ModifyTrade(const STradeEvent &e)
{
   int idx = FindRecord(e.magic);
   if(idx < 0)
      return;

   ulong slaveTicket = m_records[idx].slave_ticket;
   if(!PositionSelectByTicket(slaveTicket))
   {
      PrintFormat("Slave: cannot select position #%I64u for modify", slaveTicket);
      return;
   }

   string slaveSymbol = PositionGetString(POSITION_SYMBOL);
   if(!m_symbolInfo.Name(slaveSymbol) || !m_symbolInfo.Select(slaveSymbol))
   {
      PrintFormat("Slave: cannot select symbol %s for modify", slaveSymbol);
      return;
   }

   double slavePoint = m_symbolInfo.Point();
   double slaveOpen = PositionGetDouble(POSITION_PRICE_OPEN);
   double slaveSL = 0.0, slaveTP = 0.0;

   if(NormalizeSLTPUsingPoints)
   {
      CPriceNormalizer::NormalizeSLTP(
         e.open_price, e.sl, e.tp, e.point,
         slaveOpen, slavePoint,
         (ENUM_POSITION_TYPE)e.side, slaveSL, slaveTP);
   }
   else
   {
      slaveSL = e.sl;
      slaveTP = e.tp;
   }

   if(!RoundToTickSize(slaveSymbol, slaveSL) || !RoundToTickSize(slaveSymbol, slaveTP))
   {
      PrintFormat("Slave: failed to round SL/TP to tick size for modify on %s", slaveSymbol);
      return;
   }

   for(int attempt = 0; attempt < m_retryCount; attempt++)
   {
      if(m_trade.PositionModify(slaveTicket, slaveSL, slaveTP))
         return;
      Sleep(m_retryDelayMs);
   }
   PrintFormat("Slave: failed to modify position #%I64u", slaveTicket);
}

void CSlaveSubscriber::PartialClose(const STradeEvent &e)
{
   int idx = FindRecord(e.magic);
   if(idx < 0)
      return;

   ulong slaveTicket = m_records[idx].slave_ticket;
   if(!PositionSelectByTicket(slaveTicket))
   {
      PrintFormat("Slave: cannot select position #%I64u for partial close", slaveTicket);
      return;
   }

   string slaveSymbol = PositionGetString(POSITION_SYMBOL);
   if(!m_symbolInfo.Name(slaveSymbol) || !m_symbolInfo.Select(slaveSymbol))
      return;

   double currentSlaveVolume = PositionGetDouble(POSITION_VOLUME);
   double masterOpenVolume = m_records[idx].master_open_volume;
   double slaveOpenVolume = m_records[idx].slave_open_volume;

   if(masterOpenVolume <= 0.0)
      return;

   double fraction = e.volume / masterOpenVolume;
   double targetSlaveVolume = slaveOpenVolume * fraction;
   double lotStep = m_symbolInfo.LotsStep();
   double volumeToClose = currentSlaveVolume - targetSlaveVolume;
   volumeToClose = MathFloor(volumeToClose / lotStep) * lotStep;
   volumeToClose = MathMin(volumeToClose, currentSlaveVolume);

   if(volumeToClose <= 0.0)
      return;

   for(int attempt = 0; attempt < m_retryCount; attempt++)
   {
      if(m_trade.PositionClosePartial(slaveTicket, volumeToClose))
         return;
      Sleep(m_retryDelayMs);
   }
   PrintFormat("Slave: failed partial close for position #%I64u", slaveTicket);
}

void CSlaveSubscriber::CloseTrade(const STradeEvent &e)
{
   int idx = FindRecord(e.magic);
   if(idx < 0)
      return;

   ulong slaveTicket = m_records[idx].slave_ticket;
   for(int attempt = 0; attempt < m_retryCount; attempt++)
   {
      if(m_trade.PositionClose(slaveTicket))
      {
         ArrayRemove(m_records, idx, 1);
         return;
      }
      Sleep(m_retryDelayMs);
   }
   PrintFormat("Slave: failed to close position #%I64u", slaveTicket);
}

int CSlaveSubscriber::FindRecord(long magic)
{
   for(int i = ArraySize(m_records) - 1; i >= 0; i--)
      if(m_records[i].magic == magic)
         return i;
   return -1;
}

bool CSlaveSubscriber::IsTooOld(datetime openTime)
{
   if(openTime == 0)
      return false;
   datetime now = TimeCurrent();
   return (now - openTime) > m_maxAgeMinutes * 60;
}

bool CSlaveSubscriber::OpenSlaveOrder(const string symbol, ENUM_ORDER_TYPE type,
                                      double lots, double sl, double tp,
                                      long magic, string comment, ulong &outTicket)
{
   if(!m_symbolInfo.Name(symbol) || !m_symbolInfo.Select(symbol))
   {
      PrintFormat("Slave: cannot select symbol %s for open", symbol);
      return false;
   }

   m_trade.SetExpertMagicNumber(magic);
   m_trade.SetDeviationInPoints(10);

   for(int attempt = 0; attempt < m_retryCount; attempt++)
   {
      double price = (type == ORDER_TYPE_BUY) ? m_symbolInfo.Ask() : m_symbolInfo.Bid();
      if(m_trade.PositionOpen(symbol, type, lots, price, sl, tp, comment))
      {
         ulong ticket = m_trade.ResultOrder();
         if(ticket == 0)
         {
            PrintFormat("Slave: PositionOpen succeeded for %s but returned ticket 0",
                        symbol);
            return false;
         }
         if(!PositionSelectByTicket(ticket))
         {
            PrintFormat("Slave: PositionOpen returned ticket #%I64u for %s but position does not exist",
                        ticket, symbol);
            return false;
         }
         outTicket = ticket;
         return true;
      }
      PrintFormat("Slave: open attempt %d failed for %s, error %d",
                  attempt + 1, symbol, GetLastError());
      Sleep(m_retryDelayMs);
   }
   return false;
}

bool CSlaveSubscriber::RoundToTickSize(const string symbol, double &price)
{
   if(price <= 0.0)
      return true;
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0.0)
      return false;
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   price = NormalizeDouble(MathRound(price / tickSize) * tickSize, digits);
   return true;
}

#endif
