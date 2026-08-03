# Manual-Login + Terminal-Path-Only Selection — Design

**Date:** 2026-08-03
**Status:** Design (pre-implementation)
**Replaces:** the broker/server-browser feature (PR #11/#12, merged) and the
DPAPI credential flow.

## Goal

Replace the in-manager broker/server discovery + credential entry with a
**manual-login, terminal-path-only** workflow: the user manually logs in to
each MT5 terminal install via the terminal's own native UI, then in the
manager selects only that terminal from a dropdown. The manager connects
using the terminal's saved account — it never sees broker, server, login,
or password.

## Background / Problem

The broker/server browser ("find the correct brokers at login") does not
reliably surface the right server for the user's terminals, so Start fails
at `mt5.initialize`. The user proposed: skip broker/server discovery and
credential entry entirely — log in to each terminal by hand, then pick the
terminal path. This is technically sound: `mt5.initialize(path=<terminal>)`
with **no** `login`/`server`/`password` connects to that terminal's
last-saved account. It is also a strict reduction in credential exposure.

## Decisions (from brainstorming)

1. **Terminal-path-only; remove credentials entirely.** No login/server/
   password in `AccountSpec`, no DPAPI store, no pipe-borne password. The
   selected terminal path *is* the account identity.
2. **Pre-installed terminals only, picked from the dropdown.** No
   auto-provisioning (auto-install) at Start. The user brings terminals they
   have already installed and logged in to.
3. **Add an "Install MetaTrader" button** that opens the official MT5
   download page in the browser, with a **disclaimer** that a **custom
   install path** must be used for each terminal (the default path collides
   with existing terminals and is not separately discoverable). The user
   downloads and runs `mt5setup.exe` and clicks through its wizard choosing
   a custom path.
4. **Add a "Launch terminal" button** next to the terminal dropdown that
   runs `terminal64.exe` for the selected terminal, opening its native
   login window so the user can log in / verify the connection from inside
   the manager. This directly answers the original question ("Is there any
   way to launch the login window from MT5?").
5. **Clean removal (Approach A).** Delete the now-dead broker catalog,
   server picker, credentials, and auto-provision machinery rather than
   leaving it dormant.
6. **Drop the `password` field wholesale** from `StartMsg`,
   `WorkerHandle`, and the supervisor spawn signatures (not kept as `""`).
7. **Drop the auto-provision path entirely** (`provision_shortfall`,
   `provision_instance`, `provisioning.py`).

## Architecture / Data Flow

```
User (manual)                Manager                          MT5 terminal
  |                             |                                 |
  |── logs in to terminal64.exe ─────────────────────────────►   |
  |   (terminal saves account)                                    |
  |                             |                                 |
  |── picks terminal in dropdown                                  |
  |── clicks Start             |                                 |
  |                CopyController.start(master, slaves)           |
  |                  prepare()  ── validate/dedup terminal paths |
  |                  build_worker_configs() ── path + portable    |
  |                  supervisor.spawn_*(config)  ─── StartMsg(config)
  |                                                                  |
  |                                           worker_main: adapter.initialize(path, portable)
  |                                           mt5.initialize(path=..., portable=...)  ──► saved account
```

No credentials cross any boundary. The worker connects to the terminal's
last-saved account. `account_info()` still reports `login`/`server` for
status display, but the manager never supplies them.

## Component Changes

### Data model — `AccountSpec` (`manager/app/controller.py`)

Drop `login`, `server`, `password`. New shape:

```python
@dataclass
class AccountSpec:
    id: str
    terminal_path: str            # required (was optional override)
    symbol_map_csv: str = ""
    step_amount: float = 100.0
    step_size: float = 0.01
    max_lot: float = 10.0
    max_trade_age_minutes: float = 10.0
    normalize_sltp: bool = True
```

`terminal_path` moves from optional override to the **primary, required**
field — it *is* the account. `prepare`/`assign` already dedup terminal paths
and raise `ControllerError` on duplicates; that becomes the core validation
(two accounts cannot share a terminal).

### Adapter — `manager/worker/mt5_adapter.py`

`Mt5Adapter.initialize` Protocol signature and both impls make credentials
optional:

```python
def initialize(self, path: str, login: int | None = None,
                password: str | None = None, server: str | None = None,
                portable: bool = False) -> bool: ...
```

`RealMt5.initialize` builds `mt5.initialize` kwargs conditionally — when
`login is None`, call `mt5.initialize(path=path, portable=portable)` only
(saved-account connect). The GUI/worker never pass credentials, but the
credential path is retained so the `FakeMt5` contract and any future use
stay valid. `FakeMt5.initialize` accepts the same optional signature and
ignores the credentials (it already returned `True` regardless).

### Worker — `manager/worker/mt5_worker.py`

`worker_main` no longer reads a password and no longer passes credentials:

```python
config = start.config
...
ok = adapter.initialize(config["terminal_path"],
                        portable=bool(config.get("portable", False)))
```

Docstring updated: StartMsg now carries config only (no password).

### IPC — `manager/ipc/messages.py`

`StartMsg` drops `password`:

```python
@dataclass(frozen=True)
class StartMsg:
    config: dict
    KIND = "start"
```

`encode`/`decode` `"start"` branches drop the `password` field. This is an
IPC-format change; it is safe because supervisor and worker are always
restarted together (no long-lived pipe spans a version).

### Supervisor — `manager/supervisor.py`

- `spawn_master`/`spawn_slave`/`_spawn` drop the `password` parameter.
- `WorkerHandle` drops the `password` field.
- `_spawn` sends `StartMsg(config=config)` (no password).
- `_restart` calls `_spawn(name, h.role, h.config, h.adapter_kind, h.fake_state)`.
- Remove `on_status_msg` (the learned-server hook) and its two fire sites in
  `_dispatch_slave` and `_read_master`. `StatusMsg` is still imported and
  used for `isinstance` dispatch — keep the import.

### Controller — `manager/app/controller.py`

- `AccountSpec` as above.
- `_account_dict` slims to `{"id": a.id, "terminal_path": a.terminal_path}`
  (the tuning fields were never read by `assign`; `build_worker_configs`
  reads from `AccountSpec` directly).
- `build_worker_configs` drops `login`/`server` from the config dicts and
  drops `portable` (the worker defaults `portable` to `False`; discovered
  instances are non-portable now that provisioning is gone). Master config:
  `{"terminal_path": m_inst.exe_path, "master_interval_ms": 1000}`. Slave
  config: `{"slave_id": ..., "terminal_path": s_inst.exe_path,
  "symbol_map_csv": ..., "normalize_sltp": ..., "retry_count": 3,
  "retry_delay_ms": 500, "slave_status_interval_ms": 5000}`.
- `start` calls `sup.spawn_slave(s.id, cfgs[s.id], adapter_kind=...,
  fake_state=...)` and `sup.spawn_master(mcfg, adapter_kind=...,
  fake_state=...)` — no password argument.
- `prepare` drops the `provision_shortfall` call (no auto-provision). It
  keeps the normalize/dedup-override loop and `assign`. Status line
  "checking terminal instances…" stays; the "provisioned N instance(s)"
  log line is removed.
- **Delete:** `_cache_path`, `get_catalog`, `_build_catalog`,
  `refresh_brokers`, `_recorded_servers`, `_on_worker_status`,
  `load_password`, `save_password`, the `brokers`/`catalog` imports, and
  the `credentials=_credentials_mod` constructor parameter + import.
- `build_supervisor` drops the `sup.on_status_msg = self._on_worker_status`
  wiring.

### Terminal manager — `manager/terminal/manager.py`

- Delete `provision_shortfall`, `required_count`, and the
  `_provision_fn`/`_download_fn` constructor params + the
  `TerminalManagerError` "not enough instances" path (no longer reachable —
  every account carries an explicit terminal path, so `assign` never hits
  the empty-pool branch). Keep `discover_all`, `assign`, `kill_terminal`.
- `assign` is unchanged in logic but its docstring/empty-pool raise are now
  defensive-only (all accounts have `terminal_path`).

### Settings store — `manager/settings/store.py`

- Remove the `learned_servers` setdefault in `load` (the `learned` module
  is deleted).
- Keep the `provisioned_instances` plumbing (`setdefault`,
  `list_provisioned_instances`): `discover_all` still merges it so
  terminals a user provisioned under prior versions remain discoverable.
  `add_provisioned_instance`/`remove_provisioned_instance` become unused
  but are kept (low-cost, harmless) to avoid widening the diff into the
  store's persistence tests. (Ruling: keep, ACCEPT — optional future
  cleanup.)
- Update the class docstring: drop the "password blobs" mention.

### GUI — `manager/gui/main_window.py`

Master form:
- Remove the `master_login` (`QLineEdit`), `master_picker`
  (`BrokerServerPicker`), and `master_password` (`QLineEdit`) rows and
  their imports.
- Keep `master_terminal` (`QComboBox`, editable, auto-populated by
  `_populate_terminals`). It is now the **primary, required** field.
- Add a **"Launch terminal"** button next to the terminal dropdown: runs
  `subprocess.Popen([terminal64_exe])` for the selected terminal's exe
  path (validated non-empty), opening its login window.
- Add an **"Install MetaTrader"** button + a **disclaimer `QLabel`**:
  *"Install MetaTrader opens the download page. Download and run
  mt5setup.exe, and choose a custom install path for each terminal — the
  default path collides with existing terminals."* The button calls
  `webbrowser.open(SETUP_DOWNLOAD_URL)` (see Install-button mechanism
  below).

`_on_start`:
- Build `AccountSpec(id="master", terminal_path=...)` — no
  login/server/password.
- Validate the master terminal is non-empty (log + return if blank).
- The existing try/except around `start` stays.

Slave list label: `f"{spec.id}: {label}"` where `label` is the install-dir
basename (last segment of the terminal path), since there is no
login/server to display.

### GUI — `manager/gui/slave_editor.py`

- Remove the `login`, `_picker` (`BrokerServerPicker`), and `password`
  rows + the `BrokerServerPicker` import.
- Keep `terminal` (`QComboBox`), now required.
- Add a **"Launch terminal"** button next to the terminal dropdown (same
  behavior as the master form).
- `_spec_from_fields`/`spec()` build `AccountSpec(id, terminal_path, ...)`
  — no login/server/password. `terminal_path` is the combo's current text
  (strip); validation that it is non-empty is enforced at Start via the
  controller's dedup/`assign`, and the editor can warn if blank on OK
  (optional; the controller is the authority).

### Install-button mechanism (flagged for review)

`provisioning.py`'s `SETUP_DOWNLOAD_URL` is the MT5 *download page*
(`https://www.metatrader5.com/en/download`), not a direct binary link —
`urllib.urlretrieve` on it would fetch HTML, not a runnable `mt5setup.exe`.
So a robust Install button **opens that page in the default browser**
(`webbrowser.open`); the user downloads and runs `mt5setup.exe` and clicks
through its wizard choosing a custom install path (per the disclaimer).
This lets us **delete `provisioning.py` entirely** rather than ship a
fragile binary-scraper. If the user prefers the button to launch a
locally-cached `mt5setup.exe` directly, that is a small variant (search a
cache dir; fall back to the browser) — to confirm at spec review.

## Deletions (Approach A — clean removal)

**Source modules:**
- `manager/settings/credentials.py`
- `manager/brokers/` (whole package: `__init__.py`, `catalog.py`,
  `default.py`, `live.py`, `learned.py`, `data/brokers_default.json`)
- `manager/gui/server_picker.py`
- `manager/terminal/provisioning.py`

**Tests (deleted with their modules):**
- `manager/tests/test_credentials.py`
- `manager/tests/test_catalog.py`, `test_default.py`, `test_live.py`,
  `test_learned.py`
- `manager/tests/test_server_picker.py`
- `manager/tests/test_terminal_provisioning.py`

**Build config:**
- `pyproject.toml`: remove the `brokers/data/*.json` package-data section.

## Modified Tests

- `test_controller.py` — remove catalog/credential tests; update
  `build_worker_configs`/`start` tests to use the new `AccountSpec` and
  assert configs carry no `login`/`server`/`password`.
- `test_supervisor.py` / `test_supervisor_readiness.py` — update spawn
  calls (no `password` arg); `StartMsg` no password.
- `test_mt5_worker.py` — update `initialize` calls (path + portable only).
- `test_mt5_adapter.py` — update for the optional-credentials
  `initialize` signature.
- `test_messages.py` — update `StartMsg` encode/decode (no password).
- `test_terminal_manager.py` — remove `provision_shortfall`/`required_count`
  tests; keep `discover_all`/`assign`/`kill_terminal` tests.
- `test_main_window.py` — remove `BrokerServerPicker`/`get_catalog`/
  `refresh_brokers` fakes; update for terminal-only master form; add tests
  for the Launch + Install buttons and terminal-required validation.
- `test_slave_editor.py` — update for no-credential fields + the Launch
  button.
- `test_main_window_updates.py`, `test_tray.py` — remove the
  `get_catalog()`/`refresh_brokers()` fakes (no longer needed for
  `MainWindow` construction).
- `test_main_entry.py` — `_FakeStore`: remove the now-unused `.path`
  (it existed only for `get_catalog`).
- `test_settings_store.py` — drop the `learned_servers: []` round-trip
  assertion; keep `provisioned_instances` assertions.
- `test_updater.py` — unchanged (no credential/broker surface).

## Security Posture & Constraints

Manual-login mode **eliminates credential handling in the manager**: no
DPAPI store, no pipe-borne password, no `password_blob` in settings. This
is a strict reduction in credential exposure, consistent with the standing
security constraints — there are simply fewer credentials to protect. The
DPAPI-at-rest and pipe-passing constraints become **moot** for this codebase
(no credentials to encrypt or pipe). The binding constraint that remains in
force:

- **Demo accounts only — never log in with a real account.** This now
  applies to the *manual* terminal login the user performs in `terminal64.exe`;
  the manager enforces nothing here (it cannot — it never sees the
  credentials), so it is a user-side discipline, documented in the GUI
  disclaimer area and README.

Capture artifacts (pcaps, Frida logs) remain gitignored; this change does
not touch them.

## Testing

**Headless gate (non-GUI):** the controller, supervisor, worker, adapter,
messages, terminal-manager, and settings tests run headless and must stay
green. New/updated tests cover: optional-credentials `initialize` (path
+ portable only), `build_worker_configs` omits `login`/`server`/
`password`, `start` works with no credentials, `StartMsg` round-trips
without `password`, `prepare` rejects duplicate terminal paths.

**GUI gate (the PR #8/#11 lesson — see memory `gui-tests-need-pyside6-venv`):**
this change touches `MainWindow`/`SlaveEditor` construction (removes the
`BrokerServerPicker`, adds buttons), so the headless suite alone is **not**
a sufficient gate. Before merging, run the full suite with real PySide6
using the app venv:
`C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q`
(expect no skips). The per-task and final reviews of GUI-touching tasks
must either run this or note "GUI tests unexercised; CI is the gate."

A real-terminal smoke check is out of scope for automated tests (matches
the existing `RealMt5` "not unit-tested" convention). **Manual
verification** (documented in README): install a terminal via the Install
button's download page (custom path), log in to it via the Launch button,
select it in the dropdown, Start, and confirm copy.

## Out of Scope

- Auto-provisioning (auto-install) of terminals — explicitly removed.
- In-manager credential entry, DPAPI storage, broker/server discovery —
  explicitly removed.
- Any change to the copy engine, linkage, transform, snapshot-diff, or
  recovery logic — untouched.
- The MT5 protocol RE spike (current branch `spike/mt5-protocol-re`) —
  unrelated; this feature builds on `main`.

## Global Constraints

- Demo accounts only — manual terminal login is a demo account; never a
  real account.
- No credentials are stored, piped, or logged by the manager.
- Capture artifacts stay gitignored; not committed.
- `mt5.initialize(path=..., portable=False)` with no credentials connects
  to the terminal's saved account; the manager never supplies
  login/server/password.
- Tests: headless suite green AND GUI suite green under PySide6 (app venv)
  before merge — the headless suite is not the gate for GUI work.