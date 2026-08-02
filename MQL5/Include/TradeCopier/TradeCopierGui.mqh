#ifndef TRADE_COPIER_GUI_MQH
#define TRADE_COPIER_GUI_MQH

const string GUI_PREFIX = "TC_GUI_";
const int    GUI_WIDTH  = 350;
const int    GUI_HEIGHT = 400;
const int    ROW_HEIGHT = 22;

class CTradeCopierGui
{
private:
   long    m_chartId;
   string  m_symbolMap;
   int     m_rowCount;

   string  MakeName(const string suffix);
   void    CreateGeneralTab();
   void    CreateSymbolsTab();
   void    RefreshSymbolRows();
   void    AddSymbolRow(int index, const string master, const string slave);
   void    DestroyAllObjects();

public:
   CTradeCopierGui() : m_chartId(0), m_rowCount(0) {}

   void Create(long chartId);
   void Destroy();
   void SetMode(const string mode);
   void SetStatus(const string status);
   void SetLatency(int ms);
   void SetMasterEndpoint(const string endpoint);
   void SetSymbolMap(const string &symbolMap);
   string GetSymbolMap() const { return m_symbolMap; }
   void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam);
};

string CTradeCopierGui::MakeName(const string suffix)
{
   return GUI_PREFIX + suffix;
}

void CTradeCopierGui::Create(long chartId)
{
   m_chartId = chartId;
   DestroyAllObjects();

   int x = 10;
   int y = 30;

   ObjectCreate(m_chartId, MakeName("BG"), OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_XDISTANCE, x);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_XSIZE, GUI_WIDTH);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_YSIZE, GUI_HEIGHT);
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_BGCOLOR, C'28,28,28');
   ObjectSetInteger(m_chartId, MakeName("BG"), OBJPROP_BORDER_TYPE, BORDER_FLAT);

   ObjectCreate(m_chartId, MakeName("TITLE"), OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_XDISTANCE, x + 10);
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_YDISTANCE, y + 10);
   ObjectSetString(m_chartId, MakeName("TITLE"), OBJPROP_FONT, "Arial");
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_FONTSIZE, 10);
   ObjectSetString(m_chartId, MakeName("TITLE"), OBJPROP_TEXT, "X1 Copy MT5");
   ObjectSetInteger(m_chartId, MakeName("TITLE"), OBJPROP_COLOR, clrWhite);

   CreateGeneralTab();
   CreateSymbolsTab();
}

