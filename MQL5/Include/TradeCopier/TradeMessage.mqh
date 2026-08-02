//+------------------------------------------------------------------+
//|                                          TradeMessage.mqh        |
//|                        MT5 Local Trade Copier Trade Message      |
//+------------------------------------------------------------------+
#ifndef TRADE_MESSAGE_MQH
#define TRADE_MESSAGE_MQH

//+------------------------------------------------------------------+
//| Trade event structure                                              |
//+------------------------------------------------------------------+
struct STradeEvent
{
   string            event;       // NEW_TRADE, MODIFY_TRADE, PARTIAL_CLOSE, CLOSE_TRADE, HEARTBEAT, SYNC_REQUEST, SYNC_RESPONSE
   long              timestamp;
   long              magic;
   ulong             master_ticket;
   string            symbol;
   int               side;        // POSITION_TYPE_BUY / POSITION_TYPE_SELL
   double            open_price;
   double            volume;
   double            sl;
   double            tp;
   datetime          open_time;
   double            point;
   string            comment;
};

//+------------------------------------------------------------------+
//| Minimal JSON helpers (no external dependency)                      |
//+------------------------------------------------------------------+
bool GetJsonString(const string json, const string key, string &out);
bool GetJsonRawValue(const string json, const string key, string &out);
bool GetJsonLong(const string json, const string key, long &out);
bool GetJsonULong(const string json, const string key, ulong &out);
bool GetJsonInt(const string json, const string key, int &out);
bool GetJsonDouble(const string json, const string key, double &out);

//+------------------------------------------------------------------+
//| Trade message JSON serializer/deserializer                         |
//+------------------------------------------------------------------+
class CTradeMessage
{
public:
   static string EventToJson(const STradeEvent &e);
   static bool   JsonToEvent(const string json, STradeEvent &e);

public:
   static string JsonString(const string value);
};

//+------------------------------------------------------------------+
//| Extract string value for key                                       |
//+------------------------------------------------------------------+
bool GetJsonString(const string json, const string key, string &out)
{
   string pattern = "\"" + key + "\":";
   int pos = StringFind(json, pattern);
   if(pos == -1) return false;
   pos += StringLen(pattern);
   // skip whitespace
   while(pos < StringLen(json) && (json[pos] == ' ' || json[pos] == '\t')) pos++;
   if(pos >= StringLen(json) || json[pos] != '"') return false;
   pos++;

   // Find the closing quote, ignoring escaped quotes.
   int end = -1;
   for(int i = pos; i < StringLen(json); i++)
   {
      if(json[i] != '"') continue;
      int backslashes = 0;
      int j = i - 1;
      while(j >= 0 && json[j] == '\\')
      {
         backslashes++;
         j--;
      }
      if(backslashes % 2 == 0)
      {
         end = i;
         break;
      }
   }
   if(end == -1) return false;

   out = StringSubstr(json, pos, end - pos);
   // Unescape JSON string sequences in reverse order of escaping.
   StringReplace(out, "\\\"", "\"");
   StringReplace(out, "\\\\", "\\");
   return true;
}

//+------------------------------------------------------------------+
//| Extract raw JSON value for key                                     |
//+------------------------------------------------------------------+
bool GetJsonRawValue(const string json, const string key, string &out)
{
   string pattern = "\"" + key + "\":";
   int pos = StringFind(json, pattern);
   if(pos == -1) return false;
   pos += StringLen(pattern);
   while(pos < StringLen(json) && (json[pos] == ' ' || json[pos] == '\t')) pos++;

   int start = pos;
   // read until comma or end brace
   while(pos < StringLen(json) && json[pos] != ',' && json[pos] != '}') pos++;
   out = StringSubstr(json, start, pos - start);
   StringReplace(out, " ", "");
   StringReplace(out, "\t", "");
   return true;
}

//+------------------------------------------------------------------+
//| Extract long value for key                                         |
//+------------------------------------------------------------------+
bool GetJsonLong(const string json, const string key, long &out)
{
   string s;
   if(!GetJsonRawValue(json, key, s)) return false;
   out = (long)StringToInteger(s);
   return true;
}

