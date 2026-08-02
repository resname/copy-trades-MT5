#ifndef SNAPSHOT_FILE_MQH
#define SNAPSHOT_FILE_MQH

#include "TradeMessage.mqh"

struct SPositionSnapshot
{
   ulong  ticket;
   double volume;
   double sl;
   double tp;
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
   json += "\"positions":[";
   int n = ArraySize(snapshot.positions);
   for(int i = 0; i < n; i++)
   {
      const SPositionSnapshot &p = snapshot.positions[i];
      json += "{";
      json += "\"ticket\":" + IntegerToString((long)p.ticket) + ",";
      json += "\"volume\":" + DoubleToString(p.volume, 8) + ",";
      json += "\"sl\":" + DoubleToString(p.sl, 8) + ",";
      json += "\"tp\":" + DoubleToString(p.tp, 8);
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
      SPositionSnapshot &p = snapshot.positions[count];

      string positionJson = StringSubstr(arrayBody, pos);

      string raw;
      if(GetJsonRawValue(positionJson, "ticket", raw))
         p.ticket = (ulong)StringToInteger(raw);
      else
         p.ticket = 0;

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

      // Advance search to the next position object.
      pos += StringLen("\"ticket\":");
      pos = StringFind(arrayBody, "\"ticket\":", pos);
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

   int handle = FileOpen(tmpPath, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_SHARE_READ);
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

   if(!FileMove(tmpPath, finalPath, FILE_REWRITE|FILE_COMMON))
   {
      PrintFormat("CSnapshotFile: failed to move %s to %s", tmpPath, finalPath);
      return false;
   }
   return true;
}

bool CSnapshotFile::Read(const string basePath, STradeSnapshot &snapshot)
{
   string path = SnapshotPath(basePath);
   int handle = FileOpen(path, FILE_READ|FILE_TXT|FILE_COMMON|FILE_SHARE_READ);
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
