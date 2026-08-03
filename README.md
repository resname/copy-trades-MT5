# Copy Trades MT5 — Local Manager

A standalone **Windows desktop app** (PySide6/Qt) that copies trades from one
**master** MetaTrader 5 account to one or more **slave** accounts. It drives MT5
terminals through the official `MetaTrader5` Python package — no Expert Advisor,
no DLL imports, no manual chart attachment.

```
1 master  ──►  manager process (GUI + engine)
                    │
                    ├──► worker subprocess  ──► master MT5 terminal
                    ├──► worker subprocess  ──► slave MT5 terminal #1
                    ├──► worker subprocess  ──► slave MT5 terminal #2
                    └──► …
```

The manager process runs the GUI and the copy engine. Each terminal connection
lives in its own worker subprocess (single connection per process), so a crash
or restart in one terminal never takes down the others. The supervisor restarts
dead workers with exponential backoff and kills the stale `terminal64.exe` before
a respawn so `mt5.initialize` does not hit the `-10003` IPC-collision error.

---

## Features

- **1 master → many slaves**, each mirrored independently.
- **Positions only** — mirrors opens, modifies, partial closes, and full closes
  (matches the EA's behavior; no pending-order copying).
- **Recent + forward** — on start, copies master positions opened within the
  configurable max trade age, then mirrors new activity going forward.
- **EA-faithful slave normalization** — the manager sends raw master SL/TP, the
  master open price, and the side; each slave normalizes SL/TP and rounds to the
  symbol's tick size, and computes partial-close volume from its own live
  position (so a partial close on the master closes the same fraction on the
  slave regardless of lot differences).
- **Symbol mapping** — trade `US30` on the master → open `WS30` on a slave.
- **Lot sizing** per slave — configurable balance step amount/size and a max
  lot cap; volumes rounded down to the symbol's lot step.
- **Auto-find MT5 instances** — discovers installed terminals via `origin.txt`
  and the default Program Files locations.
- **Auto-install shortfall instances** — provisions extra portable terminals
  via `mt5setup.exe /auto` when there are fewer instances than accounts (one
  terminal per account).
- **System tray** — close-to-tray keeps the workers running in the background;
  tray Quit does an orderly shutdown (stops the engine, joins workers, then
  quits the app).
- **Restart recovery** — does not duplicate copied positions already open on a
  slave (positions are matched by their `CPY#<ticket>|MV..|SV..` comment).

---

## Security model

- **Demo accounts first.** The manual smoke runbook (`docs/smoke-test.md`) is
  demo-only. Production supports real accounts, but never defaults to one and
  never logs credentials.
- **Credentials are passed to workers through the pipe**, never on the command
  line, so they do not appear in the process list (`tasklist` / Process Explorer).
- **Credentials at rest are DPAPI-encrypted** (`pywin32 win32crypt`
  `CryptProtectData` / `CryptUnprotectData`, per-user OS-managed key) — the
  settings file holds opaque blobs, not plaintext passwords.
- Capture artifacts from protocol research (pcaps, Frida logs) are large and
  may contain credentials; they are gitignored and never committed.

---

## Requirements

- **Windows 10/11** (MetaTrader 5 is Windows-only; DPAPI via `pywin32`).
- **Python ≥ 3.11**.
- **MetaTrader 5** terminals installed (one per account). The manager can
  auto-discover existing installs and auto-provision the shortfall.
- Python dependencies (installed via `pip install -e .`):
  - `PySide6>=6.6` — GUI + system tray
  - `pywin32>=306` — DPAPI credential encryption
  - `psutil>=5.9` — terminal process discovery / kill
  - `MetaTrader5>=5.0.45` — the official MT5 Python integration

---

## Installation

```powershell
git clone https://github.com/resname/copy-trades-MT5.git
cd copy-trades-MT5
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

`pip install -e .` installs the `manager` package and its dependencies.

---

## Usage

```powershell
python -m manager
```

1. **Master account**: enter the master login, server, and (demo) password, and
   pick a terminal (or let the manager assign one). Click **Start**.
2. **Slave accounts**: click **Add slave** to open the slave editor — enter each
   slave's login, server, (demo) password, optional terminal override, and
   per-slave symbol map / lot-sizing / normalization options. Add as many slaves
   as you need (one terminal each; the manager provisions the shortfall).
3. **Start**: the manager prepares terminals (discovering/provisioning as
   needed), spawns one worker per slave, waits for every slave to report ready
   (SymbolInfo + first status), then spawns the master worker and starts the
   copy loop. The status panel shows `slaves ready; starting master`.
4. **Tray**: closing the window hides it to the tray — workers keep running.
   Use the tray **Quit** for an orderly shutdown (stop engine → join workers →
   quit).

For a full manual demo run-through (demo accounts only), see
[`docs/smoke-test.md`](docs/smoke-test.md).

---

## File layout

```
manager/
  __main__.py            App entry: QApplication + window + tray + status bridge
  app/
    controller.py        CopyController: terminal mgmt + readiness gate + creds
  engine/                The copy engine (master→slave mirroring logic)
    copy_loop.py         CopyEngine: snapshots → per-slave command queues
    baseline.py          Recent-opens backfill at start
    linkage.py           CPY# ticket-linking comment encoding
    snapshot_diff.py     Master snapshot → position events
    transform.py         Master event → slave command (normalize, lot size)
    models.py            Snapshot / Position / command dataclasses
    record_table.py      Per-slave copied-position ledger
  ipc/
    messages.py          IPC message types (Start/Ack/Status/Snapshot/…)
    pipe_framing.py      Length-prefixed pipe framing
  worker/
    mt5_worker.py        Worker subprocess entry (one per terminal)
    mt5_adapter.py       RealMt5 (MetaTrader5) + FakeMt5 (tests)
    mt5_constants.py
  supervisor.py          Worker lifecycle, restart+backoff, readiness gate
  terminal/
    discovery.py          Find installed MT5 terminals (origin.txt + defaults)
    provisioning.py      Install portable terminals via mt5setup.exe /auto
    manager.py           Assign one terminal per account; kill stale terminal64.exe
  settings/
    credentials.py       DPAPI encrypt/decrypt (pywin32 win32crypt)
    store.py             Atomic JSON settings + provisioned-instance registry
  gui/
    main_window.py       Main window (master form, slave list, status/log)
    slave_editor.py      Add/edit slave account dialog
    tray.py              System tray (close-to-tray + orderly quit)
  tests/                 pytest suite (175 tests)
docs/
  smoke-test.md          Manual demo smoke runbook
  TESTING.md             How to run the test suite
```

---

## Testing

The non-GUI logic is fully unit-tested with `pytest`; the GUI tests use
`pytest.importorskip("PySide6")` so they skip cleanly when PySide6 is not
installed and run on a PySide6-enabled host.

```powershell
pytest -q
# expected on a headless env: 175 passed, 4 skipped
```

See [`docs/TESTING.md`](docs/TESTING.md) for the suite layout and how to run
individual test modules.

---

## Architecture notes

- **Engine purity** — `manager/app/controller.py` and the `engine/` package
  import zero Qt, so the copy logic is testable without a GUI.
- **Cross-thread marshaling** — the supervisor runs on a daemon thread;
  `__main__._StatusBridge` emits Qt signals so status/log updates are delivered
  to the GUI thread via Qt's queued connection.
- **Readiness gate** — the supervisor spawns all slaves first and waits for each
  to report SymbolInfo + first status before spawning the master, avoiding the
  startup race where a master snapshot arrives before slaves are ready to act.
- **One terminal per account** — the MT5 Python package allows one `initialize`
  per process, so each account gets its own worker subprocess and its own
  terminal instance (provisioned automatically if there are not enough).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `mt5.initialize` returns `False` / `-10003` | A stale `terminal64.exe` is holding the terminal's IPC | The supervisor kills the stale terminal before respawn; restart the manager if it persists |
| Slaves never reach `ready` | Worker failed to log in or fetch SymbolInfo | Check the log view for the worker error; confirm the demo login/server are correct |
| `CredentialDecryptError` on start | Settings blob was encrypted by a different Windows user | Re-enter the password (DPAPI keys are per-user) |
| Not enough terminal instances | Fewer installed terminals than accounts | Let the manager provision the shortfall, or point accounts at specific terminals via the terminal override |

---

## Quick dev loop

```bash
pytest -q                       # run the suite
python -m manager               # launch the app
git add -A
git commit -m "fix: ..." -m "Co-Authored-By: Claude <noreply@anthropic.com>"
git push origin main
```