//+------------------------------------------------------------------+
//| Extract unsigned long value for key                                |
//+------------------------------------------------------------------+
bool GetJsonULong(const string json, const string key, ulong &out)
{
   string s;
   if(!GetJsonRawValue(json, key, s)) return false;
   out = (ulong)StringToInteger(s);
   return true;
}

//+------------------------------------------------------------------+
//| Extract int value for key                                          |
//+------------------------------------------------------------------+
bool GetJsonInt(const string json, const string key, int &out)
{
   string s;
   if(!GetJsonRawValue(json, key, s)) return false;
   out = (int)StringToInteger(s);
   return true;
}

//+------------------------------------------------------------------+
//| Extract double value for key                                       |
//+------------------------------------------------------------------+
bool GetJsonDouble(const string json, const string key, double &out)
{
   string s;
   if(!GetJsonRawValue(json, key, s)) return false;
   out = StringToDouble(s);
   return true;
}

//+------------------------------------------------------------------+
//| Serialize event to JSON                                            |
//+------------------------------------------------------------------+
string CTradeMessage::EventToJson(const STradeEvent &e)
{
   string json = "{";
   json += "\"event\":" + JsonString(e.event) + ",";
   json += "\"timestamp\":" + IntegerToString(e.timestamp) + ",";
   json += "\"magic\":" + IntegerToString(e.magic) + ",";
   json += "\"master_ticket\":" + IntegerToString((long)e.master_ticket) + ",";
   json += "\"symbol\":" + JsonString(e.symbol) + ",";
   json += "\"side\":" + IntegerToString(e.side) + ",";
   json += "\"open_price\":" + DoubleToString(e.open_price, 8) + ",";
   json += "\"volume\":" + DoubleToString(e.volume, 3) + ",";
   json += "\"sl\":" + DoubleToString(e.sl, 8) + ",";
   json += "\"tp\":" + DoubleToString(e.tp, 8) + ",";
   json += "\"open_time\":" + IntegerToString((long)e.open_time) + ",";
   json += "\"point\":" + DoubleToString(e.point, 8) + ",";
   json += "\"comment\":" + JsonString(e.comment);
   json += "}";
   return json;
}

//+------------------------------------------------------------------+
//| Deserialize JSON to event                                          |
//+------------------------------------------------------------------+
bool CTradeMessage::JsonToEvent(const string json, STradeEvent &e)
{
   e.event = "";
   e.timestamp = 0;
   e.magic = 0;
   e.master_ticket = 0;
   e.symbol = "";
   e.side = 0;
   e.open_price = 0.0;
   e.volume = 0.0;
   e.sl = 0.0;
   e.tp = 0.0;
   e.open_time = 0;
   e.point = 0.0;
   e.comment = "";

   string val;
   if(!GetJsonString(json, "event", val)) return false;
   e.event = val;

   if(!GetJsonLong(json, "timestamp", e.timestamp)) return false;
   if(!GetJsonLong(json, "magic", e.magic)) return false;
   if(!GetJsonULong(json, "master_ticket", e.master_ticket)) return false;
   if(!GetJsonString(json, "symbol", e.symbol)) return false;
   if(!GetJsonInt(json, "side", e.side)) return false;
   if(!GetJsonDouble(json, "open_price", e.open_price)) return false;
   if(!GetJsonDouble(json, "volume", e.volume)) return false;
   if(!GetJsonDouble(json, "sl", e.sl)) return false;
   if(!GetJsonDouble(json, "tp", e.tp)) return false;
   long ot;
   if(!GetJsonLong(json, "open_time", ot)) return false;
   e.open_time = (datetime)ot;
   if(!GetJsonDouble(json, "point", e.point)) return false;
   if(!GetJsonString(json, "comment", e.comment)) return false;

   return true;
}

//+------------------------------------------------------------------+
//| Escape a string value for JSON                                     |
//+------------------------------------------------------------------+
string CTradeMessage::JsonString(const string value)
{
   string out = value;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return "\"" + out + "\"";
}

#endif
