# Testing — Copy Trades MT5 Local Manager

The local manager is a Python package (`manager/`) tested with **pytest**. The
copy engine, IPC, supervisor, and terminal management are fully unit-tested
without a GUI or a live MT5 terminal. The GUI tests import PySide6 and skip
cleanly when PySide6 is not installed.

---

## 1. Set up the environment

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .[test]
```

`pip install -e .[test]` installs the runtime dependencies (`PySide6`,
`psutil`, `MetaTrader5`) plus `pytest` (the `[test]` extra), and
makes the `manager` package importable. The test suite does not require
`MetaTrader5` to actually connect — workers run against a `FakeMt5` adapter in
tests, and GUI tests skip when PySide6 is absent.

---

## 2. Run the suite

From the repo root:

```powershell
pytest -q
```

Expected on a headless Windows env without PySide6 installed:

```
180 passed, 5 skipped
```

The 5 skips are the GUI test modules (`test_main_window`,
`test_main_window_updates`, `test_slave_editor`, `test_tray`,
`test_main_entry`) — they call `pytest.importorskip("PySide6")` at module level.
On a host with PySide6 installed they run too (then `215 passed`).

---

## 3. Suite layout

Tests live in `manager/tests/` and mirror the package structure:

| Module | Covers |
|--------|--------|
| `test_copy_loop.py`, `test_copy_loop_integration.py` | `CopyEngine` — snapshots → per-slave command queues |
| `test_baseline.py` | Recent-opens backfill at start |
| `test_linkage.py` | `CPY#<ticket>\|MV..\|SV..` comment encoding |
| `test_snapshot_diff.py` | Master snapshot → position events |
| `test_transform.py` | Master event → slave command (normalize, lot size) |
| `test_models.py` | Snapshot / Position dataclasses |
| `test_record_table.py` | Per-slave copied-position ledger |
| `test_messages.py`, `test_pipe_framing.py` | IPC message types + pipe framing |
| `test_mt5_worker.py`, `test_mt5_adapter.py` | Worker subprocess + Real/Fake adapter |
| `test_supervisor.py`, `test_supervisor_readiness.py` | Worker lifecycle, restart+backoff, readiness gate |
| `test_terminal_discovery.py`, `test_terminal_manager.py` | Terminal discovery / assignment |
| `test_settings_store.py` | Atomic JSON settings store + provisioned-instance registry |
| `test_controller.py` | `CopyController` orchestration (terminal mgmt + readiness gate) |
| `test_version.py` | `_version.__version__` single source of truth |
| `test_updater.py` | Version compare + wheel pre-download/SHA-verify/cache + apply-and-restart (mocked network/popen) |
| `test_update_helper.py` | Detached update helper: wait for parent exit → reinstall → relaunch (mocked) |
| `test_main_window.py`, `test_slave_editor.py`, `test_tray.py`, `test_main_entry.py` | GUI construction + app-graph wiring (skip without PySide6) |
| `test_main_window_updates.py` | GUI update UI: check-for-updates, Update available, engine-idle-gated Update & restart (skip without PySide6) |

---

## 4. Run a single module

```powershell
pytest -q manager/tests/test_copy_loop.py
pytest -q manager/tests/test_supervisor_readiness.py::test_wait_for_slaves_ready
```

---

## 5. Manual smoke test (demo accounts only)

The automated suite never touches a live MT5 terminal. For an end-to-end run
against real (demo) MT5 terminals, follow the manual runbook:

[`docs/smoke-test.md`](smoke-test.md) — demo accounts only, never a real account.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| GUI tests `SKIPPED` | PySide6 not installed | `pip install PySide6` to run them, or leave them skipped — the non-GUI suite is the gate |
| `MetaTrader5` import error | The package is Windows-only | Not required for the suite (workers use `FakeMt5` in tests); only needed to launch the app |

---

## 7. Quick dev loop

```bash
pytest -q                       # run the suite
python -m manager               # launch the app
git add -A
git commit -m "fix: ..." -m "Co-Authored-By: Claude <noreply@anthropic.com>"
git push origin main
```