# Manual-Login + Terminal-Path-Only Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-manager broker/server discovery + DPAPI credential flow with a manual-login, terminal-path-only workflow: the user logs in to each MT5 terminal by hand, then selects only the terminal path in the manager; the worker connects via `mt5.initialize(path=...)` with no credentials.

**Architecture:** Credentials become optional end-to-end. `AccountSpec` drops `login`/`server`/`password`; the selected `terminal_path` is the account identity. The worker calls `adapter.initialize(path, portable=...)` only. `StartMsg` drops its `password` field; the supervisor spawn signatures drop the `password` parameter. The broker catalog, server picker, DPAPI credential store, and auto-provision machinery are deleted. The GUI loses its Login/Broker-Server/Password rows and gains a "Launch terminal" button (runs `terminal64.exe`) and an "Install MetaTrader" button (opens the download page in the browser + a custom-path disclaimer).

**Tech Stack:** Python 3, PySide6 (GUI), MetaTrader5 Python package (lazy-imported), multiprocessing IPC, pytest. No new dependencies.

## Global Constraints

- Demo accounts only — the manual terminal login the user performs in `terminal64.exe` is a demo account; never a real account. The manager enforces nothing here (it never sees credentials), so it is user-side discipline, documented in the GUI disclaimer and README.
- No credentials (login/server/password) are stored, piped, or logged by the manager. The DPAPI-at-rest and pipe-passing constraints are moot for this codebase now — there are no credentials to encrypt or pipe.
- `mt5.initialize(path=..., portable=False)` with no `login`/`server`/`password` connects to the terminal's saved account; the manager never supplies credentials.
- Tests: the headless suite is NOT a sufficient gate for GUI work. For any task touching GUI code or `MainWindow`/`SlaveEditor` construction, also run the full suite with real PySide6 via the app venv: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q` (expect no skips). The GUI test modules use module-level `pytest.importorskip("PySide6")` and skip headless; CI installs PySide6 so they run there. (See memory `gui-tests-need-pyside6-venv`.)
- Run all commands from the worktree root `.claude/worktrees/plan4-gui-tray-wiring`. Never `cd` to the main repo.
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Predicted pass counts are NOT pass/fail criteria — the HARD GATE is "no new failures, suite green." Implementers run the real suite and report actual counts (headless + PySide6 for GUI tasks).

---

## File Structure

**Modify (source):**
- `manager/worker/mt5_adapter.py` — optional-credentials `initialize` (Task 1).
- `manager/ipc/messages.py` — `StartMsg` drops `password` (Task 2).
- `manager/worker/mt5_worker.py` — path-only `initialize` (Task 2).
- `manager/supervisor.py` — drop `password` from spawn/`WorkerHandle`/`_restart`; remove `on_status_msg` (Task 2).
- `manager/app/controller.py` — `start` drops password passing (Task 2); `AccountSpec` slim + catalog/credential methods deleted + `build_worker_configs`/`prepare` (Task 4).
- `manager/gui/main_window.py` — terminal-only master form + Launch/Install buttons (Task 3).
- `manager/gui/slave_editor.py` — terminal-only editor + Launch button (Task 3).
- `manager/terminal/manager.py` — drop `provision_shortfall`/`required_count` + provisioning import (Task 5).
- `manager/settings/store.py` — drop `learned_servers` setdefault (Task 6).
- `pyproject.toml` — remove brokers package-data (Task 7).
- `README.md`, `docs/TESTING.md` — manual-login workflow + counts (Task 8).

**Modify (tests):** `test_mt5_adapter.py` (T1), `test_messages.py`/`test_mt5_worker.py`/`test_supervisor.py`/`test_supervisor_readiness.py` (T2), `test_main_window.py`/`test_slave_editor.py`/`test_main_window_updates.py`/`test_tray.py`/`test_main_entry.py` (T3), `test_controller.py` (T4), `test_terminal_manager.py` (T5), `test_settings_store.py` (T6).

**Delete (Task 7):** `manager/settings/credentials.py`, `manager/brokers/` (whole package + `data/`), `manager/gui/server_picker.py`, `manager/terminal/provisioning.py`, and tests `test_credentials.py`, `test_catalog.py`, `test_default.py`, `test_live.py`, `test_learned.py`, `test_server_picker.py`, `test_terminal_provisioning.py`.

---

### Task 1: Adapter — optional-credentials `initialize`

**Files:**
- Modify: `manager/worker/mt5_adapter.py:12-16` (Protocol), `manager/worker/mt5_adapter.py:27-46` (FakeMt5), `manager/worker/mt5_adapter.py:131-157` (RealMt5)
- Test: `manager/tests/test_mt5_adapter.py`

**Interfaces:**
- Produces: `Mt5Adapter.initialize(path, login=None, password=None, server=None, portable=False) -> bool`. When `login is None`, `RealMt5` calls `mt5.initialize(path=path, portable=portable)` only (saved-account connect); when credentials are present it passes them as today. `FakeMt5.initialize` accepts the same signature and ignores credentials (returns `True`). Task 2's worker depends on this signature.

- [ ] **Step 1: Write the failing tests**

Add to `manager/tests/test_mt5_adapter.py` (after `test_initialize_shutdown_last_error`):

```python
def test_fake_initialize_path_only_no_credentials():
    mt = _fake()
    # no login/server/password -> path-only initialize still succeeds
    assert mt.initialize("C:/t/terminal64.exe") is True
    assert mt.initialize("C:/t/terminal64.exe", portable=True) is True


class _FakeMt5Module:
    """Stand-in for the lazy-imported MetaTrader5 module so RealMt5's
    conditional-kwargs behavior is unit-testable without MT5 installed."""
    def __init__(self):
        self.init_kwargs = None
        self._err = (0, "")
    def initialize(self, **kwargs):
        self.init_kwargs = dict(kwargs)
        return True
    def last_error(self):
        return self._err


def test_real_initialize_path_only_omits_credentials_kwargs():
    from manager.worker.mt5_adapter import RealMt5
    r = RealMt5()
    r._mt5 = _FakeMt5Module()  # bypass the lazy MetaTrader5 import
    ok = r.initialize("C:/t/terminal64.exe")
    assert ok is True
    kw = r._mt5.init_kwargs
    assert kw == {"path": "C:/t/terminal64.exe", "portable": False}
    assert "login" not in kw and "password" not in kw and "server" not in kw


def test_real_initialize_with_credentials_passes_all_kwargs():
    from manager.worker.mt5_adapter import RealMt5
    r = RealMt5()
    r._mt5 = _FakeMt5Module()
    r.initialize("C:/t/terminal64.exe", login=123, password="pw", server="Demo",
                 portable=True)
    kw = r._mt5.init_kwargs
    assert kw == {"path": "C:/t/terminal64.exe", "login": 123, "password": "pw",
                  "server": "Demo", "portable": True}
```

Also update the existing `test_initialize_shutdown_last_error` so it exercises the path-only form (replace its body):

```python
def test_initialize_shutdown_last_error():
    mt = _fake()
    assert mt.initialize("C:/t/terminal64.exe") is True
    assert mt.last_error() == (0, "")
    mt.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_mt5_adapter.py -q`
Expected: FAIL — `RealMt5.initialize` still requires credentials positionally / passes them unconditionally; the new path-only assertions fail.

- [ ] **Step 3: Implement the optional-credentials signature**

In `manager/worker/mt5_adapter.py`, update the Protocol `initialize`:

```python
    def initialize(self, path: str, login: int | None = None,
                   password: str | None = None, server: str | None = None,
                   portable: bool = False) -> bool: ...
```

Update `FakeMt5.initialize`:

```python
    def initialize(self, path, login=None, password=None, server=None,
                   portable=False):
        self._connected = True
        return True
```

Update `RealMt5.initialize` to build kwargs conditionally:

```python
    def initialize(self, path, login=None, password=None, server=None,
                   portable=False):
        mt5 = self._mod()
        kwargs = {"path": path, "portable": portable}
        if login is not None:
            kwargs["login"] = int(login)
            kwargs["password"] = password
            kwargs["server"] = server
        ok = mt5.initialize(**kwargs)
        if not ok:
            self._last_error = mt5.last_error()
        return bool(ok)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_mt5_adapter.py -q`
Expected: PASS (all adapter tests green).

- [ ] **Step 5: Run the full headless suite and commit**

Run: `python -m pytest -q` (headless).
Expected: green — this change is backward-compatible (existing callers still pass credentials positionally; the worker still does until Task 2).

```bash
git add manager/worker/mt5_adapter.py manager/tests/test_mt5_adapter.py
git commit -m "refactor(adapter): make initialize credentials optional

