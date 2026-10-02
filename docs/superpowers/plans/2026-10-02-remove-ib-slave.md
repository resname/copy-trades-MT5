# Remove IB Slave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the shipped IB/TWS-API slave feature and all demo-account-only
disclaimers, leaving a MetaTrader-5-only product with unchanged MT5 behavior.

**Architecture:** Pure subtraction in dependency order — GUI first (the entry
points that create IB configs), then controller + the loader's drop-at-load
compat guard, then supervisor + the `worker/ib` package, then the update
helper + dependency, and finally docs. Each task leaves the full suite green.

**Tech Stack:** Python 3.11+, PySide6 (GUI), pytest, MetaTrader5 package.

**Spec:** `docs/superpowers/specs/2026-10-02-remove-ib-slave-design.md`

## Global Constraints

- Engine, IPC message shapes, MT5 worker, MT5 adapter, terminal discovery and
  the settings store get **zero diffs** (spec rule 1). Exception: `StatusMsg.detail`
  in `manager/ipc/messages.py` is **kept** (IPC contract stability); only the
  supervisor/controller GUI plumbing around it goes.
- Run the suite from the repo root with the app venv:
  `"C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests -q`.
  Every task ends with that suite green.
- Commit style: conventional prefixes (`refactor:`, `test:`, `docs:`), each
  commit message ending with `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
- A v0.1.26 settings.json carrying `platform: "ib"` must load without
  crashing: that slave is **dropped in memory with one logged line**; the
  settings file itself is never rewritten by the loader (spec rule 4).
- Demo language removal applies to user-visible text only (GUI string,
  README, runbooks). Functional instructions on the same lines (custom
  install path, launch-for-login) are preserved.

## Review Focus

1. **Legacy settings.json with an IB slave** — a v0.1.26 savefile must load
   with the mt5 slave intact, the IB slave dropped, and one log line, no
   crash and no rewritten file. → pinned by Task 2's `test_load_config_drops_saved_ib_slave_and_keeps_mt5`.
2. **Half-stripped GUI** — any leftover reference to a removed widget
   (`platform`, `ib_host`, …) makes the editor dead on open; the editor must
   render and produce the exact v0.1.25-shaped MT5 spec. → pinned by Task 1's
   suite pass on `test_slave_editor.py` + `test_main_window.py`.
3. **Worker dispatch on legacy configs** — a config dict with no `platform`
   key (and, defensively, any truthy `platform` value) must always spawn the
   MT5 worker. → pinned by Task 3's `test_worker_target_is_always_mt5`.
4. **In-app update from a v0.1.26 install** — that venv already holds
   `ib_async`; the update must remain a single `--no-deps` wheel install with
   no IB step and not uninstall the leftover package. → pinned by Task 4's
   reinstall-command tests.
5. **Dangling docs** — no live file may link to `smoke-test-ib.md`, the
   removed README section, or any `ib`/TWS surface. → pinned by Task 5's
   grep sweep.

---

### Task 1: GUI strip — slave editor + install label

**Files:**
- Modify: `manager/gui/slave_editor.py`
- Modify: `manager/gui/main_window.py:104-110` (install disclaimer label)
- Test: `manager/tests/test_slave_editor.py` (delete the Task-9 block, lines ~252-347)

**Interfaces:**
- Consumes: existing `_spec_from_fields(...)` helper and 2-column symbol
  table (both pre-date the IB feature).
- Produces: `SlaveEditor.spec()` always returns an MT5 `AccountSpec` (no
  `platform` kwarg passed — the dataclass default still exists until Task 2);
  `SlaveEditor` has no `platform`, `ib_host`, `ib_port`, `ib_client_id`
  attributes. Later tasks rely on this shape.

- [x] **Step 1: Delete the IB editor UI.** In `_build_ui`: remove the
  `self.platform` QComboBox and its two `addItem` lines, its
  `form.addRow("Slave platform", self.platform)`, the
  `self.ib_host/ib_port/ib_client_id` QLineEdits and their three addRow
  calls; in `__init__` remove `self._on_platform_changed()`; in the
  connect section remove `self.platform.currentIndexChanged.connect(...)`;
  delete the `_on_platform_changed` method, `_IB_HEADERS`, and
  `_contract_map` entirely.
- [x] **Step 2: Delete the IB branches in `set_spec` and `spec`.** In
  `set_spec`: remove the `idx = self.platform.findData(...)` /
  `setCurrentIndex` / `self._on_platform_changed()` block, the
  `if spec.platform == "ib":` contract-table fill block, the F5 comment +
  `self.platform.setEnabled(not lock_identity)` line, and the three
  `self.ib_*.setText(...)` lines. In `spec()`: delete the `platform` /
  `if platform == "ib":` return branch, returning the `_spec_from_fields(...)`
  result unconditionally; delete the now-unused `platform` variable.
- [x] **Step 3: Strip the demo sentence from the install label.** In
  `main_window.py`, the label text becomes:
  ```python
  self.install_disclaimer_label = QLabel(
      "Install MetaTrader opens the download page. Download and run "
      "mt5setup.exe, and choose a CUSTOM install path for each terminal "
      "— the default path collides with existing terminals.")
  ```
- [x] **Step 4: Delete the IB test block.** In `test_slave_editor.py`, delete
  the section comment `# --- Task 9: platform picker + IB contract table`
  and every test under it (`test_editor_platform_mt5_default`,
  `test_editor_ib_mode_hides_terminal_shows_ib_fields`,
  `test_editor_ib_spec_roundtrip`, `test_editor_edit_existing_ib_spec`,
  `test_set_spec_locks_platform_combo_and_carries_ib_connection`). Keep all
  earlier tests untouched.
