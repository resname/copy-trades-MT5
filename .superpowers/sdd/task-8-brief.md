# Task 8: Slave subscriber and order executor

**Files:**
- Create: `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`

**Interfaces:**
- Consumes: `CopierConfig.mqh`, `SymbolMapper.mqh`, `LotSizer.mqh`, `PriceNormalizer.mqh`, `TradeMessage.mqh`
- Produces: `class CSlaveSubscriber` with `bool Init(int port, const string symbolMap, int maxAgeMinutes, int retryCount, int retryDelayMs)`, `void Deinit()`, `void Poll()`.
- Internally maintains map `magic -> {slave_ticket, master_open_volume, slave_open_volume}`.

## Required behavior

- Connects to `tcp://127.0.0.1:<port>` using a ZeroMQ `Socket` of type `ZMQ_SUB`.
- Subscribes to all messages (`setSubscribe("")`).
- Receives JSON events and dispatches:
  - `NEW_TRADE` → open a corresponding slave position.
  - `MODIFY_TRADE` → update SL/TP on the matched slave position.
  - `PARTIAL_CLOSE` → close a proportional fraction of the slave position so the remaining slave volume matches the same fraction of the original master volume carried in the event.
  - `CLOSE_TRADE` → fully close the matched slave position.
  - `HEARTBEAT` → ignored (no action).
- `NEW_TRADE` ignores trades older than `MaxTradeAgeMinutes`.
- Each copied trade gets magic `e.magic` and comment `CPY#<master_ticket>`.

## Partial-close logic

The master now sends `PARTIAL_CLOSE` with `volume` equal to the **current remaining master volume** after the partial close. The slave must keep the slave position proportionally sized:

```cpp
fraction_remaining = e.volume / stored_master_open_volume;
target_slave_volume = stored_slave_open_volume * fraction_remaining;
volume_to_close = current_slave_volume - target_slave_volume;
```

Round `volume_to_close` down to the slave symbol's lot step and call `PositionClosePartial`.

## Open-trade logic

Use the lot sizer with current account balance. Determine slave symbol via mapper (fallback to same name). Compute SL/TP via `CPriceNormalizer::NormalizeSLTP` if `NormalizeSLTPUsingPoints` is true, then round to the symbol's `SYMBOL_TRADE_TICK_SIZE`. Use the current ask for buy orders and current bid for sell orders as the open price passed to `CTrade::PositionOpen`.

## Modify-trade logic

Select the slave position by ticket, then normalize and round SL/TP as in open logic and call `CTrade::PositionModify`.

## Close-trade logic

Call `CTrade::PositionClose(slaveTicket)`.

## Implementation sketch