RealMt5.initialize builds mt5.initialize kwargs conditionally: with no
login it connects to the terminal's saved account (path + portable only);
with credentials it passes them as before. FakeMt5 accepts the same
optional signature. Backward-compatible — existing callers unchanged.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Drop password from IPC, worker, supervisor; controller.start stops passing it

**Files:**
- Modify: `manager/ipc/messages.py:55-62,176-182,204-206` (StartMsg + encode/decode), `manager/worker/mt5_worker.py:288-304` (worker_main), `manager/supervisor.py:17-32,64-71,97-107,131-151,152-192,215-257` (spawn/WorkerHandle/_spawn/_restart/on_status_msg), `manager/app/controller.py:248-265` (start: drop password args)
- Test: `manager/tests/test_messages.py`, `manager/tests/test_mt5_worker.py`, `manager/tests/test_supervisor.py`, `manager/tests/test_supervisor_readiness.py`

**Interfaces:**
- Consumes: Task 1's optional-credentials `initialize` (the worker now calls it with path + portable only).
- Produces: `StartMsg(config)` (no `password`); `Supervisor.spawn_master(config, adapter_kind="real", fake_state=None)` and `spawn_slave(slave_id, config, adapter_kind="real", fake_state=None)` (no `password`); `WorkerHandle` without a `password` field; `Supervisor` without `on_status_msg`. Task 4 depends on these signatures.

- [ ] **Step 1: Write/update the failing tests**

**`manager/tests/test_messages.py`** — replace `test_start_and_error_round_trip`:

```python
def test_start_and_error_round_trip():
    st = M.StartMsg(config={"terminal_path": "C:/t/terminal64.exe"})
    rt = M.decode(M.encode(st))
    assert rt.config["terminal_path"] == "C:/t/terminal64.exe"
    # password field is gone
    assert not hasattr(rt, "password")

    err = M.ErrorMsg(source_id="s1", message="boom", fatal=True)
    assert M.decode(M.encode(err)).fatal is True
```

**`manager/tests/test_mt5_worker.py`** — in `test_slave_init_emits_recovery_symbolinfo_status`, drop the credential keys from `cfg` (slave_init never read them, but the contract no longer carries them):

```python
    cfg = {"slave_id": "s1", "symbol_map_csv": "EURUSD=EURUSD"}
```

**`manager/tests/test_supervisor.py`** — drop `"pw"` from every `spawn_slave`/`spawn_master` call, drop `login`/`server` from config dicts, drop `password` from every `WorkerHandle(...)` and `fake_spawn` signature. Concretely:

- `_slave_cfg()`: remove `"login": 2,` and `"server": "Demo",` lines.
- `test_end_to_end_open_through_subprocesses`: `sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake", fake_state=_slave_state())` and `sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 20}, adapter_kind="fake", fake_state=master_state)`.
- `test_restart_on_process_death`: `sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake", fake_state=_slave_state())`.
- `test_master_death_restarts_master`: `sup.spawn_master({"terminal_path": "C:/t/m.exe", "master_interval_ms": 20}, adapter_kind="fake", fake_state=master_state)`.
- `test_consecutive_stale_failures_restart`: `WorkerHandle(name="s1", role="slave", proc=_StubProc(), pipe=None, config={}, adapter_kind="fake", fake_state=None, last_msg_ts=0.0)`; and `fake_spawn(name, role, config, adapter_kind, fake_state)` with `WorkerHandle(name=name, role=role, proc=_StubProc(), pipe=None, config=config, adapter_kind=adapter_kind, fake_state=fake_state, last_msg_ts=fake_now[0])`.
- `test_message_resets_fail_count`: same `WorkerHandle` drop-`password` edit.
- `test_shutdown_terminates_workers`: `sup.spawn_slave("s1", _slave_cfg(), adapter_kind="fake", fake_state=_slave_state())`.

**`manager/tests/test_supervisor_readiness.py`** — same pattern: drop `"pw"` from `spawn_slave`/`spawn_master`; drop `login`/`server` from `_slave_cfg()` and the master config dict; drop `password=""` from every `WorkerHandle(...)`; change both `fake_spawn(name, role, config, password, adapter_kind, fake_state)` to `fake_spawn(name, role, config, adapter_kind, fake_state)` and drop `password=password` from the `WorkerHandle` they build.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_messages.py manager/tests/test_mt5_worker.py manager/tests/test_supervisor.py manager/tests/test_supervisor_readiness.py -q`
Expected: FAIL — `StartMsg` still requires `password`; `spawn_slave`/`spawn_master` still require `password`; `WorkerHandle` still requires `password`.

- [ ] **Step 3: Implement the changes**

**`manager/ipc/messages.py`** — `StartMsg`:

```python
@dataclass(frozen=True)
class StartMsg:
    """First message on every worker pipe: carries the worker config. Sent by
    the supervisor before the worker calls mt5.initialize. No credentials —
    the worker connects to the terminal's saved account (manual login)."""
    config: dict
    KIND = "start"
```

`encode` start branch:

```python
    if kind == "start":
        return {"_kind": kind, "config": msg.config}
```

`decode` start branch:

```python
    if kind == "start":
        return StartMsg(config=d["config"])
```

**`manager/worker/mt5_worker.py`** — `worker_main`:

```python
def worker_main(pipe, role: str, adapter_kind: str = "real", fake_state=None):
    """Subprocess entry. Reads its StartMsg (config only — no credentials)
    from the pipe, then connects to the terminal's saved account."""
    try:
        start = recv_msg(pipe)
    except EOFError:
        return
    config = start.config
    if adapter_kind == "fake":
        adapter = FakeMt5(**(fake_state or {}))
    else:
        adapter = RealMt5()
    source_id = config.get("slave_id", role)
    ok = adapter.initialize(config["terminal_path"],
                            portable=bool(config.get("portable", False)))
    if not ok:
        try:
            send_msg(pipe, ErrorMsg(source_id=source_id,
                   message=f"initialize failed: {adapter.last_error()}", fatal=True))
        except (EOFError, OSError):
            pass
        return
    try:
        if role == "master":
            _master_loop(pipe, adapter, config)
        else:
            _slave_loop(pipe, adapter, config)
    except EOFError:
        pass  # manager closed the pipe -> graceful shutdown
    finally:
        try:
            adapter.shutdown()
        except Exception:
            pass
```

**`manager/supervisor.py`** — `WorkerHandle`: drop the `password` field:

```python
@dataclass
class WorkerHandle:
    name: str
    role: str
    proc: multiprocessing.Process
    pipe: object
    config: dict
    adapter_kind: str
    fake_state: dict | None
    got_symbol_info: bool = False
    got_status: bool = False
    restart_count: int = 0
    next_restart_at: float = 0.0
    last_msg_ts: float = 0.0
    fail_count: int = 0
```

`__init__`: remove the `self.on_status_msg = None  # ...` line (and its comment).

`spawn_master`/`spawn_slave`:

```python
    def spawn_master(self, config, adapter_kind="real", fake_state=None):
        self._handles["master"] = self._spawn("master", "master", config,
                                               adapter_kind, fake_state)

    def spawn_slave(self, slave_id, config, adapter_kind="real", fake_state=None):
        self._handles[slave_id] = self._spawn(slave_id, "slave", config,
                                              adapter_kind, fake_state)
```

`_spawn`:

```python
    def _spawn(self, name, role, config, adapter_kind, fake_state):
        parent_pipe, child_pipe = multiprocessing.Pipe(duplex=True)
        proc = multiprocessing.Process(target=worker_main,
            args=(child_pipe, role, adapter_kind, fake_state), daemon=True)
        proc.start()
        child_pipe.close()  # parent owns only the parent end
        send_msg(parent_pipe, StartMsg(config=config))
        return WorkerHandle(name=name, role=role, proc=proc, pipe=parent_pipe,
                            config=config, adapter_kind=adapter_kind,
                            fake_state=fake_state, last_msg_ts=self._time_fn())
```

`_dispatch_slave`: drop the `on_status_msg` block — the `elif isinstance(msg, StatusMsg):` branch becomes:

```python
        elif isinstance(msg, StatusMsg):
            self._engine.apply_status(slave_id, msg)
```

`_read_master`: drop the `on_status_msg` block — the `elif isinstance(msg, StatusMsg):` branch becomes:

