//+------------------------------------------------------------------+
//|                                       SlaveSubscriber.mqh        |
//+------------------------------------------------------------------+
#ifndef SLAVE_SUBSCRIBER_MQH
#define SLAVE_SUBSCRIBER_MQH

#include "SnapshotFile.mqh"
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
   string            m_sharedPath;
   CTrade             m_trade;
   CSymbolMapper      m_mapper;
   CLotSizer          m_lotSizer;
   CSymbolInfo        m_symbolInfo;
   int                m_maxAgeMinutes;
   int                m_retryCount;
   int                m_retryDelayMs;
   int                m_heartbeatSeconds;
   datetime           m_lastHeartbeat;
   long               m_lastHeartbeatValue;
   bool               m_heartbeatWarned;
   bool               m_baselineSet;
   STradeSnapshot     m_prevSnapshot;
   SSlaveCopyRecord   m_records[];

   void   EstablishBaseline(const STradeSnapshot &snapshot);
   void   DiffAndProcess(const STradeSnapshot &snapshot);
   STradeEvent BuildEventFromSnapshot(const string eventName, const SPositionSnapshot &pos);
   void   CheckHeartbeat(long snapshotHeartbeat);
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

public:
   CSlaveSubscriber() : m_maxAgeMinutes(0), m_retryCount(0), m_retryDelayMs(0),
                        m_heartbeatSeconds(0), m_lastHeartbeat(0), m_lastHeartbeatValue(0),
                        m_heartbeatWarned(false), m_baselineSet(false)
   {
      m_sharedPath = "";
   }
   bool Init(const string sharedPath, const string symbolMap,
             int maxAgeMinutes, int retryCount, int retryDelayMs,
             int heartbeatSeconds);
   void Deinit();
   void Poll();
};

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
   m_lastHeartbeatValue = 0;
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

void CSlaveSubscriber::Deinit()
{
   ArrayResize(m_records, 0);
   ArrayResize(m_prevSnapshot.positions, 0);
}

void CSlaveSubscriber::Poll()
{
   STradeSnapshot snapshot;
   if(!CSnapshotFile::Read(m_sharedPath, snapshot))
   {
      CheckHeartbeat(0);
      return;
   }

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
   if(snapshotHeartbeat > 0 && snapshotHeartbeat != m_lastHeartbeatValue)
   {
      m_lastHeartbeatValue = snapshotHeartbeat;
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

int CSlaveSubscriber::FindSnapshotIndex(ulong ticket) const
{
   return FindSnapshotIndex(m_prevSnapshot.positions, ticket);
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

   if(!m_symbolInfo.Select(slaveSymbol))
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
   if(NormalizeSLTPByPriceDistance)
   {
      double slaveAsk = m_symbolInfo.Ask();
      double slaveBid = m_symbolInfo.Bid();
      double slaveOpen = (e.side == POSITION_TYPE_BUY) ? slaveAsk : slaveBid;

      // Preserve raw price distance. This handles the common case where master
      // and slave quote the same instrument at different decimal precisions
      // (e.g. US30 at 52444.31 and WS30 at 52444).
      CPriceNormalizer::NormalizeSLTP(
         e.open_price, e.sl, e.tp,
         slaveOpen,
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
   {
      PrintFormat("Slave: MODIFY_TRADE for unknown magic %I64d (master ticket %I64u), ignoring", e.magic, e.master_ticket);
      return;
   }

   ulong slaveTicket = m_records[idx].slave_ticket;
   if(!PositionSelectByTicket(slaveTicket))
   {
      PrintFormat("Slave: cannot select position #%I64u for modify", slaveTicket);
      return;
   }

   string slaveSymbol = PositionGetString(POSITION_SYMBOL);
   if(!m_symbolInfo.Select(slaveSymbol))
   {
      PrintFormat("Slave: cannot select symbol %s for modify", slaveSymbol);
      return;
   }

   double slaveOpen = PositionGetDouble(POSITION_PRICE_OPEN);
   double slaveSL = 0.0, slaveTP = 0.0;

   if(NormalizeSLTPByPriceDistance)
   {
      // Preserve raw price distance on modify as well as open.
      CPriceNormalizer::NormalizeSLTP(
         e.open_price, e.sl, e.tp,
         slaveOpen,
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
   {
      PrintFormat("Slave: PARTIAL_CLOSE for unknown magic %I64d (master ticket %I64u), ignoring", e.magic, e.master_ticket);
      return;
   }

   ulong slaveTicket = m_records[idx].slave_ticket;
   if(!PositionSelectByTicket(slaveTicket))
   {
      PrintFormat("Slave: cannot select position #%I64u for partial close", slaveTicket);
      return;
   }

   string slaveSymbol = PositionGetString(POSITION_SYMBOL);
   if(!m_symbolInfo.Select(slaveSymbol))
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
   {
      PrintFormat("Slave: CLOSE_TRADE for unknown magic %I64d (master ticket %I64u), ignoring", e.magic, e.master_ticket);
      return;
   }

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
   if(!m_symbolInfo.Select(symbol))
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