- [x] **Step 5: Run the suite**
  Run: `"C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests -q`
  Expected: PASS (`test_slave_editor.py` and `test_main_window.py` green)
- [x] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(gui): drop the IB slave editor surface

Remove the MT5/IB platform combo, IB host/port/client-id fields, and the
5-column contract table from the slave editor; strip the demo-only
sentence from the install disclaimer label.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 2: Controller MT5-only + loader drop-at-load compat

**Files:**
- Modify: `manager/app/controller.py`
- Modify: `manager/gui/main_window.py` (`_load_config` slave loop)
- Test: `manager/tests/test_controller.py` (delete IB block), `manager/tests/test_main_window.py` (new compat test)

**Interfaces:**
- Consumes: Task 1's editor shape (no IB specs are ever constructed by the GUI).
- Produces: `AccountSpec` with NO `platform`, `ib_host`, `ib_port`,
  `ib_client_id`, `contract_map` fields; `prepare()` treats every slave as
  MT5; `build_worker_configs()` never emits a `platform`/`ib_*` key;
  `apply_slave_edit()` calls `reconfigure_slave(slave_id,
  spec.symbol_map_csv, spec.normalize_sltp)` unconditionally. Task 3's
  supervisor signature matches exactly that three-argument call.

- [x] **Step 1: Write the failing loader test.** In `test_main_window.py`,
  add (it fails until Step 2 adds the drop clause; if it passes immediately,
  verify the drop clause is genuinely absent in `_load_config` before
  trusting green — a passing-fail-invalid test is a plan failure):

```python
def test_load_config_drops_saved_ib_slave_and_keeps_mt5(qapp, tmp_path):
    """A v0.1.26 settings.json with an IB slave loads on the MT5-only build:
    the IB slave is dropped in memory (one log line, no crash, the file is
    not rewritten); mt5 slaves survive."""
    import json
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    path = tmp_path / "settings.json"
    cfg = {"master": {"terminal_path": "C:/m/terminal64.exe"},
           "slaves": [
               {"id": "ib1", "terminal_path": None,
                "symbol_map_csv": "US30=YM", "platform": "ib",
                "ib_host": "127.0.0.1", "ib_port": 4002, "ib_client_id": 7,
                "contract_map": {}},
               {"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                "symbol_map_csv": "", "step_amount": 100.0,
                "step_size": 0.01, "max_lot": 10.0,
                "max_trade_age_minutes": 10.0, "normalize_sltp": True}]}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    store = SettingsStore(path=path)
    w = MainWindow(FakeController(), store=store)
    assert [x.id for x in w._slaves] == ["s1"]
    assert w.slave_list.count() == 1
    assert "ib1" in w.log_view.toPlainText()
```

  If `SettingsStore` has no plain-path constructor matching this usage,
  follow exactly how `test_config_round_trip_restores_master_and_slaves`
  obtains its store and adapt the creation — the assertion contract stays.

- [x] **Step 2: Make the loader drop IB slaves.** In `main_window._load_config`'s
  slave loop, immediately after the `isinstance` dict check and before the
  `AccountSpec.__dataclass_fields__` extraction, insert:

```python
            if str(s.get("platform") or "").lower() == "ib":
                self.append_log(
                    f"slave {s.get('id', '?')} dropped: platform 'ib' no "
                    "longer supported (saved by an older manager version)")
                continue
```