```python
            elif isinstance(msg, StatusMsg):
                pass
```

(Keep the `elif` so the `if/elif/elif` chain and the trailing `return True` are unchanged. `StatusMsg` import stays — it is used by `isinstance`.)

`_restart`: change the `_spawn` call:

```python
        new_h = self._spawn(name, h.role, h.config, h.adapter_kind, h.fake_state)
```

**`manager/app/controller.py`** — `start`: drop the password arguments. Replace the two spawn calls:

```python
        for s in slaves:
            sup.spawn_slave(s.id, cfgs[s.id],
                            adapter_kind="real" if slave_fake_state is None else "fake",
                            fake_state=slave_fake_state)
```

and:

```python
        sup.spawn_master(mcfg,
                         adapter_kind="real" if master_fake_state is None else "fake",
                         fake_state=master_fake_state)
```

(`AccountSpec` still has `password` until Task 4; it is simply unused now. `build_worker_configs` still sets `login`/`server`/`portable` — the worker ignores them. Both are cleaned up in Task 4.)

- [ ] **Step 4: Run the affected tests to verify they pass**

Run: `python -m pytest manager/tests/test_messages.py manager/tests/test_mt5_worker.py manager/tests/test_supervisor.py manager/tests/test_supervisor_readiness.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full headless suite and commit**

Run: `python -m pytest -q` (headless).
Expected: green. (`test_controller.py` start-based tests still pass — they use the real supervisor + fake workers and never assert the password arg.)

```bash
git add manager/ipc/messages.py manager/worker/mt5_worker.py manager/supervisor.py \
        manager/app/controller.py manager/tests/test_messages.py \
        manager/tests/test_mt5_worker.py manager/tests/test_supervisor.py \
        manager/tests/test_supervisor_readiness.py
git commit -m "refactor(ipc,worker,supervisor): drop password from the worker spawn path

StartMsg carries config only (no password); worker_main connects to the
terminal's saved account via adapter.initialize(path, portable=...).
Supervisor spawn_master/spawn_slave/WorkerHandle/_spawn/_restart drop the
password parameter; the learned-server on_status_msg hook is removed.
controller.start no longer passes the account password. AccountSpec still
keeps its credential fields (unused) until Task 4. No credentials are piped.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Terminal-only GUI + AccountSpec credential fields optional + remove BrokerServerPicker + Launch/Install buttons

**Files:**
- Modify: `manager/app/controller.py:26-39` (AccountSpec: give credential fields defaults), `manager/gui/main_window.py:1-11,51-134,183-228` (master form + handlers + closeEvent), `manager/gui/slave_editor.py:1-146` (editor)
- Test: `manager/tests/test_main_window.py`, `manager/tests/test_slave_editor.py`, `manager/tests/test_main_window_updates.py`, `manager/tests/test_tray.py`, `manager/tests/test_main_entry.py`

**Interfaces:**
- Consumes: Task 2's spawn signatures (no password).
- Produces: `AccountSpec(id, terminal_path, ...)` constructible without credentials (credential fields default); `MainWindow` no longer constructs `BrokerServerPicker` (so `controller.get_catalog` becomes unused — deleted in Task 4); `MainWindow.launch_terminal_button`/`install_metatrader_button`/`install_disclaimer_label`; `SlaveEditor.launch_terminal_button`.

- [ ] **Step 1: Make AccountSpec credential fields optional (bridge to Task 4's removal)**

In `manager/app/controller.py`, give the credential fields defaults so the GUI can construct `AccountSpec` without them (Task 4 drops them entirely):

```python
@dataclass
class AccountSpec:
    id: str
    terminal_path: str | None = None
    login: int = 0
    server: str = ""
    password: str = ""
    symbol_map_csv: str = ""
    step_amount: float = 100.0
    step_size: float = 0.01
    max_lot: float = 10.0
    max_trade_age_minutes: float = 10.0
    normalize_sltp: bool = True
```

(Note: `terminal_path` moves up and stays optional here only so existing tests that pass it by keyword still work; Task 4 makes it required and drops the credential fields. The field ORDER change is intentional — `terminal_path` is now the primary field.)

- [ ] **Step 2: Write the failing GUI tests**

**`manager/tests/test_main_window.py`** — replace the whole file:

```python
import pytest

pytest.importorskip("PySide6")


class FakeController:
    """Minimal controller double for construction + wiring smoke tests."""
    def __init__(self):
        self.started = False
        self.stopped = False
        self._instances = []
    def discover_instances(self):
        return self._instances
    def start(self, master, slaves, **kw):
        self.started = True
        self.last_master = master
    def stop(self):
        self.stopped = True
    def is_running(self):
        return self.started and not self.stopped


def test_main_window_constructs(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.windowTitle()  # has a title
    # terminal-only master form: no login/picker/password, terminal + buttons
    assert w.master_terminal is not None
    assert w.launch_terminal_button is not None
    assert w.install_metatrader_button is not None
    assert w.install_disclaimer_label is not None
    assert w.start_button is not None
    assert w.stop_button is not None
    assert w.status_view is not None
    assert w.log_view is not None
    assert w.slave_list is not None


def test_terminal_dropdown_populated_from_controller(qapp):
    from manager.gui.main_window import MainWindow
    from manager.terminal.discovery import TerminalInstance
    c = FakeController()
    c._instances = [TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata"),
                    TerminalInstance("C:/i1", "C:/i1/terminal64.exe", "default")]
    w = MainWindow(c)
    items = [w.master_terminal.itemText(i) for i in range(w.master_terminal.count())]
    assert "C:/i0/terminal64.exe" in items
    assert "C:/i1/terminal64.exe" in items


def test_start_button_calls_controller_start_with_terminal_path(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    w.start_button.click()
    assert c.started
    assert c.last_master.terminal_path == "C:/i0/terminal64.exe"


def test_start_button_refuses_blank_terminal(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    # no terminal selected -> start() not called, a log line is appended
    w.start_button.click()
    assert not c.started
    assert "terminal" in w.log_view.toPlainText().lower()


def test_launch_terminal_button_runs_terminal64_exe(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    w.master_terminal.addItem("C:/i0/terminal64.exe")
    w.master_terminal.setCurrentIndex(0)
    popped = []
    monkeypatch.setattr("manager.gui.main_window.subprocess.Popen",
                        lambda cmd, **k: popped.append(cmd) or object())
    w.launch_terminal_button.click()
    assert popped == [["C:/i0/terminal64.exe"]]


def test_install_metatrader_button_opens_download_page(qapp, monkeypatch):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    opened = []
    monkeypatch.setattr("manager.gui.main_window.webbrowser.open",
                        lambda url: opened.append(url) or True)
    w.install_metatrader_button.click()
    assert len(opened) == 1 and "metatrader5.com" in opened[0]
    assert "custom" in w.install_disclaimer_label.text().lower()


def test_stop_button_calls_controller_stop(qapp):
    from manager.gui.main_window import MainWindow
    c = FakeController()
    w = MainWindow(c)
    w.stop_button.setEnabled(True)
    w.stop_button.click()
    assert c.stopped


def test_status_update_appends_to_status_view(qapp):
    from manager.gui.main_window import MainWindow
    from manager.app.controller import StatusUpdate
    c = FakeController()
    w = MainWindow(c)
    w.append_status(StatusUpdate(kind="info", message="hello"))
    assert "hello" in w.status_view.toPlainText()
```

**`manager/tests/test_slave_editor.py`** — replace the whole file:

```python
# manager/tests/test_slave_editor.py
import pytest

pytest.importorskip("PySide6")


class FakeController:
    def __init__(self, instances=None):
        self._instances = instances or []
    def discover_instances(self):
        return self._instances


def test_slave_editor_constructs(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.terminal.discovery import TerminalInstance
    c = FakeController([TerminalInstance("C:/i0", "C:/i0/terminal64.exe", "appdata")])
    dlg = SlaveEditor(c)
    assert dlg.terminal is not None
    assert dlg.launch_terminal_button is not None
    assert dlg.symbol_table is not None
    assert dlg.step_amount is not None
    assert dlg.max_lot is not None
    assert dlg.normalize_sltp is not None
    items = [dlg.terminal.itemText(i) for i in range(dlg.terminal.count())]
    assert "C:/i0/terminal64.exe" in items


def test_slave_editor_spec_returns_accountspec(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.id_edit.setText("s1")
    dlg.terminal.setCurrentIndex(0)
    dlg.step_amount.setText("100")
    dlg.step_size.setText("0.01")
    dlg.max_lot.setText("10")
    dlg.max_trade_age_minutes.setText("10")
    dlg.accept()                      # simulate the user clicking OK
    spec = dlg.spec()
    assert isinstance(spec, AccountSpec)
    assert spec.id == "s1"
    assert spec.terminal_path == "C:/i0/terminal64.exe"
    assert spec.max_lot == 10.0
    assert spec.normalize_sltp is True


def test_slave_editor_symbol_table_round_trips_into_csv(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController())
    dlg.symbol_table.setRowCount(1)
    dlg.symbol_table.setItem(0, 0, _qitem("EURUSD"))
    dlg.symbol_table.setItem(0, 1, _qitem("EURUSD"))
    spec = dlg._spec_from_fields("s2", "C:/i0/terminal64.exe", "100", "0.01",
                                 "10", "10", True)
    assert "EURUSD=EURUSD" in spec.symbol_map_csv


def test_slave_editor_launch_button_runs_terminal64_exe(qapp, monkeypatch):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.terminal.setCurrentIndex(0)
    popped = []
    monkeypatch.setattr("manager.gui.slave_editor.subprocess.Popen",
                        lambda cmd, **k: popped.append(cmd) or object())
    dlg.launch_terminal_button.click()
    assert popped == [["C:/i0/terminal64.exe"]]


def _inst(exe):
    from manager.terminal.discovery import TerminalInstance
    return TerminalInstance(exe.rsplit("/terminal64.exe", 1)[0], exe, "appdata")


def _qitem(text):
    from PySide6.QtWidgets import QTableWidgetItem
    return QTableWidgetItem(text)
```

**`manager/tests/test_main_window_updates.py`** — in `FakeController`, delete the `get_catalog` and `refresh_brokers` methods (lines 18-22 in the current file):

```python
class FakeController:
    def __init__(self, running=False):
        self._running = running
        self.stopped = []
    def is_running(self):
        return self._running
    def stop(self):
        self.stopped.append(True)
    def discover_instances(self):
        return []
```

(All other tests in this file are unchanged — they exercise the update UI, which is untouched.)

**`manager/tests/test_tray.py`** — in `FakeController`, delete the `get_catalog` and `refresh_brokers` methods:

```python
class FakeController:
    def __init__(self):
        self.stopped = False
    def stop(self):
        self.stopped = True
```

**`manager/tests/test_main_entry.py`** — in `_FakeStore`, remove the `.path` attribute (it existed only for `get_catalog`, which `MainWindow` no longer calls). Replace the class:

```python
class _FakeStore:
    def load(self): return {}
    def save(self, d): pass
```

- [ ] **Step 3: Run GUI tests under PySide6 to verify they fail**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py manager/tests/test_slave_editor.py manager/tests/test_main_window_updates.py manager/tests/test_tray.py manager/tests/test_main_entry.py -q`
Expected: FAIL — `MainWindow` still builds `BrokerServerPicker` (FakeController no longer has `get_catalog` → `AttributeError`); the new attributes (`launch_terminal_button`, etc.) don't exist yet.

- [ ] **Step 4: Implement the GUI changes**

**`manager/gui/main_window.py`** — replace the imports and the master-form UI section. New imports:

```python
from __future__ import annotations

import subprocess
import webbrowser

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QPushButton, QListWidget, QPlainTextEdit, QLabel, QGroupBox,
)

