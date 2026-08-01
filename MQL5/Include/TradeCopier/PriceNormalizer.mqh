//+------------------------------------------------------------------+
//|                                      PriceNormalizer.mqh         |
//+------------------------------------------------------------------+
#ifndef PRICE_NORMALIZER_MQH
#define PRICE_NORMALIZER_MQH

class CPriceNormalizer
{
public:
   static void NormalizeSLTP(double masterOpen, double masterSL, double masterTP,
                             double slaveOpen,
                             ENUM_POSITION_TYPE side,
                             double &outSL, double &outTP);
};

void CPriceNormalizer::NormalizeSLTP(double masterOpen, double masterSL, double masterTP,
                                       double slaveOpen,
                                       ENUM_POSITION_TYPE side,
                                       double &outSL, double &outTP)
{
   outSL = 0.0;
   outTP = 0.0;

   // Preserve raw price distance. This is the correct behavior when the master
   // and slave symbols represent the same underlying instrument but different
   // brokers quote them with different decimal precision. The stop/target
   // distance measured in the master's price units is reproduced exactly on the
   // slave, regardless of each broker's SYMBOL_POINT.
   if(side == POSITION_TYPE_BUY)
   {
      if(masterSL > 0.0)
         outSL = slaveOpen - (masterOpen - masterSL);
      if(masterTP > 0.0)
         outTP = slaveOpen + (masterTP - masterOpen);
   }
   else // POSITION_TYPE_SELL
   {
      if(masterSL > 0.0)
         outSL = slaveOpen + (masterSL - masterOpen);
      if(masterTP > 0.0)
         outTP = slaveOpen - (masterOpen - masterTP);
   }
}

#endif
