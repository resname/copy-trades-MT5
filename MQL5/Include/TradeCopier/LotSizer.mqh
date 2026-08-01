//+------------------------------------------------------------------+
//|                                             LotSizer.mqh         |
//+------------------------------------------------------------------+
#ifndef LOT_SIZER_MQH
#define LOT_SIZER_MQH

#include <Trade\SymbolInfo.mqh>

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

   double lotStep = m_symbolInfo.LotsStep();
   double minLot  = m_symbolInfo.LotsMin();
   double maxLotSymbol = m_symbolInfo.LotsMax();

   if(lotStep <= 0.0)
   {
      Print("LotSizer: invalid lot step (must be > 0)");
      return 0.0;
   }

   double steps = MathFloor(balance / stepAmount);
   double lots  = steps * stepSize;

   // round DOWN to lot step
   lots = MathFloor(lots / lotStep) * lotStep;

   // clamp and cap
   lots = MathMax(lots, minLot);
   lots = MathMin(lots, maxLotSymbol);
   lots = MathMin(lots, maxLot);

   // remove floating-point noise while staying on the lot-step grid
   int lotDigits = 0;
   if(lotStep > 0.0)
      lotDigits = (int)MathMax(0.0, -MathLog10(lotStep));
   lots = NormalizeDouble(lots, lotDigits);

   return lots;
}

#endif