from manager.app.controller import AccountSpec, StatusUpdate

MT5_DOWNLOAD_URL = "https://www.metatrader5.com/en/download"
```

(Remove the `QLineEdit` import and the `BrokerServerPicker` import — `QLineEdit` is no longer used in this file.)

In `_build_ui`, replace the Master pane:

```python
        # Master pane — terminal-path only (manual login)
        master_box = QGroupBox("Master")
        mform = QFormLayout()
        self.master_terminal = QComboBox()
        self.master_terminal.setEditable(True)
        mform.addRow("Terminal", self.master_terminal)
        term_row = QHBoxLayout()
        self.launch_terminal_button = QPushButton("Launch terminal")
        self.install_metatrader_button = QPushButton("Install MetaTrader")
        term_row.addWidget(self.launch_terminal_button)
        term_row.addWidget(self.install_metatrader_button)
        mform.addRow("", term_row)
        self.install_disclaimer_label = QLabel(
            "Install MetaTrader opens the download page. Download and run "
            "mt5setup.exe, and choose a CUSTOM install path for each terminal "
            "— the default path collides with existing terminals. Log in to a "
            "DEMO account only.")
        self.install_disclaimer_label.setWordWrap(True)
        mform.addRow("", self.install_disclaimer_label)
        master_box.setLayout(mform)
```

In the wire-buttons section, add:

```python
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)
        self.install_metatrader_button.clicked.connect(self._on_install_metatrader)
```

Add the handlers (place after `_on_stop`):

```python
    def _on_launch_terminal(self):
        exe = self.master_terminal.currentText().strip()
        if not exe:
            self.append_log("select a terminal first")
            return
        try:
            subprocess.Popen([exe])
        except OSError as exc:
            self.append_log(f"failed to launch terminal: {exc}")

    def _on_install_metatrader(self):
        webbrowser.open(MT5_DOWNLOAD_URL)
```

Replace `_on_start`:

```python
    def _on_start(self):
        terminal_path = self.master_terminal.currentText().strip()
        if not terminal_path:
            self.append_log("select a master terminal first")
            return
        master = AccountSpec(id="master", terminal_path=terminal_path)
        try:
            self._controller.start(master, list(self._slaves))
            self.set_running(True)
        except Exception as exc:
            self.append_log(f"start failed: {exc}")
```

Replace the slave-list label line in `_on_add_slave`:

```python
            self._slaves.append(spec)
            label = spec.terminal_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            self.slave_list.addItem(f"{spec.id}: {label}")
```

**`manager/gui/slave_editor.py`** — replace the whole file:

```python
# manager/gui/slave_editor.py
from __future__ import annotations

import subprocess
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox,
    QTableWidget, QTableWidgetItem, QPushButton, QHBoxLayout, QCheckBox,
    QHeaderView,
)

from manager.app.controller import AccountSpec