void CTradeCopierGui::CreateGeneralTab()
{
   int x = 20;
   int y = 55;

   string labels[] = {"MODE_LABEL", "STATUS_LABEL", "LATENCY_LABEL", "MASTER_LABEL"};
   for(int i = 0; i < ArraySize(labels); i++)
   {
      ObjectCreate(m_chartId, MakeName(labels[i]), OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(m_chartId, MakeName(labels[i]), OBJPROP_XDISTANCE, x);
      ObjectSetInteger(m_chartId, MakeName(labels[i]), OBJPROP_YDISTANCE, y + i * ROW_HEIGHT);
      ObjectSetInteger(m_chartId, MakeName(labels[i]), OBJPROP_COLOR, clrWhite);
   }

   ObjectSetString(m_chartId, MakeName("MODE_LABEL"), OBJPROP_TEXT, "Mode: SLAVE");
   ObjectSetString(m_chartId, MakeName("STATUS_LABEL"), OBJPROP_TEXT, "Status: searching...");
   ObjectSetString(m_chartId, MakeName("LATENCY_LABEL"), OBJPROP_TEXT, "Latency: --");
   ObjectSetString(m_chartId, MakeName("MASTER_LABEL"), OBJPROP_TEXT, "Master: --");
}

void CTradeCopierGui::SetMode(const string mode)
{
   ObjectSetString(m_chartId, MakeName("MODE_LABEL"), OBJPROP_TEXT, "Mode: " + mode);
}

void CTradeCopierGui::SetStatus(const string status)
{
   ObjectSetString(m_chartId, MakeName("STATUS_LABEL"), OBJPROP_TEXT, "Status: " + status);
}

void CTradeCopierGui::SetLatency(int ms)
{
   string text = (ms < 0) ? "Latency: --" : StringFormat("Latency: %d ms", ms);
   ObjectSetString(m_chartId, MakeName("LATENCY_LABEL"), OBJPROP_TEXT, text);
}

void CTradeCopierGui::SetMasterEndpoint(const string endpoint)
{
   ObjectSetString(m_chartId, MakeName("MASTER_LABEL"), OBJPROP_TEXT, "Master: " + endpoint);
}

void CTradeCopierGui::CreateSymbolsTab()
{
   int x = 20;
   int y = 150;

   ObjectCreate(m_chartId, MakeName("SYM_HEADER"), OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("SYM_HEADER"), OBJPROP_XDISTANCE, x);
   ObjectSetInteger(m_chartId, MakeName("SYM_HEADER"), OBJPROP_YDISTANCE, y);
   ObjectSetString(m_chartId, MakeName("SYM_HEADER"), OBJPROP_TEXT, "Master -> Slave");
   ObjectSetInteger(m_chartId, MakeName("SYM_HEADER"), OBJPROP_COLOR, clrWhite);

   ObjectCreate(m_chartId, MakeName("SYM_ADD"), OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_XDISTANCE, x + 220);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_XSIZE, 60);
   ObjectSetInteger(m_chartId, MakeName("SYM_ADD"), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName("SYM_ADD"), OBJPROP_TEXT, "+ Add");

   ObjectCreate(m_chartId, MakeName("SYM_COPY"), OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_XDISTANCE, x + 220);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_YDISTANCE, y + 20);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_XSIZE, 100);
   ObjectSetInteger(m_chartId, MakeName("SYM_COPY"), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName("SYM_COPY"), OBJPROP_TEXT, "Copy string");

   RefreshSymbolRows();
}

void CTradeCopierGui::SetSymbolMap(const string &symbolMap)
{
   m_symbolMap = symbolMap;
   RefreshSymbolRows();
}

void CTradeCopierGui::RefreshSymbolRows()
{
   // Destroy existing rows.
   for(int i = 0; i < m_rowCount; i++)
   {
      ObjectDelete(m_chartId, MakeName("SYM_MASTER_" + IntegerToString(i)));
      ObjectDelete(m_chartId, MakeName("SYM_SLAVE_" + IntegerToString(i)));
      ObjectDelete(m_chartId, MakeName("SYM_DEL_" + IntegerToString(i)));
   }

   // Parse current map.
   string pairs[];
   int count = StringSplit(m_symbolMap, ',', pairs);
   m_rowCount = 0;

   int x = 20;
   int y = 175;

   for(int i = 0; i < count; i++)
   {
      string pair = pairs[i];
      StringReplace(pair, " ", "");
      int eq = StringFind(pair, "=");
      if(eq == -1) continue;

      string master = StringSubstr(pair, 0, eq);
      string slave  = StringSubstr(pair, eq + 1);
      if(master == "" || slave == "") continue;

      AddSymbolRow(m_rowCount, master, slave);
      m_rowCount++;
   }

   // Always add one empty row at the end.
   AddSymbolRow(m_rowCount, "", "");
   m_rowCount++;
}

