# Task 4: Lot sizing helper

**Files:**
- Create: `MQL5/Include/TradeCopier/LotSizer.mqh`

**Interfaces:**
- Produces: `class CLotSizer` with `double CalculateLots(double balance, double stepAmount, double stepSize, double maxLot, string symbol)`.
- Rounds DOWN to `SYMBOL_VOLUME_STEP`, clamps to min/max symbol volume, caps at `maxLot`.

- [ ] **Step 1: Create the lot sizer**

```cpp
//+------------------------------------------------------------------+
//|                                             LotSizer.mqh         |
//+------------------------------------------------------------------+
#ifndef LOT_SIZER_MQH
#define LOT_SIZER_MQH

#include <Trade\SymbolInfo.mqh>
#include <Math\Math.mqh>

class CLotSizer
{
private:
   CSymbolInfo m_symbolInfo;

public:
   double CalculateLots(double balance, double stepAmount, double stepSize,
                        double maxLot, const string symbol);
};

double CLotSizer::CalculateLots(double balance, double stepAmount, double stepSize,
                                double maxLot, const string symbol)
{
   if(stepAmount <= 0.0 || stepSize <= 0.0)
   {
      Print("LotSizer: invalid step amount or step size");
      return 0.0;
   }

   if(!m_symbolInfo.Name(symbol) || !m_symbolInfo.Select(symbol))
   {
      PrintFormat("LotSizer: cannot select symbol %s", symbol);
      return 0.0;
   }

   double steps = MathFloor(balance / stepAmount);
   double lots  = steps * stepSize;

   double lotStep = m_symbolInfo.LotsStep();
   double minLot  = m_symbolInfo.LotsMin();
   double maxLotSymbol = m_symbolInfo.LotsMax();

   // round DOWN to lot step
   lots = MathFloor(lots / lotStep) * lotStep;

   // clamp and cap
   lots = MathMax(lots, minLot);
   lots = MathMin(lots, maxLotSymbol);
   lots = MathMin(lots, maxLot);

   return NormalizeDouble(lots, 2);
}

#endif
```

- [ ] **Step 2: Compile-check**

Include in stub EA and F7 in MetaEditor. Expected: clean compile.

- [ ] **Step 3: Commit**

```bash
git add MQL5/Include/TradeCopier/LotSizer.mqh
git commit -m "feat: add balance-step lot sizer

Co-Authored-By: Claude <noreply@anthropic.com>"
```