```cpp
//+------------------------------------------------------------------+
//|                                       SlaveSubscriber.mqh        |
//+------------------------------------------------------------------+
#ifndef SLAVE_SUBSCRIBER_MQH
#define SLAVE_SUBSCRIBER_MQH

#include <Zmq\Zmq.mqh>
#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\SymbolInfo.mqh>
#include <Arrays\ArrayLong.mqh>
#include <Arrays\ArrayDouble.mqh>
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
   Context            m_context;
   Socket            *m_socket;
   CTrade             m_trade;
   CSymbolMapper      m_mapper;
   CLotSizer          m_lotSizer;
   CSymbolInfo        m_symbolInfo;
   int                m_maxAgeMinutes;
   int                m_retryCount;
   int                m_retryDelayMs;
   ulong              m_lastPoll;
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

public:
   bool Init(int port, const string symbolMap, int maxAgeMinutes,
             int retryCount, int retryDelayMs);
   void Deinit();
   void Poll();
};

bool CSlaveSubscriber::Init(int port, const string symbolMap,
                            int maxAgeMinutes, int retryCount, int retryDelayMs)
{
   m_maxAgeMinutes = maxAgeMinutes;
   m_retryCount = retryCount;
   m_retryDelayMs = retryDelayMs;
   m_lastPoll = 0;
   ArrayResize(m_records, 0);

   m_mapper.Init(symbolMap);

   string address = StringFormat("tcp://127.0.0.1:%d", port);
   m_context = new Context();
   if(CheckPointer(m_context) == POINTER_INVALID)
   {
      Print("SlaveSubscriber: failed to create ZMQ context");
      return false;
   }

   m_socket = new Socket(m_context, ZMQ_SUB);
   if(CheckPointer(m_socket) == POINTER_INVALID)
   {
      Print("SlaveSubscriber: failed to create ZMQ socket");
      delete m_context;
      return false;
   }

   if(!m_socket.connect(address))
   {
      PrintFormat("SlaveSubscriber: failed to connect to %s", address);
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
   PrintFormat("SlaveSubscriber: connected to %s", address);
   return true;
}

void CSlaveSubscriber::Deinit()
{
   if(CheckPointer(m_socket) != POINTER_INVALID)
      delete m_socket;
   if(CheckPointer(m_context) != POINTER_INVALID)
      delete m_context;
   m_socket = NULL;
   m_context = NULL;
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
      ProcessEvent(e);
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

   RoundToTickSize(slaveSymbol, slaveSL);
   RoundToTickSize(slaveSymbol, slaveTP);

   ENUM_ORDER_TYPE orderType = (e.side == POSITION_TYPE_BUY) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double openPrice = (orderType == ORDER_TYPE_BUY) ? m_symbolInfo.Ask() : m_symbolInfo.Bid();

   ulong ticket = 0;
   if(!OpenSlaveOrder(slaveSymbol, orderType, lots, slaveSL, slaveTP,
                      e.magic, StringFormat("CPY#%I64u", e.master_ticket), ticket))
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

   RoundToTickSize(slaveSymbol, slaveSL);
   RoundToTickSize(slaveSymbol, slaveTP);

   for(int attempt = 0; attempt <= m_retryCount; attempt++)
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
   targetSlaveVolume = MathFloor(targetSlaveVolume / lotStep) * lotStep;
   double volumeToClose = currentSlaveVolume - targetSlaveVolume;
   volumeToClose = MathFloor(volumeToClose / lotStep) * lotStep;

   if(volumeToClose <= 0.0)
      return;

   for(int attempt = 0; attempt <= m_retryCount; attempt++)
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
   for(int attempt = 0; attempt <= m_retryCount; attempt++)
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
   datetime now = TimeLocal();
   return (now - openTime) > m_maxAgeMinutes * 60;
}

bool CSlaveSubscriber::OpenSlaveOrder(const string symbol, ENUM_ORDER_TYPE type,
                                      double lots, double sl, double tp,
                                      long magic, string comment, ulong &outTicket)
{
   m_trade.SetExpertMagicNumber((int)magic);
   m_trade.SetDeviationInPoints(10);

   for(int attempt = 0; attempt <= m_retryCount; attempt++)
   {
      if(!m_symbolInfo.Name(symbol) || !m_symbolInfo.Select(symbol))
      {
         Sleep(m_retryDelayMs);
         continue;
      }
      double price = (type == ORDER_TYPE_BUY) ? m_symbolInfo.Ask() : m_symbolInfo.Bid();
      if(m_trade.PositionOpen(symbol, type, lots, price, sl, tp, comment))
      {
         outTicket = m_trade.ResultDeal(); // or ResultOrder? ResultDeal gives the deal ticket; for matching we need position ticket.
         // Use ResultOrder for the order ticket, then derive position ticket via PositionSelectByTicket if needed.
         outTicket = m_trade.ResultOrder();
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
   price = NormalizeDouble(MathRound(price / tickSize) * tickSize, 8);
   return true;
}

#endif
```

## Important notes

- `CTrade::PositionOpen` returns `true`/`false`. After a successful open, `m_trade.ResultOrder()` returns the order ticket. The position ticket is usually the same as the order ticket for market orders in hedging mode, but use `PositionSelectByTicket` to confirm.
- For simplicity, store the returned order ticket as the slave position ticket.
- `ArrayRemove` is a built-in MQL5 function to remove an element from a dynamic array.
- Commit the file after verifying syntax as much as possible.

- [ ] **Step 1: Create `SlaveSubscriber.mqh` with the corrected logic.**
- [ ] **Step 2: Compile-check using a temporary stub EA if possible.**
- [ ] **Step 3: Commit.**

```bash
git add MQL5/Include/TradeCopier/SlaveSubscriber.mqh
git commit -m "feat: add slave subscriber and order executor

Co-Authored-By: Claude <noreply@anthropic.com>"
```