void CTradeCopierGui::AddSymbolRow(int index, const string master, const string slave)
{
   int x = 20;
   int y = 175 + index * ROW_HEIGHT;

   string editMaster = "SYM_MASTER_" + IntegerToString(index);
   string editSlave  = "SYM_SLAVE_" + IntegerToString(index);
   string btnDel     = "SYM_DEL_" + IntegerToString(index);

   ObjectCreate(m_chartId, MakeName(editMaster), OBJ_EDIT, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_XDISTANCE, x);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_XSIZE, 80);
   ObjectSetInteger(m_chartId, MakeName(editMaster), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName(editMaster), OBJPROP_TEXT, master);

   ObjectCreate(m_chartId, MakeName(editSlave), OBJ_EDIT, 0, 0, 0);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_XDISTANCE, x + 90);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_YDISTANCE, y);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_XSIZE, 80);
   ObjectSetInteger(m_chartId, MakeName(editSlave), OBJPROP_YSIZE, 18);
   ObjectSetString(m_chartId, MakeName(editSlave), OBJPROP_TEXT, slave);

   if(master != "" || slave != "")
   {
      ObjectCreate(m_chartId, MakeName(btnDel), OBJ_BUTTON, 0, 0, 0);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_XDISTANCE, x + 180);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_YDISTANCE, y);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_XSIZE, 18);
      ObjectSetInteger(m_chartId, MakeName(btnDel), OBJPROP_YSIZE, 18);
      ObjectSetString(m_chartId, MakeName(btnDel), OBJPROP_TEXT, "x");
   }
}

void CTradeCopierGui::OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(id != CHARTEVENT_OBJECT_ENDEDIT && id != CHARTEVENT_OBJECT_CLICK)
      return;

   // Rebuild symbol map from all edit boxes.
   string map = "";
   for(int i = 0; i < m_rowCount; i++)
   {
      string master = ObjectGetString(m_chartId, MakeName("SYM_MASTER_" + IntegerToString(i)), OBJPROP_TEXT);
      string slave  = ObjectGetString(m_chartId, MakeName("SYM_SLAVE_" + IntegerToString(i)), OBJPROP_TEXT);
      StringReplace(master, " ", "");
      StringReplace(slave, " ", "");
      if(master != "" && slave != "")
      {
         if(map != "") map += ",";
         map += master + "=" + slave;
      }
   }

   if(map != m_symbolMap)
   {
      m_symbolMap = map;
      RefreshSymbolRows();
   }

   // Handle delete / add / copy buttons.
   if(id == CHARTEVENT_OBJECT_CLICK)
   {
      if(sparam == MakeName("SYM_ADD"))
      {
         // Empty row already exists; user can type into it.
         return;
      }
      if(sparam == MakeName("SYM_COPY"))
      {
         // MQL5 cannot access the system clipboard; print the string.
         Print("SymbolMap: " + m_symbolMap);
         return;
      }

      // Check delete buttons.
      for(int i = 0; i < m_rowCount; i++)
      {
         if(sparam == MakeName("SYM_DEL_" + IntegerToString(i)))
         {
            string master = ObjectGetString(m_chartId, MakeName("SYM_MASTER_" + IntegerToString(i)), OBJPROP_TEXT);
            string slave  = ObjectGetString(m_chartId, MakeName("SYM_SLAVE_" + IntegerToString(i)), OBJPROP_TEXT);
            StringReplace(master, " ", "");
            StringReplace(slave, " ", "");
            if(master != "" && slave != "")
            {
               const string entry = master + "=" + slave;
               string parts[];
               const int n = StringSplit(m_symbolMap, ',', parts);
               string rebuilt = "";
               for(int k = 0; k < n; k++)
               {
                  string trimmed = parts[k];
                  StringReplace(trimmed, " ", "");
                  if(trimmed == entry) continue;
                  if(rebuilt != "") rebuilt += ",";
                  rebuilt += trimmed;
               }
               if(rebuilt != m_symbolMap)
               {
                  m_symbolMap = rebuilt;
                  RefreshSymbolRows();
               }
            }
            return;
         }
      }
   }
}

void CTradeCopierGui::Destroy()
{
   DestroyAllObjects();
}

void CTradeCopierGui::DestroyAllObjects()
{
   int total = ObjectsTotal(m_chartId);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(m_chartId, i);
      if(StringFind(name, GUI_PREFIX) == 0)
         ObjectDelete(m_chartId, name);
   }
}

#endif
