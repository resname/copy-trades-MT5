# Testing Instructions — MT5 Trade Copier

> Use these steps after moving the dev environment to a Windows machine with MetaTrader 5 installed.

---

## 1. Prepare the MT5 Data Folder

1. Clone or pull the repo on the Windows machine:
   ```powershell
   git clone https://github.com/resname/copy-trades-MT5.git
   cd copy-trades-MT5
   ```

2. Open MetaTrader 5 and find the data folder:
   - In MT5: `File → Open Data Folder`
   - Typical path: `C:\Users\<You>\AppData\Roaming\MetaQuotes\Terminal\<hash>\MQL5`

3. Copy the source files into the terminal data folder:
   - `MQL5/Experts/TradeCopier/TradeCopier.mq5` → `MQL5/Experts/TradeCopier/TradeCopier.mq5`
   - `MQL5/Include/TradeCopier/*.mqh` → `MQL5/Include/TradeCopier/*.mqh`

   If the folders do not exist, create them first.

---

## 2. Compile in MetaEditor

1. Open MetaEditor (from MT5: `Tools → MetaEditor`).
2. Open `MQL5/Experts/TradeCopier/TradeCopier.mq5`.
3. Press `F7` (or click **Compile**).
4. Expected result: **0 errors, 0 warnings**.
5. If compilation fails, note the exact error message and file/line, then fix the source and recompile.

---

## 3. Localhost Smoke Test (Same PC)

Use two MT5 terminals on the same Windows machine (two demo accounts).

### 3.1 Start the master
1. Open the first terminal and log in to the master account.
2. Attach `TradeCopier` to any chart.
3. In the EA inputs, set `CopierMode = MASTER`.
4. Click **OK**.
5. Expected: the chart panel shows `Mode: MASTER` and `Status: advertising`.
6. Check the **Experts** tab for:
   ```
   TradeCopier: running as MASTER
   MasterPublisher: TCP server on port <number>
   ```

### 3.2 Start the slave
1. Open the second terminal and log in to the slave account.
2. Attach `TradeCopier` to any chart.
3. In the EA inputs, set `CopierMode = SLAVE`.
4. Click **OK**.
5. Expected: the chart panel shows `Mode: SLAVE`, then `Status: connected` and a latency value.
6. Check the **Experts** tab for:
   ```
   TradeCopier: running as SLAVE
   SlaveSubscriber: connected to master 127.0.0.1:<port>
   ```

### 3.3 Verify trade mirroring
1. Open a market order on the master chart.
2. Within ~1 second, the slave should open the corresponding position.
3. Modify SL/TP on the master → slave SL/TP updates.
4. Partially close the master position → slave closes the same fraction.
5. Fully close the master position → slave position closes.

---

## 4. LAN Smoke Test (Two PCs)

Use two PCs connected to the same local network.

### 4.1 Start the master
1. On PC A, attach `TradeCopier` with `CopierMode = MASTER`.
2. Confirm the panel shows `Mode: MASTER` and `Status: advertising`.
3. Note the **master PC's local IP address** if you want to verify manually, but no manual IP entry is required.

### 4.2 Start the slave
1. On PC B, attach `TradeCopier` with `CopierMode = SLAVE`.
2. Confirm the slave panel shows the real master endpoint (for example `192.168.x.x:<port>`) and a latency value.
3. If the slave stays at `searching...`, check the firewall settings on both PCs (UDP broadcast on port `55555` and the dynamically chosen TCP port must be allowed).

### 4.3 Verify trade mirroring
1. Open/close/modify trades on the master PC.
2. Confirm the slave PC mirrors them.

---

## 5. GUI Tests

1. **Mode / status / latency / master endpoint**
   - Confirm the General tab shows the correct mode and updates the status label when the slave connects/disconnects.

2. **Symbol-mapping table**
   - On the slave chart, click the Symbols tab.
   - Type a master symbol in the left column (for example `US30`) and a slave symbol in the right column (for example `WS30`).
   - Click somewhere outside the edit box or press Enter.
   - Check the **Experts** tab — the EA prints a line like:
     ```
     Updated SymbolMap: US30=WS30
     ```
   - To persist the mapping, copy that string into the EA's `SymbolMap` input and re-attach the EA.

3. **Add / delete rows**
   - Type into the empty bottom row to add a new mapping.
   - Click the `x` button next to a row to remove it.
   - Confirm the printed `SymbolMap` matches the visible table.

---

## 6. Multi-Slave Test

1. Attach a second slave EA to another chart/terminal (same or different PC).
2. Give it a different `SymbolMap`.
3. Open a trade on the master.
4. Confirm both slaves mirror the trade independently.
5. Close the copied position on one slave.
6. Confirm the other slave and the master remain unaffected.

---

## 7. Restart Recovery Test

1. With a copied position still open on the master, stop the slave EA.
2. Wait a few seconds, then re-attach the slave EA.
3. Confirm the slave does **not** create a duplicate for the existing copied position.
4. Confirm trades older than `MaxTradeAgeMinutes` are ignored on restart.

---

## 8. Heartbeat Test

1. With master and slave connected, stop the master EA.
2. After more than `HeartbeatSeconds * 2`, the slave should log a missing-heartbeat warning.
3. Restart the master EA.
4. The slave should reconnect and the warning should stop.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `error: 'SharedDataPath' - undeclared identifier` | Old file-based inputs still referenced | Use the current `TradeCopier.mq5` from the repo |
| Slave stays at `searching...` | UDP broadcast blocked or master not advertising | Check Windows firewall; confirm master is running on the same LAN; try same-PC test first |
| Compile error in `LanTransport.mqh` | Wrong array type or API signature | Make sure the file matches the repo exactly (`uchar` arrays, `SocketSend(socket, tail, remaining)`) |
| Duplicate copied positions on restart | Record rebuild failed | Verify position comments contain `CPY#<ticket>` and magic number >= `MAGIC_BASE` |
| Symbol mapping not applied | GUI map differs from input | Copy the printed `Updated SymbolMap: ...` line into the EA's `SymbolMap` input |

---

## 10. Feature Test Checklist

For a structured pass/fail checklist, see the **Feature Test Checklist** section in `README.md`.

---

## 11. Quick Build-Test Loop

When changing code:

1. Edit the source file in the repo.
2. Copy the changed `.mqh`/`.mq5` file(s) into the MT5 data folder.
3. Recompile in MetaEditor (`F7`).
4. Re-attach the EA to the chart.
5. Run the relevant smoke test above.
6. If everything passes, commit and push from the repo:
   ```bash
   git add -A
   git commit -m "fix: ..." -m "Co-Authored-By: Claude <noreply@anthropic.com>"
   git push origin main
   ```
