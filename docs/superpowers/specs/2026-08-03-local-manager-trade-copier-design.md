# Local-Manager Trade Copier — Design

Date: 2026-08-03
Status: Approved (brainstormed with user)
Supersedes (as the active product direction): the file-based and LAN-TCP MQL5 EA designs. The MQL5 EA remains in the repo as the prior implementation and the logic reference for the port.

## Background

The original product was an MQL5 Expert Advisor (`MQL5/Experts/TradeCopier/TradeCopier.mq5`) that copied trades master→slave via a shared JSON snapshot file, with an on-chart GUI. A reverse-engineering spike then tried to eliminate the terminal entirely (standalone cold login to the MT5 trade server with no terminal). The spike concluded FAIL: the trade-server login cipher is a custom software implementation with no hooked-crypto surface, the cipher path was located but the plaintext/key were not recoverable within budget, and even a fully successful RE would yield a build-volatile standalone login (MetaQuotes changes the wire format between builds). See `spike/verdict.md` and `spike/NOTES.md`.

The user accepted a local-manager architecture instead: MT5 terminals stay on the user's PC, driven by a standalone manager GUI through the official `MetaTrader5` Python package. This is build-stable (MetaQuotes maintains the protocol) and is the same reason every working programmatic-MT5 solution keeps a terminal in the loop.

## Goal

A standalone Windows desktop application that:

- Presents login pages for a master MT5 account and one or more slave MT5 accounts.
- Copies open positions from the master to every slave (new opens, SL/TP modifications, partial closes, full closes).
- Provides per-slave symbol mapping, balance-step lot sizing, and raw-distance SL/TP normalization.
- Runs as a single manager program with a GUI, minimizable to the system tray so copying continues in the background.
- Handles real and demo accounts, with credentials encrypted at rest via Windows DPAPI.

## Non-goals

- No standalone login without an MT5 terminal (the RE spike ruled this out).
- No copying of pending (limit/stop) orders — positions only, matching the EA.
- No extra safety layer beyond the EA's `maxLot` + `maxTradeAge` (panic stop, exposure caps, drawdown guards were considered and explicitly declined by the user).
- No macOS/Linux support — the `MetaTrader5` package is Windows-only.

## Decisions locked in brainstorming

