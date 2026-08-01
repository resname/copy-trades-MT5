# Task 5: SL/TP point-distance normalizer

**Files:**
- Create: `MQL5/Include/TradeCopier/PriceNormalizer.mqh`

**Interfaces:**
- Produces: `class CPriceNormalizer` with `void NormalizeSLTP(double masterOpen, double masterSL, double masterTP, double masterPoint, double slaveOpen, double slavePoint, ENUM_POSITION_TYPE side, double &outSL, double &outTP)`.
- `outSL` / `outTP` are `0.0` if no master SL/TP.

- [ ] **Step 1: Create the normalizer**

```cpp
//+------------------------------------------------------------------+
//|                                      PriceNormalizer.mqh         |
//+------------------------------------------------------------------+
#ifndef PRICE_NORMALIZER_MQH
#define PRICE_NORMALIZER_MQH

class CPriceNormalizer
{
public:
   static void NormalizeSLTP(double masterOpen, double masterSL, double masterTP,
                             double masterPoint,
                             double slaveOpen, double slavePoint,
                             ENUM_POSITION_TYPE side,
                             double &outSL, double &outTP);
};

void CPriceNormalizer::NormalizeSLTP(double masterOpen, double masterSL, double masterTP,
                                     double masterPoint,
                                     double slaveOpen, double slavePoint,
                                     ENUM_POSITION_TYPE side,
                                     double &outSL, double &outTP)
{
   outSL = 0.0;
   outTP = 0.0;

   if(masterPoint <= 0.0 || slavePoint <= 0.0)
   {
      Print("PriceNormalizer: invalid point size");
      return;
   }

   if(side == POSITION_TYPE_BUY)
   {
      if(masterSL > 0.0)
      {
         double slDistPoints = (masterOpen - masterSL) / masterPoint;
         outSL = slaveOpen - (slDistPoints * slavePoint);
      }
      if(masterTP > 0.0)
      {
         double tpDistPoints = (masterTP - masterOpen) / masterPoint;
         outTP = slaveOpen + (tpDistPoints * slavePoint);
      }
   }
   else // POSITION_TYPE_SELL
   {
      if(masterSL > 0.0)
      {
         double slDistPoints = (masterSL - masterOpen) / masterPoint;
         outSL = slaveOpen + (slDistPoints * slavePoint);
      }
      if(masterTP > 0.0)
      {
         double tpDistPoints = (masterOpen - masterTP) / masterPoint;
         outTP = slaveOpen - (tpDistPoints * slavePoint);
      }
   }
}

#endif
```

- [ ] **Step 2: Commit**

```bash
git add MQL5/Include/TradeCopier/PriceNormalizer.mqh
git commit -m "feat: add SL/TP point-distance normalizer

Co-Authored-By: Claude <noreply@anthropic.com>"
```