class SlaveEditor(QDialog):
    """A modal dialog to add/edit one slave account: terminal-path dropdown
    (auto-populated, required — the user manually logs in to the terminal),
    a Launch-terminal button, a master->slave symbol map table, lot-sizing
    fields, maxLot, maxTradeAge, and the normalize-SL/TP toggle. ``spec()``
    returns the configured AccountSpec (None if cancelled)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Slave")
        self._controller = controller
        self._build_ui()
        self._populate_terminals()

    def _build_ui(self):
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("s1")
        self.terminal = QComboBox()
        self.terminal.setEditable(True)
        form.addRow("Slave id", self.id_edit)
        form.addRow("Terminal", self.terminal)
        term_row = QHBoxLayout()
        self.launch_terminal_button = QPushButton("Launch terminal")
        term_row.addWidget(self.launch_terminal_button)
        form.addRow("", term_row)
        root.addLayout(form)

        self.symbol_table = QTableWidget(0, 2)
        self.symbol_table.setHorizontalHeaderLabels(["Master symbol", "Slave symbol"])
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.symbol_table)
        sym_row = QHBoxLayout()
        self.add_sym_button = QPushButton("Add Row")
        self.del_sym_button = QPushButton("Remove Row")
        sym_row.addWidget(self.add_sym_button)
        sym_row.addWidget(self.del_sym_button)
        root.addLayout(sym_row)
        self.add_sym_button.clicked.connect(self._add_sym_row)
        self.del_sym_button.clicked.connect(self._del_sym_row)

        sizing = QFormLayout()
        self.step_amount = QLineEdit("100")
        self.step_size = QLineEdit("0.01")
        self.max_lot = QLineEdit("10")
        self.max_trade_age_minutes = QLineEdit("10")
        self.normalize_sltp = QCheckBox("Normalize SL/TP to slave open price")
        self.normalize_sltp.setChecked(True)
        sizing.addRow("Step amount", self.step_amount)
        sizing.addRow("Step size", self.step_size)
        sizing.addRow("Max lots", self.max_lot)
        sizing.addRow("Max trade age (min)", self.max_trade_age_minutes)
        root.addLayout(sizing)
        root.addWidget(self.normalize_sltp)

        buttons = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        root.addLayout(buttons)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.launch_terminal_button.clicked.connect(self._on_launch_terminal)

    def _on_launch_terminal(self):
        exe = self.terminal.currentText().strip()
        if not exe:
            return
        try:
            subprocess.Popen([exe])
        except OSError:
            pass

    def _add_sym_row(self):
        self.symbol_table.insertRow(self.symbol_table.rowCount())
        self.symbol_table.setItem(self.symbol_table.rowCount() - 1, 0, QTableWidgetItem(""))
        self.symbol_table.setItem(self.symbol_table.rowCount() - 1, 1, QTableWidgetItem(""))

    def _del_sym_row(self):
        r = self.symbol_table.currentRow()
        if r >= 0:
            self.symbol_table.removeRow(r)

    def _populate_terminals(self):
        self.terminal.clear()
        try:
            for inst in self._controller.discover_instances():
                self.terminal.addItem(inst.exe_path)
        except Exception:
            pass

    def _symbol_map_csv(self) -> str:
        pairs = []
        for r in range(self.symbol_table.rowCount()):
            m = self.symbol_table.item(r, 0)
            s = self.symbol_table.item(r, 1)
            if m is None or s is None:
                continue
            mt = m.text().strip()
            st = s.text().strip()
            if mt and st:
                pairs.append(f"{mt}={st}")
        return ",".join(pairs)

    def _spec_from_fields(self, sid, terminal_path, step_amount, step_size,
                          max_lot, max_age, normalize) -> AccountSpec:
        return AccountSpec(
            id=sid, terminal_path=terminal_path or None,
            symbol_map_csv=self._symbol_map_csv(),
            step_amount=float(step_amount), step_size=float(step_size),
            max_lot=float(max_lot), max_trade_age_minutes=float(max_age),
            normalize_sltp=bool(normalize))

    def spec(self) -> AccountSpec | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        return self._spec_from_fields(
            self.id_edit.text().strip() or "s1",
            self.terminal.currentText().strip(),
            self.step_amount.text(), self.step_size.text(),
            self.max_lot.text(), self.max_trade_age_minutes.text(),
            self.normalize_sltp.isChecked())


def add_slave(parent_window) -> AccountSpec | None:
    """Open the SlaveEditor modally against the main window's controller.
    Returns the configured AccountSpec, or None if the user cancelled."""
    dlg = SlaveEditor(parent_window._controller, parent=parent_window)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.spec()
    return None
```

(Add `from PySide6.QtWidgets import QLineEdit` back into the imports if needed — `QLineEdit` is used for `id_edit`, `step_amount`, etc. Keep `QLineEdit` in the import list.)

- [ ] **Step 5: Run GUI tests under PySide6 to verify they pass**

Run: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest manager/tests/test_main_window.py manager/tests/test_slave_editor.py manager/tests/test_main_window_updates.py manager/tests/test_tray.py manager/tests/test_main_entry.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full headless suite and commit**

Run: `python -m pytest -q` (headless) — GUI modules skip; non-GUI green.
Also run the PySide6 full suite once more: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q` — expect all green, no skips. (This is the merge gate for this GUI task per the Global Constraints.)

```bash
git add manager/app/controller.py manager/gui/main_window.py manager/gui/slave_editor.py \
        manager/tests/test_main_window.py manager/tests/test_slave_editor.py \
        manager/tests/test_main_window_updates.py manager/tests/test_tray.py \
        manager/tests/test_main_entry.py
git commit -m "feat(gui): terminal-path-only master form + slave editor, Launch/Install buttons

Remove the Login/BrokerServerPicker/Password rows from the master form and
slave editor — the selected terminal path is the account (manual login via
the terminal's own UI). Add a Launch terminal button (runs terminal64.exe
for the selected terminal) and an Install MetaTrader button (opens the
download page in the browser) with a custom-install-path + demo disclaimer.
AccountSpec credential fields become optional (defaults) as a bridge; Task 4
drops them. MainWindow no longer constructs BrokerServerPicker, so the
controller catalog methods become unused (deleted in Task 4).

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Controller cleanup — drop AccountSpec credential fields, delete catalog/credential methods, slim build_worker_configs/prepare

**Files:**
- Modify: `manager/app/controller.py:1-17,26-39,98-141,143-180,182-208,210-221,223-304` (imports, AccountSpec, delete catalog/credential methods, _account_dict, build_worker_configs, prepare, build_supervisor, start)
- Test: `manager/tests/test_controller.py`

**Interfaces:**
- Consumes: Task 2 spawn signatures (no password); Task 3 GUI (no longer calls `get_catalog`).
- Produces: `AccountSpec(id, terminal_path, symbol_map_csv="", ...)` — no credential fields; `build_worker_configs` returns dicts with `terminal_path` + tuning only (no `login`/`server`/`portable`); `prepare` no longer provisions. Task 7 deletes `manager.settings.credentials` and `manager.brokers` once this task removes the last imports.

- [ ] **Step 1: Write the failing tests**

Replace `manager/tests/test_controller.py` with:

```python
# manager/tests/test_controller.py
import time
import pytest

from manager.engine.models import SymbolInfo, BUY, Position
from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.app.controller import (
    CopyController, AccountSpec, StatusUpdate, ControllerError,
)
from manager.terminal.discovery import TerminalInstance

SI = SymbolInfo(point=0.00001, digits=5, tick_size=0.00001,
                volume_step=0.01, volume_min=0.01, volume_max=100.0)
NOW = int(time.time())


class FakeTerminalManager:
    """Stub TerminalManager: no real install/discover/kill. Returns a fixed
    pool of instances and records calls. No provision_shortfall (removed)."""
    def __init__(self, instances):
        self._instances = instances
        self.killed = []
    def discover_all(self):
        return list(self._instances)
    def assign(self, accounts):
        pool = list(self._instances)
        out = {}
        for a in accounts:
            ov = a.get("terminal_path")
            if ov:
                exe = ov if ov.endswith("terminal64.exe") else f"{ov}/terminal64.exe"
                out[a["id"]] = TerminalInstance(exe.rsplit("/", 1)[0], exe, "override")
                pool = [p for p in pool if p.exe_path != exe]
                continue
            if not pool:
                raise ControllerError("not enough")
            out[a["id"]] = pool.pop(0)
        return out
    def kill_terminal(self, exe_path):
        self.killed.append(exe_path)
        return 0


def _master(spec_id="master", terminal_path="C:/m/terminal64.exe"):
    return AccountSpec(id=spec_id, terminal_path=terminal_path)


def _slave(sid="s1", terminal_path="C:/s/terminal64.exe"):
    return AccountSpec(id=sid, terminal_path=terminal_path,
                       symbol_map_csv="EURUSD=EURUSD")


def _slave_state():
    return {"symbol_infos": {"EURUSD": SI},
            "account": {"login": 2, "balance": 1000.0, "equity": 1000.0,
                        "currency": "USD", "server": "Demo"},
            "ticks": {"EURUSD": (1.10000, 1.10010)}}


def _controller(instances):
    statuses, logs = [], []
    c = CopyController(
        terminal_manager=FakeTerminalManager(instances),
        on_status=lambda s: statuses.append(s),
        on_log=lambda m: logs.append(m),
    )
    return c, statuses, logs


def _tick_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_prepare_assigns_one_terminal_per_account():
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    assert set(assigned) == {"master", "s1"}
    assert assigned["master"].exe_path == "C:/m/terminal64.exe"
    assert assigned["s1"].exe_path == "C:/s/terminal64.exe"


def test_prepare_rejects_duplicate_terminal_path_overrides():
    c, _, _ = _controller([])
    with pytest.raises(ControllerError):
        c.prepare(AccountSpec(id="m", terminal_path="C:/same/terminal64.exe"),
                  [AccountSpec(id="s1", terminal_path="C:/same/terminal64.exe")])


def test_prepare_normalizes_override_dir_to_exe_path():
    c, _, _ = _controller([])
    assigned = c.prepare(_master(terminal_path="C:/override"),
                         [_slave(terminal_path="C:/s/terminal64.exe")])
    assert assigned["master"].exe_path == "C:/override/terminal64.exe"
    assert assigned["master"].source == "override"


def test_build_worker_configs_omits_credentials_and_portable():
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    assigned = c.prepare(_master(), [_slave()])
    cfgs = c.build_worker_configs(_master(), [_slave()], assigned)
    assert cfgs["master"]["terminal_path"] == "C:/m/terminal64.exe"
    assert cfgs["s1"]["terminal_path"] == "C:/s/terminal64.exe"
    assert cfgs["s1"]["symbol_map_csv"] == "EURUSD=EURUSD"
    # no credential/portable keys in any config
    for cfg in cfgs.values():
        assert "login" not in cfg
        assert "server" not in cfg
        assert "portable" not in cfg


def test_start_runs_readiness_gate_then_master_and_copies():
    """End-to-end: start() spawns slaves, waits for readiness, THEN spawns the
    master — and the first OPEN is not skipped. Real Supervisor + FakeMt5."""
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, statuses, logs = _controller(insts)
    master_state = {
        "positions": [Position(42, "EURUSD", BUY, 1.10000, 0.5, 1.095, 1.105,
                               NOW, 0.00001, "")],
        "symbol_infos": {"EURUSD": SI},
        "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                    "currency": "USD", "server": "Demo"}}
    c.start(_master(), [_slave()],
            master_fake_state=master_state,
            slave_fake_state=_slave_state())
    try:
        ok = _tick_until(
            lambda: c._engine._slaves["s1"].table.get(42) is not None
            and c._engine._slaves["s1"].table.get(42).slave_ticket != 0)
        assert ok, "first OPEN did not flow (readiness gate failed)"
        assert any(s.kind == "ready" for s in statuses)
    finally:
        c.stop()


def test_start_passes_kill_terminal_to_supervisor():
    c, _, _ = _controller([TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
                           TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")])
    sup = c.build_supervisor(heartbeat_seconds=5)
    assert sup._kill_terminal == c._terminal_manager.kill_terminal


def test_is_running_reflects_start_stop():
    c, _, _ = _controller([TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
                           TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")])
    assert not c.is_running()
    c.start(_master(), [_slave()],
            master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                            "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    assert c.is_running()
    c.stop()
    assert not c.is_running()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_controller.py -q`
Expected: FAIL — `AccountSpec` still requires `login`/`server`/`password` positionally for the old call sites that are now gone; the deleted catalog/credential methods are still present (tests above don't reference them, but the import of `credentials` at top of the OLD test file is gone — the new file has no such import). The failures are: `AccountSpec` still has the old required ordering, `build_worker_configs` still emits `login`/`server`/`portable`, `FakeTerminalManager` in the new test has no `provisioned` but `prepare` still calls `provision_shortfall`.

- [ ] **Step 3: Implement the controller cleanup**

In `manager/app/controller.py`, replace the imports (drop brokers/catalog/credentials):

```python
# manager/app/controller.py
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from manager.engine.copy_loop import CopyEngine, SlaveConfig
from manager.engine.models import BUY, SELL  # noqa: F401  (re-exported for GUI)
from manager.supervisor import Supervisor
from manager.terminal.discovery import TerminalInstance
from manager.settings.store import SettingsStore


class ControllerError(Exception):
    """Raised before Start for unrecoverable config: duplicate/unresolvable
    terminal-path assignments, or not enough instances after assignment."""


@dataclass
class AccountSpec:
    id: str
    terminal_path: str
    symbol_map_csv: str = ""
    step_amount: float = 100.0
    step_size: float = 0.01
    max_lot: float = 10.0
    max_trade_age_minutes: float = 10.0
    normalize_sltp: bool = True


@dataclass
class StatusUpdate:
    kind: str            # "info" | "error" | "provisioning" | "ready" | "slave_status"
    message: str
    slave_id: str | None = None
    connected: bool | None = None
    balance: float | None = None
    equity: float | None = None


def _normalize_override_exe(path: str) -> str:
    """An assignment may point at the install dir or directly at terminal64.exe.
    Normalize to the exe path."""
    p = path.replace("\\", "/")
    if p.endswith("terminal64.exe"):
        return p
    return p.rstrip("/") + "/terminal64.exe"
```

`CopyController.__init__` — drop the `credentials` param and the catalog/recorded-servers state:

```python
    def __init__(self, terminal_manager, store=None,
                 supervisor_factory=None,
                 on_status=None, on_log=None, clock=time.time):
        self._terminal_manager = terminal_manager
        self._store = store if store is not None else SettingsStore()
        self._supervisor_factory = (supervisor_factory
                                     if supervisor_factory is not None
                                     else self._default_supervisor_factory)
        self._on_status = on_status or (lambda s: None)
        self._on_log = on_log or (lambda m: None)
        self._clock = clock
        self._supervisor: Supervisor | None = None
        self._engine: CopyEngine | None = None
```

Delete entirely: `_cache_path`, `get_catalog`, `_build_catalog`, `refresh_brokers`, `_on_worker_status`, `load_password`, `save_password`.

`_account_dict` — slim to just what `assign` reads:

```python
    @staticmethod
    def _account_dict(a: AccountSpec) -> dict:
        return {"id": a.id, "terminal_path": a.terminal_path}
```

`build_worker_configs` — drop `login`/`server`/`portable`:

```python
    def build_worker_configs(self, master: AccountSpec, slaves: list[AccountSpec],
                             assigned: dict[str, TerminalInstance]
                             ) -> dict[str, dict]:
        """Build the per-account worker config dicts the Supervisor spawns."""
        cfgs: dict[str, dict] = {}
        m_inst = assigned[master.id]
        cfgs[master.id] = {
            "terminal_path": m_inst.exe_path,
            "master_interval_ms": 1000,
        }
        for s in slaves:
            s_inst = assigned[s.id]
            cfgs[s.id] = {
                "slave_id": s.id,
                "terminal_path": s_inst.exe_path,
                "symbol_map_csv": s.symbol_map_csv,
                "normalize_sltp": s.normalize_sltp,
                "retry_count": 3, "retry_delay_ms": 500,
                "slave_status_interval_ms": 5000,
            }
        return cfgs
```

`prepare` — drop the `provision_shortfall` call and the "provisioned" log line:

```python
    def prepare(self, master: AccountSpec, slaves: list[AccountSpec]
                ) -> dict[str, TerminalInstance]:
        """Validate terminal-path assignments (uniqueness + normalize to exe
        path) and assign one instance per account. Raises ControllerError on
        duplicate/unresolvable assignments. No auto-provisioning — the user
        brings terminals they have already installed and logged in to."""
        seen: dict[str, str] = {}
        accounts = [self._account_dict(master)] + [self._account_dict(s)
                                                   for s in slaves]
        for a in accounts:
            ov = a.get("terminal_path")
            if ov:
                exe = _normalize_override_exe(ov)
                a["terminal_path"] = exe
                if exe in seen:
                    raise ControllerError(
                        f"terminal path {exe} assigned to both "
                        f"{seen[exe]} and {a['id']}")
                seen[exe] = a["id"]
        self._status("info", "assigning terminal instances…")
        assigned = self._terminal_manager.assign(accounts)
        self._status("info", "terminal instances assigned")
        return assigned
```

`build_supervisor` — drop the `on_status_msg` wiring:

```python
    def build_supervisor(self, heartbeat_seconds: int = 5) -> Supervisor:
        eng = CopyEngine()
        self._engine = eng
        sup = self._supervisor_factory(
            eng, heartbeat_seconds=heartbeat_seconds,
            kill_terminal=self._terminal_manager.kill_terminal)
        sup.on_restart = lambda name, role: self._status(
            "info", f"restarted {role} {name}")
        return sup
```

`start` — keep the Task 2 spawn calls (no password). The `master_fake_state`/`slave_fake_state` test hooks stay. No change to the spawn call lines from Task 2, but verify they read `adapter_kind="real" if slave_fake_state is None else "fake"` and pass `fake_state=...` (no password). The body otherwise is unchanged from the end of Task 2.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_controller.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full headless suite and commit**

Run: `python -m pytest -q` (headless).
Expected: green. (GUI modules skip headless; `controller.py` no longer imports `brokers`/`credentials`, and `manager.brokers`/`manager.settings.credentials` still exist until Task 7 — they are now simply unreferenced.)

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "refactor(controller): drop credentials + broker catalog; terminal-path-only accounts

AccountSpec loses login/server/password — the selected terminal_path is
the account identity. build_worker_configs drops login/server/portable
(the worker connects to the terminal's saved account). prepare drops
auto-provisioning (no provision_shortfall call). Deletes the broker catalog
wiring (get_catalog/_build_catalog/refresh_brokers/_cache_path), the
learned-server hook (_on_worker_status/_recorded_servers), and the DPAPI
credential methods (load_password/save_password) + the credentials
constructor param. manager.brokers and manager.settings.credentials are now
unreferenced — deleted in Task 7.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Terminal manager — drop provision_shortfall/required_count + provisioning import

**Files:**
- Modify: `manager/terminal/manager.py:7-86` (imports, constructor, provision_shortfall, required_count)
- Test: `manager/tests/test_terminal_manager.py`

**Interfaces:**
- Produces: `TerminalManager(store=, discover_fn=, process_iter_fn=, sleep_fn=, time_fn=)` — no `provision_fn`/`download_fn` params; no `provision_shortfall`/`required_count` methods. `discover_all`/`assign`/`kill_terminal` unchanged. Task 7 deletes `manager/terminal/provisioning.py` once this task removes the import.

- [ ] **Step 1: Update the tests**

In `manager/tests/test_terminal_manager.py`:

- Delete `test_required_count_is_one_plus_slaves`.
- Delete `test_provision_shortfall_installs_and_registers`.
- Delete `test_provision_shortfall_only_installs_the_gap`.
- In `_mgr`, the helper already does not pass `provision_fn`/`download_fn` — no change needed there. (It sets `discover_fn`, `process_iter_fn`, `sleep_fn`, `time_fn` via `setdefault`.)
- Keep `test_discover_all_merges_appdata_default_and_provisioned`, `test_discover_all_dedups_by_exe_path`, `test_assign_*`, and all `test_kill_terminal_*` unchanged.
- The `from manager.terminal.manager import TerminalManager, TerminalManagerError` import stays — `assign` still raises `TerminalManagerError` defensively.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_terminal_manager.py -q`
Expected: FAIL — `TerminalManager` constructor still accepts/`__init__` still stores `_provision_fn`/`_download_fn`; `provision_shortfall`/`required_count` still exist (the deleted tests are gone, but the module still imports `provisioning`).

- [ ] **Step 3: Implement the changes**

In `manager/terminal/manager.py`, remove the provisioning import and the provisioning-related constructor params and methods. New module:

```python
# manager/terminal/manager.py
from __future__ import annotations

import os
from pathlib import Path

from manager.terminal.discovery import TerminalInstance, discover_terminals
from manager.settings.store import SettingsStore


class TerminalManagerError(Exception):
    """Raised when there are not enough terminal instances to assign one per
    account (defensive — every account carries an explicit terminal_path in
    the terminal-path-only workflow, so this is unreachable in normal use)."""


def _default_process_iter(attrs=None):
    import psutil
    return psutil.process_iter(attrs)


def _norm(p: str) -> str:
    return os.path.normpath(p).lower()


class TerminalManager:
    """Owns terminal-instance lifecycle: discover existing installs
    (origin.txt + default Program Files + the store's provisioned registry)
    and assign one instance per account. Provisioning (auto-install) was
    removed — the user installs and logs in to terminals manually. Kill a
    specific instance's terminal64.exe before a worker respawn so
    mt5.initialize does not hit the -10003 IPC-collision from a stale
    terminal. The provisioned-instance registry is still merged by
    discover_all so terminals installed under prior versions remain
    discoverable."""

    def __init__(self, store=None, discover_fn=None, process_iter_fn=None,
                 sleep_fn=None, time_fn=None):
        self._store = store if store is not None else SettingsStore()
        self._discover_fn = (discover_fn if discover_fn is not None
                             else discover_terminals)
        self._process_iter_fn = (process_iter_fn if process_iter_fn is not None
                                 else _default_process_iter)
        self._sleep = sleep_fn if sleep_fn is not None else __import__("time").sleep
        self._time = time_fn if time_fn is not None else __import__("time").monotonic

    def discover_all(self) -> list[TerminalInstance]:
        """Merge origin.txt-discovered installs with the store's provisioned
        registry. Dedup by exe_path (origin.txt wins ties on order)."""
        seen: dict[str, TerminalInstance] = {}
        for inst in self._discover_fn():
            seen.setdefault(_norm(inst.exe_path), inst)
        for install_dir in self._store.list_provisioned_instances():
            exe_path = str(Path(install_dir) / "terminal64.exe")
            key = _norm(exe_path)
            if key not in seen:
                seen[key] = TerminalInstance(install_dir=install_dir,
                                              exe_path=exe_path,
                                              source="provisioned")
        return list(seen.values())

    def assign(self, accounts: list[dict]) -> dict[str, TerminalInstance]:
        """Assign one terminal instance per account. Accounts carrying a
        ``terminal_path`` keep it (tagged ``override``); the rest auto-assign
        from the available pool. Raises if there are not enough instances."""
        available = self.discover_all()
        pool = list(available)
        assigned: dict[str, TerminalInstance] = {}
        for acct in accounts:
            acct_id = acct["id"]
            override = acct.get("terminal_path")
            if override:
                install_dir = str(Path(override).parent)
                assigned[acct_id] = TerminalInstance(
                    install_dir=install_dir, exe_path=override,
                    source="override")
                pool = [p for p in pool if _norm(p.exe_path) != _norm(override)]
                continue
            if not pool:
                raise TerminalManagerError(
                    f"not enough terminal instances to assign {acct_id} "
                    f"(need {len(accounts)}, have {len(assigned) + len(pool)})")
            assigned[acct_id] = pool.pop(0)
        return assigned

    def kill_terminal(self, exe_path: str) -> int:
        """Terminate every running process whose executable path equals
        exe_path (case-insensitive, normalized). Best-effort: per-process
        NoSuchProcess / AccessDenied are swallowed. terminate() then wait();
        on a wait timeout, fall back to kill(). Returns the count of processes
        we attempted to terminate (matched by exe path). Never kills by image
        name alone — multiple instances share the name terminal64.exe."""
        target = _norm(exe_path)
        count = 0
        for proc in self._process_iter_fn():
            try:
                pexe = proc.exe()
            except Exception:
                pexe = None
            if not pexe or _norm(pexe) != target:
                continue
            count += 1
            try:
                proc.terminate()
            except Exception:
                continue
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_terminal_manager.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full headless suite and commit**

Run: `python -m pytest -q` (headless).
Expected: green.

```bash
git add manager/terminal/manager.py manager/tests/test_terminal_manager.py
git commit -m "refactor(terminal): drop auto-provisioning; discover + assign + kill only

TerminalManager loses provision_shortfall/required_count and the
provision_fn/download_fn constructor params + the provisioning import. The
user installs and logs in to terminals manually; the manager only discovers,
assigns, and kills. discover_all still merges the store's provisioned
registry so terminals installed under prior versions remain discoverable.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Settings store — drop learned_servers

**Files:**
- Modify: `manager/settings/store.py:15-46` (docstring + load setdefault)
- Test: `manager/tests/test_settings_store.py`

**Interfaces:**
- Produces: `SettingsStore.load()` no longer sets a `learned_servers` default. The `provisioned_instances` registry stays (used by `discover_all`). Task 7 deletes `manager.brokers.learned` once this task removes the last reference.

- [ ] **Step 1: Update the tests**

In `manager/tests/test_settings_store.py`:

- In `test_save_then_load_round_trip`, drop the `learned_servers` key from `data`:

```python
def test_save_then_load_round_trip(tmp_path):
    store = _store(tmp_path)
    data = {"accounts": {"master": {"login": 5001, "server": "Demo-Server"}},
            "provisioned_instances": [], "global": {"heartbeat_seconds": 5}}
    store.save(data)
    assert store.load() == data
```

- Delete `test_load_defaults_learned_servers_list`.
- Delete `test_password_blob_survives_round_trip` (credential concept removed).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest manager/tests/test_settings_store.py -q`
Expected: FAIL — `load()` still setdefaults `learned_servers` (the round-trip data no longer has it, but load adds it, so `load() == data` fails).

- [ ] **Step 3: Implement the change**

In `manager/settings/store.py`, remove the `learned_servers` setdefault line from `load`:

```python
        data.setdefault("accounts", {})
        data.setdefault("provisioned_instances", [])
        data.setdefault("global", {})
        return data
```

Update the class docstring (drop the "password blobs" mention):

```python
class SettingsStore:
    """Plain-JSON persistence of the manager's config. Also owns the registry
    of terminal instances THIS manager provisioned (under prior versions), so
    discovery can merge them with origin.txt-discovered installs."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest manager/tests/test_settings_store.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full headless suite and commit**

Run: `python -m pytest -q` (headless).
Expected: green.

```bash
git add manager/settings/store.py manager/tests/test_settings_store.py
git commit -m "refactor(store): drop learned_servers default; no broker catalog

SettingsStore.load no longer setdefaults learned_servers (the broker
learned-server feature is gone). The provisioned_instances registry stays
(discover_all still merges it). Drops the password-blob docstring note.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Delete dead modules + tests + pyproject package-data

**Files:**
- Delete: `manager/settings/credentials.py`, `manager/brokers/__init__.py`, `manager/brokers/catalog.py`, `manager/brokers/default.py`, `manager/brokers/live.py`, `manager/brokers/learned.py`, `manager/brokers/data/brokers_default.json`, `manager/gui/server_picker.py`, `manager/terminal/provisioning.py`, `manager/tests/test_credentials.py`, `manager/tests/test_catalog.py`, `manager/tests/test_default.py`, `manager/tests/test_live.py`, `manager/tests/test_learned.py`, `manager/tests/test_server_picker.py`, `manager/tests/test_terminal_provisioning.py`
- Modify: `pyproject.toml` (remove brokers package-data section)

**Interfaces:**
- Consumes: Tasks 4-6 removed all references to these modules. Verify with grep before deleting.

- [ ] **Step 1: Verify no remaining references**

Run (from the worktree root):
```bash
grep -rn "manager.brokers\|manager.settings.credentials\|manager.gui.server_picker\|manager.terminal.provisioning\|BrokerServerPicker\|BrokerCatalog\|provision_shortfall\|provision_instance\|download_setup\|load_password\|save_password\|on_status_msg\|learned_servers" manager/ --include=*.py
```
Expected: no matches in source files (tests that imported the deleted modules are themselves deleted in this task; the grep should be run AFTER the test deletions below — or run it first and confirm only the soon-to-be-deleted test files match). If any non-deleted source file references them, stop and fix before proceeding.

- [ ] **Step 2: Delete the modules and tests**

```bash
git rm manager/settings/credentials.py \
       manager/brokers/__init__.py manager/brokers/catalog.py \
       manager/brokers/default.py manager/brokers/live.py \
       manager/brokers/learned.py manager/brokers/data/brokers_default.json \
       manager/gui/server_picker.py manager/terminal/provisioning.py \
       manager/tests/test_credentials.py manager/tests/test_catalog.py \
       manager/tests/test_default.py manager/tests/test_live.py \
       manager/tests/test_learned.py manager/tests/test_server_picker.py \
       manager/tests/test_terminal_provisioning.py
```

(If `manager/brokers/data/` becomes empty, also `rmdir` it — `git rm` of the json leaves the dir; git tracks files only, so the empty dir is harmless and ignored. No action needed.)

- [ ] **Step 3: Remove the brokers package-data from pyproject.toml**

Open `pyproject.toml`, find the package-data section that includes `brokers/data/*.json` (added in the broker-browser PR), and remove that entry. (Read the file first to locate the exact lines; the section is under the build config that ships the default brokers JSON as package data. Remove only the `brokers/data/*.json` line — leave any other package-data untouched.)

- [ ] **Step 4: Run both suites to verify green**

Run headless: `python -m pytest -q` — expected green (fewer test files, no skips beyond the usual 6 GUI modules).
Run PySide6: `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe -m pytest -q` — expected green, no skips.
Also verify the package still builds: `python -m build --wheel` (or `pip install -e .` in a throwaway venv) — expected success with no missing-data errors.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: delete dead broker catalog, server picker, credentials, provisioning

Removes modules made unreferenced by the terminal-path-only workflow:
manager.settings.credentials (DPAPI), the manager.brokers package (catalog/
default/live/learned + data), manager.gui.server_picker, and
manager.terminal.provisioning (auto-install), with their tests. Drops the
brokers package-data from pyproject.toml. No source references remain.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: README + docs/TESTING — manual-login workflow, demo disclaimer, counts

**Files:**
- Modify: `README.md`, `docs/TESTING.md`

- [ ] **Step 1: Update README**

Read `README.md` first. Make these edits (adapt phrasing to the existing structure, preserving the Features + File-layout conventions):

- In the **Features** section: replace any broker/server-browser bullet with: *"Manual-login, terminal-path-only account setup — log in to each MT5 terminal via its own UI, then select the terminal path in the manager. No credentials are stored or entered in the manager."* Add a bullet: *"Launch terminal button (opens terminal64.exe for the selected terminal) and Install MetaTrader button (opens the download page; use a custom install path per terminal)."*
- Add/note the demo-only constraint prominently near the setup steps: *"Log in to a DEMO account only — never a real account. The manager never sees or stores your credentials."*
- In the **File layout** section: remove the `brokers/` and `gui/server_picker.py` and `settings/credentials.py` and `terminal/provisioning.py` entries; add `gui/main_window.py` (Launch/Install buttons) and note `gui/slave_editor.py` (terminal-only). Keep the layout consistent with the actual tree after Task 7.
- In any **Setup/Usage** section: replace the broker/server/login/password entry steps with the manual-login steps: (1) Install MetaTrader via the Install button (custom path per terminal), (2) Launch each terminal via the Launch button and log in to a demo account, (3) In the manager select the terminal path for master and each slave, (4) Start.

- [ ] **Step 2: Update docs/TESTING.md**

Read `docs/TESTING.md` first. Update the suite-layout table: remove rows for the deleted test files (`test_credentials`, `test_catalog`, `test_default`, `test_live`, `test_learned`, `test_server_picker`, `test_terminal_provisioning`); the remaining rows stay. Do NOT change any pass/skip count prose unless you have just run the suites — record the actual counts from the Task 7 runs in the commit message, and only update count prose if the existing convention (headless baseline) is clearly contradicted. (Ruling: a stale README count is a pre-existing convention issue, not this task's defect — leave it unless the numbers are obviously wrong after the deletions.)

- [ ] **Step 3: Commit**

```bash
git add README.md docs/TESTING.md
git commit -m "docs: manual-login workflow, demo disclaimer, file-layout + test-layout update

README: replace broker/server-browser with the manual-login terminal-path
workflow (Install + Launch buttons, custom install path, demo-only).
docs/TESTING: drop rows for the deleted broker/credential/provisioning
test modules.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Manual-login, terminal-path-only, `mt5.initialize(path=...)` no-creds — Task 1 (adapter) + Task 2 (worker).
- AccountSpec drops login/server/password — Task 4 (full drop) via Task 3 (optional bridge).
- `StartMsg`/`WorkerHandle`/spawn drop password wholesale — Task 2.
- Drop auto-provisioning — Task 5 (TerminalManager) + Task 7 (delete provisioning.py).
- Launch terminal button — Task 3 (main_window + slave_editor).
- Install MetaTrader button + custom-path disclaimer — Task 3 (main_window).
- Demo-only constraint documented — Task 3 (disclaimer label) + Task 8 (README).
- Delete broker catalog / server_picker / credentials — Task 7 (after Tasks 4-6 remove references).
- Security posture (no credentials stored/piped) — Tasks 2 + 4 + 7.
- Testing gates (headless + PySide6) — Global Constraints + each GUI task runs the PySide6 suite.
- README/docs — Task 8.

**Placeholder scan:** No TBD/TODO; every code step contains the actual code or the exact deletion list. Task 7's pyproject edit instructs reading the file first (the exact lines are not known without reading it) — acceptable since the instruction is concrete ("remove only the `brokers/data/*.json` line"). Task 8 instructs reading README/TESTING first then making named edits — the edits are concrete in content even if phrased to match the existing structure.

**Type consistency:**
- `AccountSpec(id, terminal_path, ...)` consistent across Tasks 3 (optional bridge) → 4 (final, no cred fields). Task 3's `terminal_path: str | None = None` (bridge) becomes Task 4's `terminal_path: str` (required). Tests in Task 3 construct `AccountSpec(id, terminal_path)` — valid in both.
- `initialize(path, login=None, password=None, server=None, portable=False)` — Task 1 defines it; Task 2 worker calls `adapter.initialize(config["terminal_path"], portable=...)` — matches.
- `spawn_master(config, adapter_kind="real", fake_state=None)` / `spawn_slave(slave_id, config, adapter_kind="real", fake_state=None)` — Task 2 defines; Task 4 `start` uses them — matches.
- `WorkerHandle` without `password` — Task 2; tests in Task 2 drop it — matches.
- `_spec_from_fields(sid, terminal_path, step_amount, step_size, max_lot, max_age, normalize)` — Task 3 slave_editor; Task 3 test calls it positionally with those 7 args — matches.
- `build_worker_configs` returns dicts with `terminal_path`/tuning only — Task 4; Task 4 test asserts no `login`/`server`/`portable` — matches.

No issues found.