#ifndef SNAPSHOT_FILE_MQH
#define SNAPSHOT_FILE_MQH

#include "TradeMessage.mqh"

struct SPositionSnapshot
{
   ulong  ticket;
   string symbol;
   int    side;
   double open_price;
   double volume;
   double sl;
   double tp;
   long   open_time;
   double point;
   string comment;
};

struct STradeSnapshot
{
   long                timestamp;
   long                heartbeat;
   SPositionSnapshot   positions[];
};

class CSnapshotFile
{
private:
   static string SnapshotPath(const string basePath);
   static string TempPath(const string basePath);
   static string SnapshotToJson(const STradeSnapshot &snapshot);
   static bool   JsonToSnapshot(const string json, STradeSnapshot &snapshot);

public:
   static bool Write(const string basePath, const STradeSnapshot &snapshot);
   static bool Read(const string basePath, STradeSnapshot &snapshot);
};

string CSnapshotFile::SnapshotPath(const string basePath)
{
   string folder = basePath;
   int len = StringLen(folder);
   if(len > 0 && folder[len - 1] != '\\' && folder[len - 1] != '/')
      folder += "\\";
   return folder + "TradeCopier.snapshot.json";
}

string CSnapshotFile::TempPath(const string basePath)
{
   return SnapshotPath(basePath) + ".tmp";
}

string CSnapshotFile::SnapshotToJson(const STradeSnapshot &snapshot)
{
   string json = "{";
   json += "\"timestamp\":" + IntegerToString(snapshot.timestamp) + ",";
   json += "\"heartbeat\":" + IntegerToString(snapshot.heartbeat) + ",";
   json += "\"positions\":";
   json += "[";
   int n = ArraySize(snapshot.positions);
   for(int i = 0; i < n; i++)
   {
      SPositionSnapshot p = snapshot.positions[i];
      json += "{";
      json += "\"ticket\":" + IntegerToString((long)p.ticket) + ",";
      json += "\"symbol\":" + CTradeMessage::JsonString(p.symbol) + ",";
      json += "\"side\":" + IntegerToString(p.side) + ",";
      json += "\"open_price\":" + DoubleToString(p.open_price, 8) + ",";
      json += "\"volume\":" + DoubleToString(p.volume, 8) + ",";
      json += "\"sl\":" + DoubleToString(p.sl, 8) + ",";
      json += "\"tp\":" + DoubleToString(p.tp, 8) + ",";
      json += "\"open_time\":" + IntegerToString(p.open_time) + ",";
      json += "\"point\":" + DoubleToString(p.point, 8) + ",";
      json += "\"comment\":" + CTradeMessage::JsonString(p.comment);
      json += "}";
      if(i < n - 1) json += ",";
   }
   json += "]";
   json += "}";
   return json;
}

bool CSnapshotFile::JsonToSnapshot(const string json, STradeSnapshot &snapshot)
{
   snapshot.timestamp = 0;
   snapshot.heartbeat = 0;
   ArrayResize(snapshot.positions, 0);

   if(!GetJsonLong(json, "timestamp", snapshot.timestamp)) return false;
   if(!GetJsonLong(json, "heartbeat", snapshot.heartbeat)) return false;

   // Count positions by scanning for "ticket" keys inside the positions array.
   int searchPos = StringFind(json, "\"positions\":");
   if(searchPos == -1) return false;

   int arrayStart = StringFind(json, "[", searchPos);
   int arrayEnd = StringFind(json, "]", arrayStart);
   if(arrayStart == -1 || arrayEnd == -1 || arrayEnd <= arrayStart) return false;

   string arrayBody = StringSubstr(json, arrayStart + 1, arrayEnd - arrayStart - 1);

   int count = 0;
   int pos = StringFind(arrayBody, "\"ticket\":");
   while(pos != -1)
   {
      if(count >= ArraySize(snapshot.positions))
         ArrayResize(snapshot.positions, count + 1);
      SPositionSnapshot p;

      string positionJson = StringSubstr(arrayBody, pos);

      string raw;
      if(GetJsonRawValue(positionJson, "ticket", raw))
         p.ticket = (ulong)StringToInteger(raw);
      else
         p.ticket = 0;

      if(GetJsonString(positionJson, "symbol", raw))
         p.symbol = raw;
      else
         p.symbol = "";

      if(GetJsonRawValue(positionJson, "side", raw))
         p.side = (int)StringToInteger(raw);
      else
         p.side = 0;

      if(GetJsonRawValue(positionJson, "open_price", raw))
         p.open_price = StringToDouble(raw);
      else
         p.open_price = 0.0;

      if(GetJsonRawValue(positionJson, "volume", raw))
         p.volume = StringToDouble(raw);
      else
         p.volume = 0.0;

      if(GetJsonRawValue(positionJson, "sl", raw))
         p.sl = StringToDouble(raw);
      else
         p.sl = 0.0;

      if(GetJsonRawValue(positionJson, "tp", raw))
         p.tp = StringToDouble(raw);
      else
         p.tp = 0.0;

      if(GetJsonRawValue(positionJson, "open_time", raw))
         p.open_time = StringToInteger(raw);
      else
         p.open_time = 0;

      if(GetJsonRawValue(positionJson, "point", raw))
         p.point = StringToDouble(raw);
      else
         p.point = 0.0;

      if(GetJsonString(positionJson, "comment", raw))
         p.comment = raw;
      else
         p.comment = "";

      // Advance search to the next position object.
      pos += StringLen("\"ticket\":");
      pos = StringFind(arrayBody, "\"ticket\":", pos);

      if(count >= ArraySize(snapshot.positions))
         ArrayResize(snapshot.positions, count + 1);
      snapshot.positions[count] = p;
      count++;
   }

   if(count != ArraySize(snapshot.positions))
      ArrayResize(snapshot.positions, count);
   return true;
}

bool CSnapshotFile::Write(const string basePath, const STradeSnapshot &snapshot)
{
   string tmpPath = TempPath(basePath);
   string finalPath = SnapshotPath(basePath);

   int handle = FileOpen(tmpPath, FILE_WRITE|FILE_TXT|FILE_COMMON);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("CSnapshotFile: cannot open tmp file %s", tmpPath);
      return false;
   }

   string json = SnapshotToJson(snapshot);
   if(FileWriteString(handle, json) <= 0)
   {
      FileClose(handle);
      PrintFormat("CSnapshotFile: failed to write tmp file %s", tmpPath);
      return false;
   }
   FileClose(handle);

   if(!FileMove(tmpPath, FILE_COMMON, finalPath, FILE_COMMON|FILE_REWRITE))
   {
      PrintFormat("CSnapshotFile: failed to move %s to %s", tmpPath, finalPath);
      return false;
   }
   return true;
}

bool CSnapshotFile::Read(const string basePath, STradeSnapshot &snapshot)
{
   string path = SnapshotPath(basePath);
   int handle = FileOpen(path, FILE_READ|FILE_TXT|FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return false;

   string json = "";
   while(!FileIsEnding(handle))
   {
      string line = FileReadString(handle);
      json += line;
   }
   FileClose(handle);

   return JsonToSnapshot(json, snapshot);
}
#endif