- [x] **Step 3: Strip the IB surface from `controller.py`.** In `AccountSpec`,
  delete the comment + the `platform`, `ib_host`, `ib_port`, `ib_client_id`,
  `contract_map` fields (lines 46-52). In `prepare()`: delete the "IB slaves
  are excluded" docstring sentence, the `if master.platform == "ib": raise`
  check, and the `if s.platform == "ib": continue` skip. In
  `build_worker_configs()`: delete the whole `if s.platform == "ib":` branch
  (keeping the docstring's byte-identical note); in `start()` replace
  `cfgs[s.id].get("terminal_path", f"{s.ib_host}:{s.ib_port}")` with
  `cfgs[s.id].get("terminal_path", "?")`. In `apply_slave_edit()`: replace
  the `if spec.platform == "ib": ... else: ...` pair with the single
  unconditional call
  `self._supervisor.reconfigure_slave(slave_id, spec.symbol_map_csv, spec.normalize_sltp)`.
  Delete the `sup.on_slave_status = lambda ...` wiring (the supervisor-side
  detail plumbing dies in Task 3).

- [x] **Step 4: Delete the IB test bodies.** In `test_controller.py`: delete
  the section comment `# ---- IB platform plumbing (AccountSpec.platform /
  IB worker config) ----`, `_ib_spec()`, `test_build_worker_configs_ib_slave`,
  and `test_prepare_skips_ib_slaves_for_terminal_assignment`. Keep
  `test_build_worker_configs_mt5_unchanged` as-is. Then grep
  `test_controller.py` for any remaining `platform == "ib"`, `on_slave_status`,
  or `apply_slave_edit.*platform` test bodies and delete those too, keeping
  their MT5 siblings.

- [x] **Step 5: Run the suite** (command as in Task 1). Expected: PASS, and
  `grep -n "platform" manager/app/controller.py` shows only the
  byte-identical docstring note.
- [x] **Step 6: Commit**

```bash
git add manager/app/controller.py manager/gui/main_window.py \
        manager/tests/test_controller.py manager/tests/test_main_window.py
git commit -m "refactor(controller): AccountSpec is MT5-only; saved IB slaves drop at load

The loader drops a v0.1.26 'ib'-platform slave in memory with one log
line, never rewriting the file.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3: Supervisor MT5-only + delete the IB worker package

**Files:**
- Modify: `manager/supervisor.py`
- Delete: `manager/worker/ib/contracts.py`, `manager/worker/ib/tags.py`,
  `manager/worker/ib/adapter.py`, `manager/worker/ib/worker.py`
- Test: `manager/tests/test_supervisor.py` (IB bodies out, dispatch test in);
  Delete: `manager/tests/test_ib_adapter.py`, `manager/tests/test_ib_contracts.py`,
  `manager/tests/test_ib_tags.py`, `manager/tests/test_ib_worker.py`

**Interfaces:**
- Consumes: Task 2's controller — configs never carry `platform`/`ib_*` keys;
  `apply_slave_edit` calls `reconfigure_slave(slave_id, symbol_map_csv, normalize_sltp)`.
- Produces: `Supervisor._worker_target(config)` returns the MT5
  `worker_main` for every config; `Supervisor.reconfigure_slave(slave_id,
  symbol_map_csv, normalize_sltp)` (no contracts/ib params);
  `WorkerHandle` has no `detail` attribute and `Supervisor` no
  `on_slave_status`. `manager/ipc/messages.py` keeps `StatusMsg.detail`
  unchanged (Global Constraint).

- [x] **Step 1: Write the failing dispatch test.** In `test_supervisor.py`,
  add near the other `_worker_target`-adjacent tests:

```python
def test_worker_target_is_always_mt5():
    """The MT5 worker is the only reachable target — a legacy config even
    carrying a stale 'platform' key must still spawn the MT5 worker."""
    sup = Supervisor(engine=None, status_cb=lambda *_: None)
    from manager.worker.mt5_worker import worker_main
    assert sup._worker_target({}) is worker_main
    assert sup._worker_target({"platform": "ib"}) is worker_main
    assert sup._worker_target({"platform": "mt5"}) is worker_main
```

  (Match the actual constructor idiom used by the file's other tests for
  building a `Supervisor`; assertion contract stays.)
  Run: `pytest manager/tests/test_supervisor.py::test_worker_target_is_always_mt5 -q`
  Expected: FAIL (today `_worker_target` imports the IB worker for `"ib"`).

- [x] **Step 2: Make the supervisor MT5-only.** In `_worker_target`:

```python
    def _worker_target(self, config):
        """MT5 is the only worker target. Legacy configs carrying a platform
        key spawn MT5 too — configs are no longer platform-tagged."""
        return worker_main
```

  In `reconfigure_slave`: restore the pre-IB signature
  `(self, slave_id, symbol_map_csv, normalize_sltp)`, delete the
  `contracts`/`ib_host`/`ib_port`/`ib_client_id` parameters, the
  `if contracts:` block, the three `if ib_* is not None:` injections, and
  rewrite the docstring to the MT5-only behavior. Delete
  `WorkerHandle.detail`, `self.on_slave_status = None`, and the
  change-gated `if (msg.detail and h is not None and h.detail != msg.detail ...)`
  branch in `_dispatch_slave` (keep the plain status handling around it).

- [x] **Step 3: Delete the package and its tests.**
  `git rm -r manager/worker/ib manager/tests/test_ib_adapter.py
   manager/tests/test_ib_contracts.py manager/tests/test_ib_tags.py
   manager/tests/test_ib_worker.py`

- [x] **Step 4: Trim `test_supervisor.py`.** Delete
  `test_spawn_ib_slave_uses_ib_worker`, `test_reconfigure_ib_slave_sends_contracts`,
  `test_reconfigure_ib_slave_carries_connection_params_into_config`,
  `_ib_status_handle()`, `test_slave_status_detail_forwarded_when_changed`,
  and `test_mt5_status_never_forwards_detail`. Trim
  `test_reconfigure_slave_sends_message_and_updates_config`,
  `test_reconfigure_slave_updates_config_even_when_pipe_gone`, and
  `test_reconfigure_slave_noop_when_handle_missing` to the three-argument
  signature (drop every `contracts=`/`ib_` call and assertion).

- [x] **Step 5: Run the suite.** Expected: PASS, and
  `grep -rn "worker.ib\|ib_async" manager/` is empty.
- [x] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(supervisor): mt5-only worker dispatch; delete the IB worker package

reconfigure_slave returns to its pre-IB three-argument signature; the
StatusMsg.detail GUI plumbing and WorkerHandle.detail go with the IB
worker. IPC message shapes are unchanged (StatusMsg.detail stays).

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 4: Update helper + dependency removal

**Files:**
- Modify: `manager/update_helper.py:118-160` (delete `_ensure_ib_async` + call site)
- Modify: `pyproject.toml:13` (drop `"ib_async==2.1.0",`)
- Test: `manager/tests/test_update_helper.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_reinstall(wheel)` performs exactly one pip command (the
  `--no-deps` wheel install); no `ib_async` string anywhere in the tree's
  product code.

- [x] **Step 1: Write the failing test.** In `test_update_helper.py`, replace
  `test_reinstall_ensures_ib_async_after_wheel` with:

```python
def test_reinstall_runs_wheel_install_only(tmp_path, monkeypatch):
    """The update is one pip command: the --no-deps wheel install. No extra
    install step runs after it (the removed ib_async ensure step must not
    come back via old call sites)."""
```

  with a fake `subprocess.run` recorder asserted to have seen exactly one
  command built from `[sys.executable, "-m", "pip", "install", "--no-deps",
  "--force-reinstall", wheel]` (match the exact list the real call site
  builds — read `_reinstall` first and assert the recorder's single command
  equals it).
- [x] **Step 2: Run it.** Expected: FAIL (`_ensure_ib_async` still issues a
  second pip command).
- [x] **Step 3: Delete `_ensure_ib_async` and its call in `_reinstall`'s
  else-branch.** In `test_update_helper.py`, fix the stale comment in
  `test_reinstall_passes_valid_wheel_filename_to_pip` (the "the helper
  installs ib_async in a" comment fragment — delete it and its continuation
  line). Run the suite. Expected: PASS
- [x] **Step 4: Drop the dependency line** `\"ib_async==2.1.0\",` from
  `pyproject.toml`'s `dependencies`.
- [x] **Step 5: Run the suite.** Expected: PASS
- [x] **Step 6: Commit**

```bash
git add manager/update_helper.py pyproject.toml manager/tests/test_update_helper.py
git commit -m "refactor(update): drop the ib_async ensure step and dependency

The in-app update is again a single --no-deps wheel install; an existing
venv's leftover ib_async package is inert and left alone.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 5: Docs — IB removal + demo-language removal

**Files:**
- Modify: `README.md`, `docs/smoke-test.md`, `docs/TESTING.md`
- Delete: `docs/smoke-test-ib.md`

**Interfaces:**
- Consumes: the code state after Tasks 1-4 (no IB surface exists).
- Produces: docs that describe the MT5-only product with no IB section,
  pointer, or demo-only mandate.

- [x] **Step 1: README — delete the IB content.** Remove: the
  `For IB slave setup — including how to enable the API in the TWS
  workstation...` pointer block (lines ~54-56), the
  `For a full IB paper run-through, see docs/smoke-test-ib.md.` lines
  (~223-224), and the entire `## TWS / IB Gateway API setup` section
  (through its final `> Read-Only API left on:` blockquote, ~lines 228-277).
- [x] **Step 2: README — strip the demo-only language** (exact replacements):

| Line(s) | Now | Becomes |
|---|---|---|
| ~34-35 (Quick Start 2) | `logged in to a **DEMO account** (never a real account).` | `logged in to the account it will trade.` |
| ~52-53 | `For demo setup, see docs/smoke-test.md` | `For a manual run-through, see docs/smoke-test.md` |
| ~86-88 (Features, launch-for-login bullet) | `via its own UI (demo account), then select` | `via its own UI, then select` |
| ~110-111 (Security model first bullet) | the whole `**Demo accounts only — never a real account.**` bullet | delete the bullet (keep the credentials bullet and the capture-artifacts bullet) |
| ~133-134 (Requirements) | `logged in to (one per account, demo accounts)` | `logged in to (one per account)` |
| ~191-193 (Usage step 2) | `log in to a **DEMO account** (never a real account). The terminal saves the account.` | `log in to the account it will trade. The terminal saves the account.` |
| ~221 | `For a full manual demo run-through (demo accounts only), see` | `For a full manual run-through, see` |
| ~393 (file layout) | `smoke-test.md          Manual demo smoke runbook` | `smoke-test.md          Manual smoke runbook` |
| ~437 (Troubleshooting, slaves-never-ready row) | `confirm the terminal is logged in to a demo account` | `confirm the terminal is logged in via the terminal's own UI / the Launch button` |

  Then run `rg -n "demo|DEMO" README.md` — expected: zero hits (or only
  hits inside `docs/superpowers/` which stay untouched).
- [x] **Step 3: README — update the Testing counts.** Run the suite; put the
  real numbers in the README Testing block (replace the stale `180 passed,
  5 skipped (215 with PySide6)` text with the actual counts of this suite).
- [x] **Step 4: `git rm docs/smoke-test-ib.md`.**
- [x] **Step 5: `docs/smoke-test.md` + `docs/TESTING.md`.** Strip every
  demo-only mandate the same way (e.g. "demo accounts only" framings →
  "validate before trusting a real account" runbook language), update the
  title/line that calls it the *demo* runbook, and keep the MT5 steps
  otherwise untouched. `docs/TESTING.md` gets no IB edits (verify by its own
  grep) — only demo-language edits if it has any.
- [x] **Step 6: Sweep.**
  Run: `rg -n "ib_async|smoke-test-ib|TWS API|IB Gateway|worker/ib" README.md docs/ manager/ scripts/ .github/`
  Expected: zero hits outside `docs/superpowers/` archives and `pyproject.toml`
  name strings; fix anything found.
- [x] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: MetaTrader 5 only — remove IB/TWS sections and demo-only disclaimers

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 6: Whole-suite verification

**Files:** none (verification gate)

- [x] **Step 1: Full suite from the repo root** with the app venv. Expected:
  PASS (expect roughly the v0.1.26 count minus IB tests plus the two new
  tests; record the exact number for the README in Task 5 was already set).
- [x] **Step 2: Import sweep.**
  Run: `"C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -c "import manager.gui.main_window, manager.app.controller, manager.supervisor, manager.update_helper; print('imports ok')"`
  Expected: `imports ok`.
- [x] **Step 3: Grep sweep** (same as Task 5 Step 6, plus
  `rg -n "ib_host|ib_port|ib_client_id|contract_map" manager/ --glob '!docs/**'`
  → zero hits).
- [x] **Step 4: Manual smoke** (the spec's launch step): start the app
  (`"C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/pythonw.exe" -m
  manager` or `python -m manager`), open **Add Slave…** once to confirm the
  editor renders with no Qt warnings, then close the window. If a live MT5
  terminal with a position is available, run one Start/Stop cycle; otherwise
  record the skipped cycle for the release note.
- [x] **Step 5: Commit nothing** (steps 1-4 are verification; if anything
  fails, it is a defect in Tasks 1-5 — fix in the owning file, not here), and
  hand off to the finishing skill.