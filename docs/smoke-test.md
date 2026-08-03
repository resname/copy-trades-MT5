# Manual Demo Smoke Test — CopyTrades MT5 (demo accounts only)

**Scope:** the tier-3 manual validation. The full unit + fake-worker
integration suite (`pytest manager/tests`) covers the copy logic with no
terminal and no GUI. This runbook is the only step that touches real MT5
terminals, and it is **demo accounts only** — never use a real account.

## Prereqs
- Windows 11, Python 3.11+.
- `pip install -e .` (pulls PySide6, pywin32, psutil, MetaTrader5).
- Two MT5 **demo** accounts on the same broker (one master, one slave),
  with their login (integer), password, and server name to hand.
- Internet (the `mt5setup.exe` web installer downloads components).

## Setup
1. Clear any prior manager state: delete `%APPDATA%\CopyTradesMT5\` and
   `%LOCALAPPDATA%\CopyTradesMT5\terminals\` so provisioning + the
   provisioned-instance registry start clean.
2. Launch: `python -m manager`.

## Run
3. **Discovery.** In the Master pane, open the Terminal dropdown. Confirm it
   lists any already-installed MT5 (`%APPDATA%\MetaQuotes\Terminal\<hash>\
   origin.txt` discovery + the default `C:\Program Files\MetaTrader 5\`).
4. **Provisioning.** Add one Slave (Add Slave → fill the slave demo account).
   Click Start. The status panel should show `provisioning…` then
   `provisioned 1 terminal instance(s): …instance_0`. Confirm a new terminal
   appears at `%LOCALAPPDATA%\CopyTradesMT5\terminals\instance_0\` with a
   `terminal64.exe` (portable — its data folder is inside the install dir,
   NOT under `%APPDATA%\MetaQuotes\Terminal\`).
5. **Readiness gate.** The log should show `starting slave workers…` then
   `slaves ready; starting master` — i.e. the master is spawned ONLY after
   the slave reported SymbolInfo + Status. (This is the Plan 2/3 startup-race
   fix. Without it, the master's first snapshot would beat the slave's
   SymbolInfo and the first OPEN would be permanently skipped.)
6. **Copy.** On the master demo terminal, open a small market position on a
   symbol the slave maps (e.g. EURUSD). Within ~1–2 s the slave demo
   terminal should open the mirrored position with the `CPY#<ticket>|MV..|SV..`
   comment. Modify the master SL/TP → the slave follows. Partial-close the
   master → the slave partial-closes. Fully close the master → the slave
   closes. Watch the status panel: per-slave connected/balance/equity updates;
   the log shows each OPEN/MODIFY/PARTIAL_CLOSE/CLOSE.
7. **Restart recovery.** Stop, then Start again (same accounts). The slave
   should NOT re-open the position it already holds (recovery seeds the
   RecordTable from the `CPY` comment; the first diff skips it). Confirm no
   duplicate.
8. **Worker crash → kill stale terminal → respawn (no -10003).** While
   copying, force-kill one worker's `terminal64.exe` from Task Manager. The
   supervisor should detect the death, call `kill_terminal(exe_path)` to
   clear any stale terminal for that instance, then respawn the worker. The
   log should show `restarted slave …` and copying should resume — with NO
   `initialize failed: -10003` error (the stale-terminal IPC collision the
   kill clears).
9. **Close-to-tray.** Close the window. It should hide to the tray (process
   + workers stay alive; copying continues). Double-click the tray icon to
   show it again.
10. **Orderly quit.** Tray → Quit. The log should show `stopping…` then
    `stopped`; all `terminal64.exe` the manager launched should exit within
    a few seconds (workers `mt5.shutdown()` on pipe EOF).

## Pass criteria
- Steps 5, 6, 8, 10 behave as described. Steps 3, 4, 7, 9 show the expected
  UI/FS state. No `CredentialDecryptError` on a fresh install (no stored
  creds yet); if you copy the settings file to another user/machine and
  Start, the GUI must re-prompt for the password (DPAPI cross-user failure).

## What this runbook does NOT cover (forward-looking)
- Mid-run slave respawn re-arming the readiness gate (Plan 3 MUST #5 — the
  master keeps sending during the respawn window; a respawned slave could
  miss a NEW). Watch for a missed open after a mid-run slave crash; if seen,
  that is the known deferred item.
- A real downloader for `mt5setup.exe` (Plan 3 MUST #1 — the default
  `SETUP_DOWNLOAD_URL` is the HTML page). If provisioning fails at the
  download step, pre-stage `mt5setup.exe` at
  `%LOCALAPPDATA%\CopyTradesMT5\mt5setup.exe` and re-run; or pass a real
  downloader in a future plan.