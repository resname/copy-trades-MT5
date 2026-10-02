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

## Quick Start

1. **Install & launch** (end users):
   ```powershell
   irm https://github.com/resname/copy-trades-MT5/releases/latest/download/install.ps1 | iex
   ```
   Then launch **CopyTrades MT5** from the Start Menu (or run `copytrades`). From
   source: `pip install -e .[test]` then `python -m manager` — see
   [Installation](#installation).
2. **Install/log in to terminals**: one MetaTrader 5 terminal per account, each
   logged in to the account it will trade. Use the manager's
   **Install MetaTrader** button if you need more (choose a custom install path
   per terminal).
3. **Enable Algo Trading on every terminal** ⚠️ — in each MetaTrader terminal,
   click the **Algo Trading** toolbar button so it is ON (or Tools → Options →
   Expert Advisors → *Allow algorithmic trading*). The copier places slave trades
   through the MT5 Python API; if Algo Trading is off, `order_send` is blocked and
   **nothing copies**. The manager refuses to Start until every terminal reports
   Algo Trading enabled.
4. **Select the master terminal** in the manager and **Add Slave…** for each
   slave (per-slave terminal, symbol map, lot sizing, normalization).
5. **Click Start** — the manager connects to every terminal, gates on readiness
   and Algo Trading, then copies opens/modifies/partial-closes/closes from master
   to slaves.
6. Close the window for an orderly stop — the engine stops and the app exits.
   The app auto-checks for updates hourly.

For the full run-through, see [Usage](#usage). For a manual run-through, see
[`docs/smoke-test.md`](docs/smoke-test.md).

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
  Master entries can be **regular expressions** with optional `$1`–`$9`
  group substitution in the slave name (see
  [Symbol map: regex support](#symbol-map-regex-support)).
- **Lot sizing** per slave — choose a mode per slave:
  - **Balance step (lots step)** — `floor(slave_balance / step_amount) * step_size`,
    rounded down to the symbol's lot step, clamped to its min/max. Optionally set
    a **Master base lot size** (the master's usual lot, e.g. 0.1): when a specific
    master trade is *smaller* than the base, the slave opens a proportionally
    smaller position (still snapped to lot steps); larger trades are not scaled up.
  - **Copy master lot** — the slave mirrors the master's lot per trade (snapped to
    the symbol's lot step, clamped to its min/max).
  - **Fixed lot** — the slave opens one configured lot size for every trade.
  All modes cap at the per-slave **max lot**.
- **Auto-find MT5 instances** — discovers installed terminals via `origin.txt`
  and the default Program Files locations.
- **Manual-login, terminal-path-only setup** — you log in to each MT5 terminal
  via its own UI, then select only the terminal path in the
  manager; the manager never sees or stores credentials.
- **Install MetaTrader / Open terminal for login buttons** — open the MT5
  download page to install another terminal (custom install path per
  terminal), or open a selected terminal's `terminal64.exe` login window to
  log in/verify. Install is on the left; Open-for-login is on the right.
- **Single-window close = stop** — closing the window stops the engine (joins
  workers) and exits the app; no system tray, no background-running mode.
- **Auto-start on boot + auto-copy** — optionally launch the app at Windows
  login and auto-start copying 15 s after launch (one click to cancel). If the
  terminals aren't ready, it fails silently (logged, no modal).
- **Restart recovery** — does not duplicate copied positions already open on a
  slave (positions are matched by their `CPY#<ticket>|MV..|SV..` comment).
- **Persistent config** — the master terminal + slaves (with per-slave symbol
  map / lot-sizing / normalization) are saved to `settings.json` and restored
  on the next launch, so a restart (or an update) does not lose your setup.

---

## Security model

- **No credentials are stored, piped, or logged by the manager.** There is no
  DPAPI store, no password in the settings file, no password on the worker pipe
  or command line. The selected terminal path is the account identity; the
  worker connects to the terminal's saved account.
- Capture artifacts from protocol research (pcaps, Frida logs) are large and
  may contain credentials; they are gitignored and never committed.

---

## Requirements

- **Windows 10/11** (MetaTrader 5 is Windows-only).
- **Python ≥ 3.11, x86_64 build.** The `MetaTrader5` package ships only
  `win_amd64` wheels, so the manager needs an x64 Python. The one-liner
  installer enforces this: it skips the Microsoft Store Python (sandboxed;
  redirects venv writes) and any native ARM64 Python, installs x64 Python
  (via winget, with a python.org fallback), and on ARM64 Windows runs that
  x64 Python under emulation. On a plain x64 machine this is all automatic
  and invisible.
- **MetaTrader 5** terminals installed and logged in to (one per account).
  The manager discovers existing installs; install extras
  via the **Install MetaTrader** button (custom install path per terminal).
- Python dependencies (installed via `pip install -e .`):
  - `PySide6>=6.6` — GUI
  - `psutil>=5.9` — terminal process discovery / kill
  - `MetaTrader5>=5.0.45` — the official MT5 Python integration

---

## Installation

### One-liner (recommended — end users)

```powershell
irm https://github.com/resname/copy-trades-MT5/releases/latest/download/install.ps1 | iex
```

This installs everything: Python (via winget, with a python.org fallback if
winget is unavailable), a private venv, the latest manager wheel (SHA256
verified), a `copytrades` command on your PATH, and a Start Menu shortcut.
Re-running the same one-liner **updates** to the newest release and relaunches
the app. No `git`, no build tools, no manual dependency handling — just Windows.

The installer is committed to the repo (`scripts/install.ps1`), versioned per
release, and served over HTTPS from GitHub Releases — the same model as the
Claude Code installer.

### From source (development)

```powershell
git clone https://github.com/resname/copy-trades-MT5.git
cd copy-trades-MT5
python -m venv .venv
.venv\Scripts\activate
pip install -e .[test]
```

`pip install -e .[test]` installs the `manager` package, its runtime
dependencies, and `pytest` (the `[test]` extra).

---

## Usage

```powershell
copytrades          # if installed via the one-liner (on PATH)
python -m manager   # from a dev checkout / venv
```

The app also checks for updates automatically (on launch, then hourly) and
shows an **Update available** indicator with an **Update & restart** button
(enabled only while the copy engine is idle). `copytrades update` runs the
same update from the command line.

1. **Install terminals** (if you don't have enough): click **Install MetaTrader**
   to open the download page, download and run `mt5setup.exe`, and choose a
   **custom install path** for each terminal (the default path collides with
   existing terminals). Install one terminal per account.
2. **Log in to each terminal**: click **Launch terminal** to open a terminal's
   login window (or open it yourself), and log in to the account it will
   trade. The terminal saves the account.
3. **Master**: in the manager, select the master terminal from the dropdown.
   Click **Start** (the manager connects to that terminal's saved account —
   no login/server/password entered in the manager). **Algo Trading must be
   enabled on every terminal** (see Quick Start step 3) or Start is blocked.
4. **Slaves**: click **Add slave** to open the slave editor — select each
   slave's terminal, and set the per-slave symbol map / lot-sizing mode
   (balance step with optional master base lot, copy master lot, or fixed lot) /
   normalization options. Add as many slaves as you need (one terminal each).
5. **Start**: the manager assigns terminals, spawns one worker per slave,
   waits for every slave to report ready (SymbolInfo + first status), then
   spawns the master worker and starts the copy loop. The status panel shows
   `slaves ready; starting master`.
6. **Close**: closing the window stops the engine (joins workers) and exits
   the app — there is no tray. Minimize the window to keep it running in the
   background.
7. **Auto-start (optional)**: in the **Auto-start** group, tick **Launch on
   Windows startup** to start the app at login (a shortcut in `shell:startup`),
   and/or **Auto-start copying on launch** to begin copying 15 s after the app
   opens — a **Cancel** button appears during the countdown. Auto-start fails
   silently (a log line) if no master/slaves are configured or Algo Trading is
   off; it never shows a modal, so an unattended reboot is not blocked.
8. **Updates**: the app checks for updates hourly and pre-downloads the
   verified wheel when one is found, so clicking **Update & restart** finishes
   in seconds (no network in the restart path) and reliably relaunches the
   manager.

For a full manual run-through, see
[`docs/smoke-test.md`](docs/smoke-test.md).

---

## Symbol map: regex support

Each row of the slave editor's symbol map is `master=slave`. The master
side may be a **regular expression**; the slave side may reference the
pattern's captured groups with `$1`–`$9` (`$$` = a literal `$`).

### How a master symbol is resolved

Rows are checked in this order:

1. **Exact match** — a row whose master entry equals the symbol
   character-for-character always wins first, so old maps behave exactly as
   before.
2. **Regex rows, top to bottom** — each master entry is tried as a pattern
   with a *full-match* rule: the entire master symbol must match the pattern.
   The first row that matches wins; the slave entry has `$1`–`$9` substituted
   with the captured groups.
3. **Same-name fallback** — if no row matched, the master symbol itself is
   used when it also exists on the slave terminal.

A row that cannot work as a regex falls back to exact matching only — an
entry that fails to compile (e.g. an unclosed `(`), or a slave entry
referencing a group the pattern never captured (e.g. `AB=$1` on a pattern
with no `(...)`), simply behaves like a plain literal pair. Nothing crashes,
no existing config changes meaning.

For regex rows the slave symbol is only known once a master symbol arrives,
so its lot-step/tick-size info is fetched on demand: the first trade on such
a symbol is held for a moment while the manager asks the slave terminal,
then copied — you may see the copy land a second or two after the master
trade. If the slave terminal answers that the derived symbol doesn't exist,
the trade is skipped and the symbol is remembered as missing.

### Regex syntax

Python `re` patterns are supported. The pieces that matter for symbol names:

| Syntax | Meaning | Example pattern | Matches |
|---|---|---|---|
| `.` | any single character | `US30.` | `US30.a`, `US30x` |
| `*` | 0+ of the previous item | `ABC.*` | `ABC`, `ABC2026` |
| `+` | 1+ of the previous item | `ABC\d+` | `ABC2026` |
| `?` | 0/1 of the previous item | `US30.?` | `US30`, `US30A` |
| `\d` `\w` `\s` | digit / word char / space | `ABC\d+` | `ABC2026` |
| `[...]` | one char from a set | `US[35]0` | `US30`, `US50` |
| `(...)` | capture group (usable as `$1`) | `(.+)\.m` | `EURUSD.m` (captures `EURUSD`) |
| `a\|b` | either alternative | `US30\|US_30` | `US30` or `US_30` |
| `\.` `\|` etc. | literal `.` \| etc. | `US30\.a` | `US30.a` exactly |

Matching is **case-sensitive** and requires the **whole** symbol to match
(`fullmatch`, not substring search): `ABC` alone does not match `ABC2026` —
write `ABC.*` or `ABC\d+`.

### Examples

| Map row | Effect |
|---|---|
| `US_30=WS30` | Plain literal row (unchanged behavior): master `US_30` → slave `WS30`. |
| `ABC\d+=US30` | Any `ABC` + digits symbol (`ABC2026`, `ABC9`) fans in to one fixed slave symbol `US30`. |
| `US30.*=WS30` | `US30`, `US30.a`, `US30.pro` all → `WS30`. |
| `(.+)\.m=$1` | Master `EURUSD.m` → slave `EURUSD`; one row handles every `.m`-suffixed symbol. |
| `US(\d+)#US$1` | Master `US30` → slave `#US30`; `USTEC` → `#USTEC`. |
| `US30\|US_30=WS30` | Either master spelling → `WS30`. |
| `ABC(=US30` | Invalid regex → falls back to an exact pair: only the literal symbol `ABC(` maps to `US30`. |

Limitations: the row separator is the first `=` — a literal `=` **inside**
the pattern can't be expressed (write it as `\x3d`, e.g. `US30\x3dA=WS30`),
and `,` separates rows, so a literal comma in a pattern must be written
`\x2c`.

---

## File layout

```
manager/
  __main__.py            App entry: QApplication + window + status bridge
  _version.py            Single source of truth for the installed version (CI overwrites at build)
  updater.py             Headless update check + detached self-update spawn (no Qt)
  app/
    controller.py        CopyController: terminal mgmt + readiness gate
  engine/                The copy engine (master→slave mirroring logic)
    copy_loop.py         CopyEngine: snapshots → per-slave command queues
    baseline.py          Recent-opens backfill at start
    linkage.py           CPY# ticket-linking comment encoding
    snapshot_diff.py     Master snapshot → position events
    transform.py          Master event → slave command (normalize, lot size)
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
    manager.py           Assign one terminal per account; kill stale terminal64.exe
  settings/
    store.py             Atomic JSON settings + provisioned-instance registry
  gui/
    main_window.py       Main window (master terminal form + Launch/Install buttons, slave list, status/log, update UI)
    slave_editor.py      Add/edit slave account dialog
  tests/                 pytest suite (291 headless / 357 with PySide6)
scripts/
  install.ps1            One-liner installer/updater (winget-first Python, venv, SHA256-verified wheel)
  smoke-install.ps1      Local install.ps1 smoke check
.github/workflows/
  release.yml            Build wheel + publish GitHub Release (auto on push to main)
docs/
  smoke-test.md          Manual smoke runbook
  TESTING.md             How to run the test suite
```

---

## Testing

The non-GUI logic is fully unit-tested with `pytest`; the GUI tests use
`pytest.importorskip("PySide6")` so they skip cleanly when PySide6 is not
installed and run on a PySide6-enabled host.

```powershell
python -m pytest manager/tests -q
# headless (no PySide6): 291 passed, 5 GUI modules skipped
# on a host with PySide6 installed: 357 passed
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
  terminal instance (installed manually via the Install MetaTrader button if there are not enough).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Could not find a version that satisfies the requirement MetaTrader5` (`from versions: none`) | An ARM64 or Microsoft Store Python is in the venv — `MetaTrader5` has only `win_amd64` wheels | Re-run the one-liner; the installer skips Store/ARM64 Pythons and installs x64. If installing manually, use an x64 (`win-amd64`) Python |
| `mt5.initialize` returns `False` / `-10003` | A stale `terminal64.exe` is holding the terminal's IPC | The supervisor kills the stale terminal before respawn; restart the manager if it persists |
| Slaves never reach `ready` | Worker failed to log in or fetch SymbolInfo | Check the log view for the worker error; confirm the terminal is logged in via the terminal's own UI / the Launch button (the manager does not enter credentials) |
| Not enough terminal instances | Fewer installed terminals than accounts | Install more via the Install MetaTrader button (custom path) and log in, or point accounts at specific terminals via the dropdown |
| Update & restart closes the app but it doesn't reopen | The detached helper's pip install or relaunch step failed | Open `%LOCALAPPDATA%\CopyTradesMT5\updates\update.log` for the step that failed; re-run `copytrades update` or the one-liner installer |
| Start blocked: "Algo Trading is disabled on: …" / trades don't copy | The Algo Trading toolbar button is off on one or more terminals (the MT5 Python API's `order_send` is blocked) | Enable the **Algo Trading** button in each named terminal (or Tools → Options → Expert Advisors → Allow algorithmic trading), then click Start again |

---

## Quick dev loop

```bash
pytest -q                       # run the suite
python -m manager               # launch the app
git add -A
git commit -m "fix: ..." -m "Co-Authored-By: Claude <noreply@anthropic.com>"
git push origin main
```