| Decision | Choice |
|---|---|
| GUI form factor | Desktop app (PySide6 / Qt6) |
| Topology | One master → many slaves |
| Copy scope | Open positions only (new / modify / partial / close) |
| Start-sync behavior | Recent + forward: ignore master positions older than per-slave `maxTradeAge` on baseline, copy recent opens + everything new going forward (matches the EA's `EstablishBaseline` + `maxTradeAgeMinutes`) |
| Account types | Demo and real; credentials DPAPI-encrypted at rest |
| Safety controls | EA-level only (`maxLot` + `maxTradeAge`) |
| Run mode | Tray / background capable — closing the window minimizes to tray and the engine keeps running |

## Critical constraint

The `MetaTrader5` Python package holds a **single terminal connection per process** (global `mt5.initialize()` state; no documented way to hold two connections in one process). Driving a master terminal plus one or more slave terminals concurrently therefore requires **one worker subprocess per terminal connection**. This shapes the entire architecture.

## Architecture

Two process tiers:

- **Manager process** (PySide6 GUI + the engine). The hub. Hosts the diff/replicate brain, the GUI, the tray icon, and the worker supervisor.
- **Worker subprocesses** — one `mt5_worker.py` per terminal connection (1 master + N slaves). Each holds exactly one `MetaTrader5` connection. Parameterized by role: `master` or `slave`.

**IPC.** A `multiprocessing` Pipe per worker (bidirectional, in-process, no ports to manage). Credentials are passed to workers *through the pipe*, never on the command line, so they do not appear in the process list.

**Manager-side components:**

- **GUI** (PySide6): Master pane (login + terminal path), a Slave list (add/remove; each slave = login + terminal path + editable symbol-map table + lot-sizing fields + SL/TP-normalize toggle + maxLot + maxTradeAge), Start/Stop, a live status panel (per-slave latency / last action / errors), and a log view. Minimizes to a system-tray icon.
- **Engine** — the brain, a direct port of the EA's logic to Python:
  - `SnapshotDiff` — detect NEW / MODIFY (SL or TP changed) / PARTIAL (volume down) / CLOSE (ticket gone) by master ticket.
  - `Baseline` + `maxTradeAge` filter (per-slave) — the recent+forward start behavior.
  - `Transform` per slave: `SymbolMapper`, `LotSizer` (balance-step + lot-step rounding + min/max cap), `PriceNormalizer` (raw-distance SL/TP + tick rounding).
  - `RecordTable` per slave — master ticket → {slave ticket, master open volume, slave open volume}, the linkage for modify/partial/close and for recovery.
- **Worker supervisor** — spawns workers, watches for subprocess exit, restarts on crash, routes pipe messages.
- **Settings store** — DPAPI-encrypted credentials + per-account/global config, saved under `%APPDATA%\CopyTradesMT5\`.

**Worker-side components (`mt5_worker.py`, role-parameterized):**

- **Master role:** `mt5.initialize(path, login, password, server)` → poll `positions_get()` on `masterIntervalMs` → stream full snapshots up the pipe; also report account info (balance/equity) for the status panel.
- **Slave role:** connect → **restart recovery** on init (scan that terminal's open positions for magic in `[MAGIC_BASE, MAGIC_BASE+900000)` + a `CPY#<ticket>|MV<vol>|SV<vol>` comment, rebuild the record table, send it up so the manager seeds its `RecordTable` and never duplicates) → receive commands, execute `order_send` (open/modify/partial-close/close) with `retryCount`/`retryDelayMs`, report acks (slave ticket + fill price + fill volume) and account info back up.

**Shared protocol module** (imported by both manager and worker): message types (`Snapshot`, `Command`, `Ack`, `Status`, `Error`, `RecoveryRecords`) + the `MAGIC_BASE`/comment encode-decode helpers.

**Linkage scheme (reused verbatim from the EA):**

- Slave magic number = `MAGIC_BASE + (master_ticket % 900000)`, with `MAGIC_BASE = 1000000`.
- Copied-position comment = `CPY#<master_ticket>|MV<master_open_vol>|SV<slave_open_vol>`.

This is the linkage key for MODIFY/PARTIAL/CLOSE and for restart recovery. Reusing the EA's exact scheme means a slave could be migrated between the old EA and the new manager without confusing state.

## Data flow

**Startup:**

1. User fills the Master pane + adds Slaves (each with its own config), clicks **Start**.
2. Manager DPAPI-decrypts credentials in-process and spawns one worker subprocess per account, passing config + credentials over the pipe (never argv).
3. Each worker `mt5.initialize(path, login, password, server)`. On success it reports `Status(connected, account_info)`. On failure it reports `Error` and the supervisor retries connect with backoff.
4. Slave workers run restart recovery immediately on connect and send `RecoveryRecords` up; the manager seeds each slave's `RecordTable` from it.

**Steady-state copy loop (every `masterIntervalMs`):**

1. Master worker polls `positions_get()` → sends one `Snapshot` (timestamp + heartbeat + full position list: ticket, symbol, side, open_price, volume, sl, tp, open_time, point) up the pipe.
2. Manager `SnapshotDiff` compares against the previous master snapshot → emits master events: `NEW(ticket)`, `MODIFY(ticket, new_sl, new_tp)`, `PARTIAL(ticket, new_vol)`, `CLOSE(ticket)`.
3. For **each slave**, the engine walks the events through that slave's `RecordTable` + `Transform`:

| Event | Per-slave handling |
|---|---|
| `NEW` | If master ticket already in this slave's `RecordTable` → skip (already copied; this is the restart/baseline case — see below). Else if `open_time` older than `slave.maxTradeAge` → mark seen, skip (baseline). Else resolve symbol via `SymbolMapper`; if missing on slave → skip + log. Compute lots via `LotSizer` from slave balance. Normalize SL/TP via `PriceNormalizer`. Send `Command(OPEN, …)`. |

**Restart/baseline correctness:** on manager start (or restart), the manager has no previous master snapshot, so the first snapshot's diff treats every current master position as `NEW`. Recovery has already seeded each slave's `RecordTable` with the master tickets that slave already holds. The `RecordTable` membership check in the `NEW` handler therefore skips already-copied positions on the first tick (no duplicate opens) while still letting genuinely-new positions through. After the first snapshot, the manager stores it as the "previous" snapshot and subsequent diffs only emit `NEW` for tickets not seen before.
| `MODIFY` | If ticket in `RecordTable` → re-normalize SL/TP to the slave's open price, send `Command(MODIFY, slave_ticket, sl, tp)`. |
| `PARTIAL` | `fraction = new_master_vol / master_open_vol`; `target_slave_vol = slave_open_vol * fraction`; close `current_slave_vol - target_slave_vol` rounded to lot step → `Command(PARTIAL_CLOSE, slave_ticket, vol_to_close)`. |
| `CLOSE` | If in `RecordTable` → `Command(CLOSE, slave_ticket)`, then drop the record. |

4. Slave worker executes each `Command` via `order_send` (with `retryCount`/`retryDelayMs`), returns `Ack(slave_ticket, fill_price, fill_vol)` (for OPEN) or confirmation.
5. Manager updates the slave's `RecordTable` with the returned slave ticket + fill volume.
6. Status panel updates (per-slave latency = snapshot round-trip time, last action, open-position count).

**Faithfulness points to the EA:**

- The diff is computed **once** from the master snapshot (shared across slaves); only the per-slave transform + record lookup is per-slave — efficient for one-master-to-many.
- `maxTradeAge` is a **per-slave** setting, so the baseline filter is applied per slave (one slave can copy a 5-minute-old trade while another ignores anything older than 1 minute).
- Partial-close uses the master's *open* volume vs *current* volume (stored in `RecordTable`), not a delta from the previous snapshot — robust against a missed intermediate snapshot.
- Heartbeat: if no master `Snapshot` within `heartbeatSeconds * 2`, the manager flags "no heartbeat from master" per slave (matches the EA).

**Concurrency:** the manager's copy loop runs in a single engine thread (events processed sequentially per snapshot — no per-slave race). Each slave worker is independent, so a slow/slaved broker on one slave does not block the others; the manager sends commands asynchronously and collects acks.

## Error handling and recovery

**Worker crash / subprocess exit.** The supervisor watches each worker's process handle. On unexpected exit it logs the error, marks that account "disconnected" in the status panel, and offers reconnect. Reconnect re-spawns the worker; the slave worker re-runs restart recovery and re-sends `RecoveryRecords`, so the manager's `RecordTable` is rebuilt — no duplicated trades.

**Manager restart / crash.** If the manager dies, workers lose their pipe and detect EOF → each worker calls `mt5.shutdown()` and self-terminates gracefully (no orphaned terminal connections). On manager relaunch, Start → workers spawn → recovery runs → copying resumes from current master state. Positions that closed on the master while the manager was down are detected as `CLOSE` on the first diff (if the slave still holds the position, it closes it); positions that opened are `NEW` (copied subject to `maxTradeAge`). Brief gap, no duplication.

**Terminal / connection failures:**

- `mt5.initialize` fails (bad credentials, terminal not installed, broker down) → worker reports `Error`; supervisor retries with backoff up to a cap, then surfaces "failed to connect" in the GUI.
- Mid-session terminal disconnect → worker reports it, supervisor attempts reconnect; meanwhile the master worker stops emitting snapshots so the heartbeat warning fires on the slave side (matches the EA).

**Trade execution failures (slave side):**

- `order_send` fails (requote, no quotes, off-market, insufficient margin) → slave worker retries `retryCount` times with `retryDelayMs` (matches the EA), then reports the final `Error` with the MT5 retcode. Manager logs it, shows it in the status panel, and **leaves the record in the table** so a later retry or a manual fix can reconcile (it will not be treated as a fresh NEW).
- Symbol missing on slave → skip the event + log (matches the EA `CSymbolMapper`).
- Lot size below `volume_min` / above cap → `LotSizer` clamps; if clamped to 0 (below min) → skip + log.

**Heartbeat.** No master `Snapshot` within `heartbeatSeconds * 2` → per-slave "no heartbeat from master" warning (logged once, shown in status). Resolves when snapshots resume.

**Credential errors.** DPAPI decrypt failure (e.g. settings copied to another machine or user) → manager prompts the user to re-enter credentials rather than silently failing.

**Tray / background.** Close-to-tray keeps the manager process + workers alive. Quitting from the tray menu does an orderly shutdown: stops the copy loop, sends shutdown to all workers (they `mt5.shutdown()`), waits briefly, then exits. Force-kill of the manager still leaves workers to self-terminate on pipe EOF (above).

**Not retried automatically:** a `CLOSE` that fails repeatedly is surfaced loudly — a slave holding a position the master already closed is a real risk to flag, not paper over. The manager never silently diverges; every skip/failure is visible in the status panel and log.

## Project structure

New top-level `manager/` Python package. The `MQL5/` tree stays as the prior implementation and logic reference; the `spike/` tree stays as the RE record.

```
manager/
  __main__.py            # entry point: launch GUI
  gui/
    main_window.py       # Master pane, Slave list, Start/Stop, status panel, log
    slave_editor.py      # per-slave config form + symbol-map table
    tray.py              # system-tray icon, close-to-tray, quit
  engine/
    snapshot_diff.py     # NEW/MODIFY/PARTIAL/CLOSE detection
    baseline.py          # EstablishBaseline + maxTradeAge filter
    record_table.py      # per-slave master->slave linkage state
    transform.py         # SymbolMapper, LotSizer, PriceNormalizer (ported from EA)
    linkage.py           # MAGIC_BASE + CPY#..|MV|SV encode/decode
    copy_loop.py         # the engine thread: snapshot -> events -> per-slave commands
  worker/
    mt5_worker.py        # role-parameterized master/slave worker subprocess
    mt5_adapter.py       # thin wrapper over the MetaTrader5 package (mockable)
  ipc/
    messages.py          # Snapshot/Command/Ack/Status/Error/RecoveryRecords schemas
    pipe_framing.py      # length-prefixed JSON framing over the Pipe
  settings/
    store.py             # config load/save (JSON)
    credentials.py       # DPAPI encrypt/decrypt via pywin32
  supervisor.py          # spawn/watch/restart workers, route IPC
  tests/
    test_snapshot_diff.py
    test_transform.py        # SymbolMapper/LotSizer/PriceNormalizer
    test_linkage.py
    test_record_table.py
    test_baseline.py
    test_copy_loop.py        # fake master+slave workers, no real terminal
    test_recovery.py
pyproject.toml           # PySide6, MetaTrader5, pywin32, pytest
```

## Tech stack

- **PySide6** (Qt6) — GUI + system tray.
- **MetaTrader5** — official package, terminal control (Windows-only).
- **pywin32** (`win32crypt`) — DPAPI credential encryption (`CryptProtectData` / `CryptUnprotectData`, per-user, OS-managed key).
- **multiprocessing** — worker subprocesses + Pipe IPC.
- **pytest** — unit + fake-worker integration tests.
- **Python 3.11+.**
- **Platform:** Windows-only.

**Testability property:** the `MetaTrader5` calls are isolated behind `mt5_adapter.py`'s interface, and the engine never touches the terminal — it only sees `Snapshot` messages and emits `Command` messages. The entire copy logic is therefore unit-testable and integration-testable with fake workers, no MT5 installed. The only thing requiring a real terminal is the final manual demo smoke test.

## Testing

Three tiers, bottom-up.

**1. Unit tests (pytest, no terminal, no GUI):** the ported logic, fully deterministic.

- `test_snapshot_diff.py` — feed two consecutive snapshots, assert the right NEW/MODIFY/PARTIAL/CLOSE events (including SL-only change, TP-only change, volume decrease, ticket disappearance, no-op when identical).
- `test_transform.py` — `SymbolMapper` (explicit map, same-name fallback, missing → skip), `LotSizer` (balance-step formula, lot-step rounding down, min/max + maxLot cap, sub-min → 0), `PriceNormalizer` (raw-distance reproduction BUY/SELL, tick-size rounding, decimals differ between brokers).
- `test_linkage.py` — `MAGIC_BASE + ticket % 900000` encode/decode round-trip; `CPY#..|MV|SV` comment parse, including malformed comment.
- `test_record_table.py` — record add/lookup/drop; partial-close bookkeeping (master open volume vs current volume).
- `test_baseline.py` — `EstablishBaseline` marks old positions seen (skip) and lets recent ones through, per `maxTradeAge`.

**2. Integration test (pytest, no real terminal):** a fake master worker that replays a scripted sequence of snapshots and a fake slave worker that records the `Command`s it receives (no `order_send`), driven through the *real* `copy_loop` + IPC framing. Asserts the full event → transform → command flow end-to-end: a scripted master open/modify/partial/close produces the expected OPEN/MODIFY/PARTIAL_CLOSE/CLOSE commands with correct lots, SL/TP, and slave-ticket linkage. Also covers restart recovery (fake slave reports `RecoveryRecords` → manager seeds `RecordTable` → no duplicate OPEN) and the heartbeat warning (no snapshot for `heartbeatSeconds * 2` → warning fires).

**3. Manual demo smoke test (real demo terminals, documented in docs/):** the real-world validation, on demo accounts only for the test itself.

- Same-PC: master demo terminal + slave demo terminal. Open / modify SL-TP / partial-close / full-close on the master → verify the slave mirrors each, with correct lot sizing and normalized SL/TP.
- Multi-slave: one master → two slaves with *different* symbol maps and lot steps → verify each transforms independently.
- Restart mid-copy: kill the manager, relaunch, Start → verify recovery rebuilds state and no duplicate trades; verify a master close during the gap closes the slave position.
- Tray: minimize to tray, confirm copying continues; restore, confirm live status.
- Disconnect: stop the master terminal → heartbeat warning fires; restart it → copying resumes.

**Definition of done for the spec:** tiers 1 + 2 green in the repo; tier 3 documented as a runbook the user executes against their own demo accounts (a real terminal cannot be run in this build environment).

## Settings model

Per-account and global config persisted as JSON under `%APPDATA%\CopyTradesMT5\`:

- `global.json` — `masterIntervalMs`, `slavePollMs` (if distinct), `heartbeatSeconds`, default retry/heartbeat values.
- `master.json` — terminal path, login, server (password stored DPAPI-encrypted in a separate `credentials.bin`).
- `slave_<login>.json` — terminal path, login, server, symbol map (`master=slave` pairs), `balanceStepAmount`, `balanceStepSize`, `maxLot`, `maxTradeAgeMinutes`, `normalizeSLTP`, `retryCount`, `retryDelayMs`.
- `credentials.bin` — DPAPI-encrypted blob holding all account passwords.

Settings fields mirror the EA's `SCopierSettings` so configuration knowledge transfers.

## Open items deferred to the implementation plan

- Exact PySide6 widget layout / sizing of the main window and slave editor (implementation detail, not architecture).
- IPC message field-level schemas (fleshed out in `ipc/messages.py` during implementation; the types are fixed here).
- Reconnect backoff constants and retry cap values (sensible defaults chosen during implementation).
- Whether `slavePollMs` is a separate knob or derives from `masterIntervalMs` (default: the engine is driven by master snapshots, so a separate slave poll is unnecessary — resolved during implementation).