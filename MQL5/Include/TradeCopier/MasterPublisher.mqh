//+------------------------------------------------------------------+
//|                                       MasterPublisher.mqh        |
//+------------------------------------------------------------------+
#ifndef MASTER_PUBLISHER_MQH
#define MASTER_PUBLISHER_MQH

#include "SnapshotFile.mqh"
#include <Trade\PositionInfo.mqh>
#include "CopierConfig.mqh"

class CMasterPublisher
{
private:
   string            m_sharedPath;
   int               m_heartbeatSeconds;
   datetime          m_lastHeartbeat;
   ulong             m_lastPublish;
   SPositionSnapshot m_prevSnapshots[];

   int         FindSnapshotIndex(ulong ticket) const;
   int         FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const;
   void        BuildCurrentSnapshots(SPositionSnapshot &out[]);
   void        ReplaceSnapshots(const SPositionSnapshot &src[]);

public:
   CMasterPublisher() : m_heartbeatSeconds(0), m_lastHeartbeat(0), m_lastPublish(0)
   {
      m_sharedPath = "";
   }
   bool Init(const string sharedPath, int heartbeatSeconds);
   void Deinit();
   void PublishChanges(int intervalMs);
};

bool CMasterPublisher::Init(const string sharedPath, int heartbeatSeconds)
{
   m_sharedPath = sharedPath;
   m_heartbeatSeconds = heartbeatSeconds;
   m_lastHeartbeat = 0;
   m_lastPublish = 0;
   ArrayResize(m_prevSnapshots, 0);

   // Ensure the shared directory exists.
   if(!FolderCreate(m_sharedPath, FILE_COMMON))
   {
      int err = GetLastError();
      if(err != 5052) // ERR_FILE_ALREADY_EXIST
      {
         PrintFormat("MasterPublisher: failed to create shared path %s (error %d)", m_sharedPath, err);
         return false;
      }
   }

   PrintFormat("MasterPublisher: using shared path %s", m_sharedPath);
   return true;
}

void CMasterPublisher::Deinit()
{
   ArrayResize(m_prevSnapshots, 0);
}

void CMasterPublisher::PublishChanges(int intervalMs)
{
   ulong now = GetTickCount();
   if(now - m_lastPublish < (uint)intervalMs)
      return;
   m_lastPublish = now;

   STradeSnapshot snapshot;
   snapshot.timestamp = (long)TimeLocal();
   snapshot.heartbeat = snapshot.timestamp;

   BuildCurrentSnapshots(snapshot.positions);

   if(!CSnapshotFile::Write(m_sharedPath, snapshot))
      Print("MasterPublisher: failed to write snapshot");

   // Update internal prev snapshots from the written snapshot for closed-position detection.
   ReplaceSnapshots(snapshot.positions);

   m_lastHeartbeat = (datetime)snapshot.heartbeat;
}

int CMasterPublisher::FindSnapshotIndex(ulong ticket) const
{
   return FindSnapshotIndex(m_prevSnapshots, ticket);
}

int CMasterPublisher::FindSnapshotIndex(const SPositionSnapshot &snapshots[], ulong ticket) const
{
   int n = ArraySize(snapshots);
   for(int i = 0; i < n; i++)
      if(snapshots[i].ticket == ticket)
         return i;
   return -1;
}

void CMasterPublisher::BuildCurrentSnapshots(SPositionSnapshot &out[])
{
   ArrayResize(out, 0);

   int total = PositionsTotal();
   int count = 0;
   for(int i = 0; i < total; i++)
   {
      CPositionInfo pos;
      if(!pos.SelectByIndex(i))
         continue;

      ulong ticket = pos.Ticket();
      if(ticket == 0)
         continue;

      if(count >= ArraySize(out))
         ArrayResize(out, count + 1);

      out[count].ticket     = ticket;
      out[count].symbol     = pos.Symbol();
      out[count].side       = (int)pos.PositionType();
      out[count].open_price = pos.PriceOpen();
      out[count].volume     = pos.Volume();
      out[count].sl         = pos.StopLoss();
      out[count].tp         = pos.TakeProfit();
      out[count].open_time  = (long)pos.Time();
      out[count].point      = SymbolInfoDouble(pos.Symbol(), SYMBOL_POINT);
      out[count].comment    = pos.Comment();
      count++;
   }
}

void CMasterPublisher::ReplaceSnapshots(const SPositionSnapshot &src[])
{
   int n = ArraySize(src);
   ArrayResize(m_prevSnapshots, n);
   for(int i = 0; i < n; i++)
      m_prevSnapshots[i] = src[i];
}

#endif
