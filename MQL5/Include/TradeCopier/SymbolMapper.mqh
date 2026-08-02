//+------------------------------------------------------------------+
//|                                         SymbolMapper.mqh         |
//+------------------------------------------------------------------+
#ifndef SYMBOL_MAPPER_MQH
#define SYMBOL_MAPPER_MQH

#include <Arrays\ArrayString.mqh>
#include <Trade\SymbolInfo.mqh>

class CSymbolMapper
{
private:
   CArrayString m_masterSymbols;
   CArrayString m_slaveSymbols;
   CSymbolInfo  m_symbolInfo;

public:
   void Init(const string symbolMapCsv);
   string Resolve(const string masterSymbol);
   bool ExistsOnSlave(const string symbol);
};

void CSymbolMapper::Init(const string symbolMapCsv)
{
   m_masterSymbols.Clear();
   m_slaveSymbols.Clear();

   if(symbolMapCsv == "")
      return;

   string pairs[];
   int pairCount = StringSplit(symbolMapCsv, ',', pairs);
   for(int i = 0; i < pairCount; i++)
   {
      string pair = pairs[i];
      StringReplace(pair, " ", ""); // remove spaces
      if(pair == "")
         continue;

      string sides[];
      int sideCount = StringSplit(pair, '=', sides);
      if(sideCount != 2)
      {
         PrintFormat("SymbolMapper: invalid pair '%s'", pair);
         continue;
      }

      m_masterSymbols.Add(sides[0]);
      m_slaveSymbols.Add(sides[1]);
   }
}

string CSymbolMapper::Resolve(const string masterSymbol)
{
   // 1. explicit mapping
   for(int i = 0; i < m_masterSymbols.Total(); i++)
   {
      if(m_masterSymbols[i] == masterSymbol)
         return m_slaveSymbols[i];
   }

   // 2. fallback to same name
   if(ExistsOnSlave(masterSymbol))
      return masterSymbol;

   // 3. not found
   return "";
}

bool CSymbolMapper::ExistsOnSlave(const string symbol)
{
   return m_symbolInfo.Name(symbol) && m_symbolInfo.Select();
}

#endif
