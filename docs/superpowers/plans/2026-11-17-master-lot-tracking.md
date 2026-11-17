# Master Lot Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make slave lot sizing follow the largest master lot ever observed (per master symbol, persisted across restarts) as an opt-in effective base, never scaling up.

**Architecture:** A thread-safe `LearnedMasterLots` holder in the engine tracks per-symbol maxima from master snapshots. `derive_command` uses `max(configured base, learned lot)` when a slave opts in, then the unchanged `calculate_slave_lot` tail does the work. The controller loads/persists the values in `settings.json` and marshals updates to the GUI; the profile/slave editor gains an opt-in checkbox and the Master pane gains a label plus reset button.

**Tech Stack:** Python 3.11+, PySide6 (GUI), pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-master-lot-tracking-design.md`

## Global Constraints

- Tests run with the app venv interpreter: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest ...` (pytest is not on PATH; the system Python lacks pytest/PySide6). GUI tests use the `qapp` fixture.
- Work directly on `main` (standing user instruction: no worktrees, no feature branches).
- `calculate_slave_lot` and `_snap_clamp` are **not modified**. The effective base is computed at the `derive_command` call site.
- Scaling stays **down-only**: the slave lot never exceeds the configured fixed/balance-step lot, and Max Lots remains the universal cap.
- The learned values are used only when `SlaveConfig.follow_master_lot` is `True`; the default `False` keeps every existing configuration behaviour-identical.
- Tracking and persistence run regardless of whether a slave opted in; the values are inert otherwise.
- The learned holder is thread-safe (`threading.Lock`); `SettingsStore` serializes every read-modify-write with one internal `threading.RLock` so GUI saves and tracking saves cannot clobber each other.
- Terminal identity is the normalized exe path actually assigned this run (`assigned[master.id].exe_path`); compare with `os.path.normpath(path).lower()` semantics (same as `TerminalManager._norm`).
- A terminal change is applied in memory for the run but committed to `settings.json` only after `sup.start()` succeeds; a failed Start leaves the old tracking intact.
- Reset suppresses re-learning from master tickets that were open at reset time until they close.
- No worker/IPC changes; sizing stays manager-side.

## Review Focus

1. **Reset while the large master position is still open** — the reset must stick; the still-open ticket must not re-learn the old base on the next tick.
2. **A failed Start must not delete the previous terminal's learned lots** — readiness/Algo/spawn failures leave `settings.json` untouched.
3. **The same terminal path written with different separators or case** — must NOT drop the learned values on every Start.
4. **A GUI config save and a learned-lots save in either order** — must preserve both the `config` and `global.master_lot_tracking` keys.
5. **Tracking off** — sizing must be byte-identical to today (configured base only, no up-scaling).

Each is pinned by a test in the task that owns the code (1 → Tasks 1 and 3; 2, 3 → Task 4; 4 → Task 2; 5 → Task 3).

---

## File Structure

- `manager/engine/learned_lots.py` — new thread-safe per-symbol maximum holder. (Task 1)
- `manager/tests/test_learned_lots.py` — new unit tests. (Task 1)
- `manager/settings/store.py` — tracking load/save + one `RLock` for every read-modify-write. (Task 2)
- `manager/tests/test_settings_store.py` — persistence tests. (Task 2)
- `manager/engine/copy_loop.py` — engine wiring, effective base, follow flag. (Task 3)
- `manager/tests/test_copy_loop.py` — engine/sizing tests. (Task 3)
- `manager/app/controller.py` — spec/profile field, start/stop tracking flow, reset, callback. (Task 4)
- `manager/tests/test_controller.py` — controller tests. (Task 4)
- `manager/gui/profile_editor.py` — opt-in checkbox + visibility. (Task 5)
- `manager/tests/test_profile_editor.py`, `manager/tests/test_slave_editor.py` — GUI tests. (Task 5)
- `manager/gui/main_window.py`, `manager/__main__.py` — Master pane label/reset + bridge signal. (Task 6)
- `manager/tests/test_main_window.py`, `manager/tests/test_main_entry.py` — GUI wiring tests. (Task 6)
- `README.md`, `.github/workflows/release.yml` — docs and release notes. (Task 7)

---

### Task 1: Thread-safe learned-lots holder

**Files:**
- Create: `manager/engine/learned_lots.py`
- Test: `manager/tests/test_learned_lots.py`

**Interfaces:**
- Produces:
  - `class LearnedMasterLots` in `manager/engine/learned_lots.py`
  - `LearnedMasterLots(initial: dict[str, float] | None = None)`
  - `update(positions: Iterable[Position]) -> bool` — records per-`Position.symbol` maxima; returns True when any value grew.
  - `get(symbol: str) -> float` — 0.0 when unknown.
  - `as_dict() -> dict[str, float]` — a copy.
  - `reset(open_tickets: Iterable[int]) -> None` — clears values; suppresses the given tickets until they disappear from a later snapshot.
- Consumes: `manager.engine.models.Position`.

- [ ] **Step 1: Write the failing tests**

Create `manager/tests/test_learned_lots.py`:

```python
from manager.engine.learned_lots import LearnedMasterLots
from manager.engine.models import Position, BUY

NOW = 1700000000


def _pos(ticket, symbol="EURUSD", volume=0.5):
    return Position(ticket=ticket, symbol=symbol, side=BUY, open_price=1.1,
                    volume=volume, sl=0.0, tp=0.0, open_time=NOW, point=0.00001)


def test_update_learns_largest_volume_per_symbol():
    lots = LearnedMasterLots()
    assert lots.update([_pos(1, volume=10.0)]) is True
    assert lots.update([_pos(2, volume=23.0)]) is True
    assert lots.update([_pos(3, volume=5.0)]) is False
    assert lots.as_dict() == {"EURUSD": 23.0}


def test_update_keeps_symbols_isolated():
    lots = LearnedMasterLots()
    lots.update([_pos(1, "EURUSD", 23.0), _pos(2, "XAUUSD", 0.5)])
    assert lots.get("EURUSD") == 23.0
    assert lots.get("XAUUSD") == 0.5
    assert lots.get("GBPUSD") == 0.0


def test_update_ignores_non_finite_and_non_positive_volumes():
    lots = LearnedMasterLots()
    lots.update([_pos(1, volume=float("nan")), _pos(2, volume=float("inf")),
                 _pos(3, volume=0.0), _pos(4, volume=-5.0)])
    assert lots.as_dict() == {}


def test_initial_values_are_validated():
    lots = LearnedMasterLots({"EURUSD": 23.0, "XAUUSD": float("nan"),
                              "GBPUSD": -1.0, "": 5.0})
    assert lots.as_dict() == {"EURUSD": 23.0}


def test_as_dict_returns_a_copy():
    lots = LearnedMasterLots({"EURUSD": 23.0})
    copy = lots.as_dict()
    copy["EURUSD"] = 99.0
    assert lots.get("EURUSD") == 23.0


def test_reset_clears_and_suppresses_open_tickets_until_they_close():
    lots = LearnedMasterLots({"EURUSD": 23.0})
    lots.reset(open_tickets={7})
    assert lots.as_dict() == {}
    # ticket 7 is still open: it does not re-learn
    assert lots.update([_pos(7, volume=23.0)]) is False
    assert lots.as_dict() == {}
    # once ticket 7 disappears, a fresh large trade learns again
    lots.update([_pos(8, volume=10.0)])
    assert lots.get("EURUSD") == 10.0


def test_reset_frees_suppression_when_the_ticket_disappears():
    lots = LearnedMasterLots({"EURUSD": 23.0})
    lots.reset(open_tickets={7})
    lots.update([])  # ticket 7 is gone
    assert lots.update([_pos(7, volume=23.0)]) is True
    assert lots.get("EURUSD") == 23.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_learned_lots.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'manager.engine.learned_lots'`.

- [ ] **Step 3: Implement `LearnedMasterLots`**

In `manager/engine/learned_lots.py`, implement the class with:
- `self._lock = threading.Lock()`, `self._lots: dict[str, float] = {}`, `self._suppressed: set[int] = set()`.
- `update`: under the lock, first `self._suppressed &= {p.ticket for p in positions}` (drop closed tickets), then for each position **not** in `_suppressed` with `math.isfinite(p.volume)` and `p.volume > 0` and `p.volume > self._lots.get(p.symbol, 0.0)`, store it and set `grew = True`. Return `grew`.
- `get`: under the lock, `self._lots.get(symbol, 0.0)`.
- `as_dict`: under the lock, `dict(self._lots)`.
- `reset`: under the lock, `self._lots.clear()`, `self._suppressed = set(open_tickets)`.
- `__init__` validates `initial` with the same finite/positive rule, ignoring non-str or empty keys, and calls `update`-style growth.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_learned_lots.py -q`

Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/learned_lots.py manager/tests/test_learned_lots.py
git commit -m "feat(engine): add thread-safe master lot tracker"
```

---

### Task 2: SettingsStore tracking persistence and write serialization

**Files:**
- Modify: `manager/settings/store.py`
- Test: `manager/tests/test_settings_store.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `SettingsStore.load_master_lot_tracking() -> tuple[str, dict[str, float]]`
  - `SettingsStore.save_master_lot_tracking(terminal_path: str, lots: Mapping[str, float]) -> None`
  - All `SettingsStore` read-modify-write methods (`save`, `save_config`, `save_master_lot_tracking`, and any add/remove helper) run under one internal `threading.RLock`.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_settings_store.py`:

```python
def test_master_lot_tracking_round_trip(tmp_path):
    store = _store(tmp_path)
    store.save_master_lot_tracking("C:/m/terminal64.exe",
                                   {"EURUSD": 23.0, "XAUUSD": 0.5})
    assert store.load_master_lot_tracking() == (
        "C:/m/terminal64.exe", {"EURUSD": 23.0, "XAUUSD": 0.5})


def test_master_lot_tracking_missing_or_corrupt_degrades(tmp_path):
    store = _store(tmp_path)
    assert store.load_master_lot_tracking() == ("", {})
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"global": {"master_lot_tracking": "nope"}}),
                 encoding="utf-8")
    assert SettingsStore(path=p).load_master_lot_tracking() == ("", {})


def test_master_lot_tracking_drops_invalid_entries(tmp_path):
    store = _store(tmp_path)
    store.save({"global": {"master_lot_tracking": {
        "terminal_path": "C:/m/terminal64.exe",
        "lots": {"EURUSD": 23.0, "BAD": "x", "NAN": float("nan"),
                 "NEG": -1.0, "BOOL": True, "": 5.0}}}})
    assert store.load_master_lot_tracking() == ("C:/m/terminal64.exe",
                                                {"EURUSD": 23.0})


def test_config_and_tracking_saves_do_not_clobber_each_other(tmp_path):
    store = _store(tmp_path)
    store.save_config({"master": {"terminal_path": "C:/m/terminal64.exe"}})
    store.save_master_lot_tracking("C:/m/terminal64.exe", {"EURUSD": 23.0})
    assert store.load_config()["master"]["terminal_path"] == "C:/m/terminal64.exe"
    assert store.load_master_lot_tracking() == (
        "C:/m/terminal64.exe", {"EURUSD": 23.0})
    # a later config save keeps the tracking key
    store.save_config({"master": {"terminal_path": "C:/other/terminal64.exe"}})
    assert store.load_master_lot_tracking() == (
        "C:/m/terminal64.exe", {"EURUSD": 23.0})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_settings_store.py -q`

Expected: FAIL with `AttributeError: 'SettingsStore' object has no attribute 'load_master_lot_tracking'`.

- [ ] **Step 3: Implement the store changes**

- Add `import threading`, `import math`, and `from collections.abc import Mapping`.
- In `__init__`: `self._lock = threading.RLock()`.
- Split the current `load`/`save` bodies into `_load_unlocked`/`_save_unlocked`; make `load`/`save` acquire the lock and delegate (RLock keeps `save_config` reentrant).
- `save_config`: inside the lock, `_load_unlocked()` → set `data["config"]` → `_save_unlocked(data)`.
- `load_master_lot_tracking`: read `data["global"]["master_lot_tracking"]`; return `("", {})` unless it is a dict; read `terminal_path` only if str; keep each `lots` entry whose key is a non-empty str and whose value is a finite float `> 0` (reject bools).
- `save_master_lot_tracking`: inside the lock, `_load_unlocked()`, `data.setdefault("global", {})["master_lot_tracking"] = {"terminal_path": terminal_path, "lots": {clean entries}}`, `_save_unlocked(data)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_settings_store.py -q`

Expected: PASS (all existing plus 4 new).

- [ ] **Step 5: Commit**

```bash
git add manager/settings/store.py manager/tests/test_settings_store.py
git commit -m "feat(settings): persist learned master lots under a store lock"
```

---

### Task 3: Engine wiring and effective base

**Files:**
- Modify: `manager/engine/copy_loop.py`
- Test: `manager/tests/test_copy_loop.py`

**Interfaces:**
- Consumes: `LearnedMasterLots` from Task 1.
- Produces:
  - `SlaveConfig.follow_master_lot: bool = False`
  - `SlaveState.learned_lots: LearnedMasterLots` (default new instance; the engine replaces it with its shared holder in `add_slave`)
  - `CopyEngine(learned_lots: LearnedMasterLots | None = None, on_learned_lots_changed: Callable[[dict[str, float]], None] | None = None)`
  - `CopyEngine.learned_master_lots() -> dict[str, float]`
  - `CopyEngine.open_master_tickets() -> set[int]`
  - `CopyEngine.reset_learned_master_lots(open_tickets: Iterable[int]) -> None`
  - `update_slave_config(..., follow_master_lot: bool = False)`

- [ ] **Step 1: Write the failing tests**

Extend the existing `_pos` helper in `manager/tests/test_copy_loop.py` by adding a trailing `symbol="EURUSD"` parameter and passing `symbol=symbol` into `Position`, then append the new tests (also add `LearnedMasterLots` to that file's imports):

```python
def _sizing_cfg(**kw):
    base = dict(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                step_amount=100.0, step_size=0.01, max_lot=10.0,
                max_trade_age_minutes=10, normalize_sltp=True,
                sizing_mode="fixed_lot", master_base_lot=10.0, fixed_lot=0.1,
                follow_master_lot=True)
    base.update(kw)
    return SlaveConfig(**base)


def test_ingest_learns_largest_lot_per_symbol_and_reports_it():
    seen = []
    eng = CopyEngine(on_learned_lots_changed=seen.append)
    eng.add_slave(_sizing_cfg())
    eng.apply_symbol_info("s1", {"EURUSD": SI, "XAUUSD": SI})
    eng.ingest_snapshot(_snap([_pos(1, volume=23.0),
                               _pos(2, volume=0.5, symbol="XAUUSD")]), now=NOW)
    assert eng.learned_master_lots() == {"EURUSD": 23.0, "XAUUSD": 0.5}
    assert seen[-1] == {"EURUSD": 23.0, "XAUUSD": 0.5}


def test_follow_master_lot_scales_down_from_the_learned_base_in_the_same_tick():
    eng = _engine(slaves=(_sizing_cfg(),), infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=23.0)]), now=NOW)["s1"]
    assert cmds[0].volume == 0.1            # learned base 23 -> full fixed lot
    cmds = eng.ingest_snapshot(_snap([_pos(43, volume=10.0)]), now=NOW)["s1"]
    assert cmds[0].volume == 0.04           # 0.1 * 10/23 = 0.043 -> snap down


def test_follow_off_uses_the_configured_base_only():
    eng = _engine(slaves=(_sizing_cfg(follow_master_lot=False),),
                  infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(1, volume=23.0)]), now=NOW)  # learns 23
    cmds = eng.ingest_snapshot(
        _snap([_pos(1, volume=23.0), _pos(2, volume=10.0)]), now=NOW)["s1"]
    assert cmds[0].volume == 0.1            # base stays the configured 10


def test_effective_base_is_the_max_of_configured_and_learned():
    eng = _engine(slaves=(_sizing_cfg(master_base_lot=20.0),),
                  infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(1, volume=10.0)]), now=NOW)["s1"]
    assert cmds[0].volume == 0.05           # base 20, 10/20


def test_follow_is_per_master_symbol():
    eng = _engine(
        slaves=(_sizing_cfg(symbol_map_csv="EURUSD=EURUSD,XAUUSD=XAUUSD"),),
        infos={"s1": {"EURUSD": SI, "XAUUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(1, volume=23.0)]), now=NOW)
    cmds = eng.ingest_snapshot(
        _snap([_pos(1, volume=23.0),
               _pos(2, volume=0.5, symbol="XAUUSD")]), now=NOW)["s1"]
    assert cmds[0].volume == 0.1            # XAUUSD base 0.5, not EURUSD's 23


def test_reset_learned_master_lots_suppresses_open_tickets():
    eng = _engine(slaves=(_sizing_cfg(),), infos={"s1": {"EURUSD": SI}})
    eng.ingest_snapshot(_snap([_pos(1, volume=23.0)]), now=NOW)
    eng.reset_learned_master_lots(eng.open_master_tickets())
    assert eng.learned_master_lots() == {}
    eng.ingest_snapshot(_snap([_pos(1, volume=23.0)]), now=NOW)
    assert eng.learned_master_lots() == {}  # still-open ticket ignored


def test_update_slave_config_patches_follow_master_lot():
    eng = _engine(infos={"s1": {"EURUSD": SI}})
    eng.update_slave_config("s1", step_amount=100.0, step_size=0.01,
                            max_lot=10.0, max_trade_age_minutes=10,
                            symbol_map_csv="EURUSD=EURUSD", normalize_sltp=True,
                            sizing_mode="fixed_lot", master_base_lot=10.0,
                            fixed_lot=0.1, follow_master_lot=True)
    assert eng._slaves["s1"].config.follow_master_lot is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_copy_loop.py -q`

Expected: FAIL — `SlaveConfig` has no `follow_master_lot`, `CopyEngine` rejects the new kwargs, and the sizing tests return 0.1 where 0.04/0.05 is expected.

- [ ] **Step 3: Implement the engine changes**

- `SlaveConfig`: add `follow_master_lot: bool = False`.
- `SlaveState`: add `learned_lots: LearnedMasterLots = field(default_factory=LearnedMasterLots)` after `mapper`.
- `derive_command` NEW branch: compute
  ```python
  base = cfg.master_base_lot
  if cfg.follow_master_lot:
      base = max(base, state.learned_lots.get(pos.symbol))
  ```
  and pass `base` in place of `cfg.master_base_lot` to `calculate_slave_lot`.
- `CopyEngine.__init__`: accept the two new optional args; `self._learned = learned_lots or LearnedMasterLots()`; `self._on_learned = on_learned_lots_changed or (lambda lots: None)`.
- `add_slave`: pass `learned_lots=self._learned` into `SlaveState(...)`.
- `ingest_snapshot`: **before** the per-slave loop, `if self._learned.update(snapshot.positions): self._on_learned(self._learned.as_dict())`.
- Add `learned_master_lots()` → `self._learned.as_dict()`.
- Add `open_master_tickets()` → `{p.ticket for p in list(self._prev)}`.
- Add `reset_learned_master_lots(open_tickets)` → `self._learned.reset(open_tickets)` then `self._on_learned(self._learned.as_dict())`.
- `update_slave_config`: add the keyword and assign `cfg.follow_master_lot = follow_master_lot`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_copy_loop.py manager/tests/test_transform.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/engine/copy_loop.py manager/tests/test_copy_loop.py
git commit -m "feat(engine): follow the learned master lot as an opt-in base"
```

---

### Task 4: Controller plumbing, persistence flow, and reset

**Files:**
- Modify: `manager/app/controller.py`
- Test: `manager/tests/test_controller.py`

**Interfaces:**
- Consumes: `LearnedMasterLots` (Task 1), `SettingsStore.load_master_lot_tracking` / `save_master_lot_tracking` (Task 2), `SlaveConfig.follow_master_lot` and `CopyEngine` reset/learned API (Task 3).
- Produces:
  - `AccountSpec.follow_master_lot: bool = False`
  - `CopyProfile.follow_master_lot: bool = False`
  - `"follow_master_lot"` in `SHARED_PROFILE_FIELDS`
  - `CopyController(..., on_learned_lots_changed=None)`
  - `CopyController.build_supervisor(heartbeat_seconds=5, learned_lots=None, on_learned_lots_changed=None)`
  - `CopyController.reset_learned_master_lots() -> None`

- [ ] **Step 1: Update the test helper and write the failing tests**

In `manager/tests/test_controller.py`, add `import tempfile` and `from pathlib import Path`, `from manager.settings.store import SettingsStore`, then change `_controller` to accept a store that defaults to a temp file (so no test writes the real APPDATA settings):

```python
def _controller(instances, store=None):
    statuses, logs = [], []
    if store is None:
        store = SettingsStore(path=Path(tempfile.mkdtemp()) / "settings.json")
    c = CopyController(
        terminal_manager=FakeTerminalManager(instances),
        store=store,
        on_status=lambda s: statuses.append(s),
        on_log=lambda m: logs.append(m),
    )
    return c, statuses, logs
```

Append the new tests:

```python
def _empty_master_state():
    return {"positions": [], "symbol_infos": {"EURUSD": SI},
            "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                        "currency": "USD", "server": "Demo"}}


def _insts():
    return [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
            TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]


def test_start_passes_follow_master_lot_to_engine():
    store = SettingsStore(path=Path(tempfile.mkdtemp()) / "settings.json")
    c, _, _ = _controller(_insts(), store)
    slave = AccountSpec(id="s1", terminal_path="C:/s/terminal64.exe",
                        symbol_map_csv="EURUSD=EURUSD",
                        follow_master_lot=True)
    c.start(_master(), [slave], master_fake_state=_empty_master_state(),
            slave_fake_state=_slave_state())
    try:
        assert c._engine._slaves["s1"].config.follow_master_lot is True
    finally:
        c.stop()


def test_start_loads_lots_for_the_same_terminal_despite_path_spelling():
    store = SettingsStore(path=Path(tempfile.mkdtemp()) / "settings.json")
    store.save_master_lot_tracking(r"c:\m\terminal64.exe", {"EURUSD": 23.0})
    c, _, _ = _controller(_insts(), store)
    c.start(_master(), [_slave()], master_fake_state=_empty_master_state(),
            slave_fake_state=_slave_state())
    try:
        assert c._engine.learned_master_lots() == {"EURUSD": 23.0}
    finally:
        c.stop()


def test_start_drops_lots_for_a_different_terminal_after_success():
    store = SettingsStore(path=Path(tempfile.mkdtemp()) / "settings.json")
    store.save_master_lot_tracking("C:/old/terminal64.exe", {"EURUSD": 23.0})
    c, _, _ = _controller(_insts(), store)
    c.start(_master(), [_slave()], master_fake_state=_empty_master_state(),
            slave_fake_state=_slave_state())
    try:
        assert c._engine.learned_master_lots() == {}
        assert store.load_master_lot_tracking() == ("C:/m/terminal64.exe", {})
    finally:
        c.stop()


def test_failed_start_keeps_the_previous_tracking():
    store = SettingsStore(path=Path(tempfile.mkdtemp()) / "settings.json")
    store.save_master_lot_tracking("C:/old/terminal64.exe", {"EURUSD": 23.0})
    c, _, _ = _controller(_insts(), store)
    state = dict(_empty_master_state())
    state["terminal_info"] = {"trade_allowed": False}
    with pytest.raises(AlgoTradingDisabledError):
        c.start(_master(), [_slave()], master_fake_state=state,
                slave_fake_state=_slave_state())
    assert store.load_master_lot_tracking() == (
        "C:/old/terminal64.exe", {"EURUSD": 23.0})


def test_learning_persists_and_reset_clears_engine_and_store(tmp_path):
    store = SettingsStore(path=tmp_path / "settings.json")
    c, _, _ = _controller(_insts(), store)
    master_state = {"positions": [Position(42, "EURUSD", BUY, 1.1, 23.0,
                                           0.0, 0.0, NOW, 0.00001, "")],
                    "symbol_infos": {"EURUSD": SI},
                    "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                                "currency": "USD", "server": "Demo"}}
    c.start(_master(), [_slave()], master_fake_state=master_state,
            slave_fake_state=_slave_state())
    try:
        assert _tick_until(
            lambda: store.load_master_lot_tracking()[1].get("EURUSD") == 23.0)
        c.reset_learned_master_lots()
        assert c._engine.learned_master_lots() == {}
        assert store.load_master_lot_tracking() == ("C:/m/terminal64.exe", {})
    finally:
        c.stop()


def test_reset_while_stopped_clears_the_stored_lots():
    store = SettingsStore(path=Path(tempfile.mkdtemp()) / "settings.json")
    store.save_master_lot_tracking("C:/m/terminal64.exe", {"EURUSD": 23.0})
    c, _, _ = _controller(_insts(), store)
    c.reset_learned_master_lots()
    assert store.load_master_lot_tracking() == ("C:/m/terminal64.exe", {})


def test_apply_slave_edit_forwards_follow_master_lot():
    c, _, _ = _controller(_insts())
    c.start(_master(), [_slave()], master_fake_state=_empty_master_state(),
            slave_fake_state=_slave_state())
    try:
        c._supervisor.reconfigure_slave = lambda sid, csv, norm, **kw: None
        new = AccountSpec(id="s1", terminal_path="C:/s/terminal64.exe",
                          symbol_map_csv="EURUSD=EURUSD",
                          follow_master_lot=True)
        c.apply_slave_edit("s1", new)
        assert c._engine._slaves["s1"].config.follow_master_lot is True
    finally:
        c.stop()


def test_coerce_profiles_rejects_non_bool_follow_master_lot():
    from manager.app.controller import coerce_profiles
    raw = [{"id": "p1", "name": "A", "follow_master_lot": "yes"}]
    assert coerce_profiles(raw) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_controller.py -q`

Expected: FAIL — `AccountSpec` has no `follow_master_lot`, and the controller has no tracking/reset methods.

- [ ] **Step 3: Implement the controller changes**

- Dataclasses: add `follow_master_lot: bool = False` to `AccountSpec` and `CopyProfile`; add `"follow_master_lot"` to `SHARED_PROFILE_FIELDS`.
- `coerce_profiles`: after `normalize_sltp`, validate `follow_master_lot` as bool (invalid → drop the profile).
- `__init__`: `on_learned_lots_changed=None`; store it; `self._master_tracking_path = ""`.
- `build_supervisor(..., learned_lots=None, on_learned_lots_changed=None)`: pass both to `CopyEngine(...)`; default the callback to `self._on_learned_lots_changed`.
- `start`:
  - After `assigned = self.prepare(...)`, compute `master_path = assigned[master.id].exe_path` (normalized comparison via a module helper `_same_path(a, b) -> bool` wrapping `os.path.normpath(...).lower()`).
  - `stored_path, stored_lots = self._store.load_master_lot_tracking()`.
  - `same = _same_path(stored_path, master_path)`; `lots = stored_lots if same else {}`.
  - Call `build_supervisor(learned_lots=LearnedMasterLots(lots), on_learned_lots_changed=lambda lots: self._learned_changed(master_path, lots))` — the closure carries the correct path even if a later Start fails.
  - Pass `follow_master_lot=s.follow_master_lot` into `SlaveConfig`.
  - After `sup.start(); self._supervisor = sup`, set `self._master_tracking_path = master_path`, and if `not same`, persist `(master_path, {})` (the in-memory drop is now committed because Start succeeded).
- `_learned_changed(master_path, lots)`: `save_master_lot_tracking(master_path, lots)` (try/except → `self._log`), then `self._on_learned_lots_changed(lots)`.
- `reset_learned_master_lots()`: if `self._engine` is not None, call `self._engine.reset_learned_master_lots(self._engine.open_master_tickets())` (its callback persists + notifies). Else load the stored path; if non-empty, persist `(path, {})`; then call `self._on_learned_lots_changed({})`.
- `apply_slave_edit`: pass `follow_master_lot=spec.follow_master_lot` to `update_slave_config`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_controller.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/app/controller.py manager/tests/test_controller.py
git commit -m "feat(app): load, persist, and reset learned master lots"
```

---

### Task 5: Opt-in checkbox in the settings widget

**Files:**
- Modify: `manager/gui/profile_editor.py`, `manager/gui/slave_editor.py`
- Test: `manager/tests/test_profile_editor.py`, `manager/tests/test_slave_editor.py`

**Interfaces:**
- Consumes: `AccountSpec.follow_master_lot` / `CopyProfile.follow_master_lot` and `SHARED_PROFILE_FIELDS` (Task 4).
- Produces: `CopySettingsWidget.follow_master_lot` (QCheckBox), included in `values()`, `set_values()`, `set_editable()`, and the mode visibility; `SlaveEditor.follow_master_lot` property/setter forwarding to `self.copy_settings.follow_master_lot` (same pattern as `master_base_lot`).

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_profile_editor.py`:

```python
def test_copy_settings_widget_follow_master_lot_round_trip(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    assert w.follow_master_lot.isChecked() is False
    w.follow_master_lot.setChecked(True)
    assert w.values()["follow_master_lot"] is True
    w.set_values({"follow_master_lot": False})
    assert w.follow_master_lot.isChecked() is False
    w.set_editable(False)
    assert not w.follow_master_lot.isEnabled()


def test_copy_settings_widget_follow_master_lot_visibility(qapp):
    from manager.gui.profile_editor import CopySettingsWidget
    w = CopySettingsWidget()
    for mode in ("balance_step", "fixed_lot"):
        w.sizing_mode.setCurrentIndex(w.sizing_mode.findData(mode))
        assert not w.follow_master_lot.isHidden()
    w.sizing_mode.setCurrentIndex(w.sizing_mode.findData("copy_master"))
    assert w.follow_master_lot.isHidden()
```

Append to `manager/tests/test_slave_editor.py`:

```python
def test_slave_editor_follow_master_lot_round_trip(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.terminal.setCurrentIndex(0)
    dlg.follow_master_lot.setChecked(True)
    dlg.accept()
    assert dlg.spec().follow_master_lot is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_profile_editor.py manager/tests/test_slave_editor.py -q`

Expected: FAIL — `CopySettingsWidget` has no `follow_master_lot`.

- [ ] **Step 3: Implement the widget changes**

- In `CopySettingsWidget.__init__`, after the master-base-lot row: `self.follow_master_lot = QCheckBox("Follow the master's largest lot")`; add it with `self._sizing.addRow("", self.follow_master_lot)`.
- `values()`: add `"follow_master_lot": self.follow_master_lot.isChecked()`.
- `set_values()`: if `"follow_master_lot" in data`, `self.follow_master_lot.setChecked(bool(data["follow_master_lot"]))`.
- `set_editable()`: include the checkbox.
- `_update_sizing_visibility()`: `show_row(self.follow_master_lot, is_balance or is_fixed)`.
- `SlaveEditor`: add a `follow_master_lot` property and setter that read/assign `self.copy_settings.follow_master_lot`, matching the existing `master_base_lot` property pair.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_profile_editor.py manager/tests/test_slave_editor.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/profile_editor.py manager/tests/test_profile_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): add follow-master-lot opt-in checkbox"
```

---

### Task 6: Master pane label, reset button, and bridge signal

**Files:**
- Modify: `manager/gui/main_window.py`, `manager/__main__.py`
- Test: `manager/tests/test_main_window.py`, `manager/tests/test_main_entry.py`

**Interfaces:**
- Consumes: `CopyController.reset_learned_master_lots()` and `on_learned_lots_changed` (Task 4), `SettingsStore.load_master_lot_tracking()` (Task 2).
- Produces:
  - `MainWindow.learned_lots_label` (QLabel), `MainWindow.reset_learned_lots_button` (QPushButton)
  - `MainWindow.on_learned_lots(lots: dict[str, float]) -> None`
  - `_StatusBridge.learned = Signal(object)`

- [ ] **Step 1: Update the test double and write the failing tests**

In `manager/tests/test_main_window.py`, add to `FakeController`:

```python
    def __init__(self):
        ...
        self.reset_calls = 0
    def reset_learned_master_lots(self):
        self.reset_calls += 1
```

Append:

```python
def test_main_window_has_learned_lots_widgets(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    assert w.learned_lots_label is not None
    assert w.reset_learned_lots_button is not None
    assert w.reset_learned_lots_button.isEnabled() is False


def test_learned_lots_label_shows_persisted_values(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_master_lot_tracking("C:/m/terminal64.exe",
                                   {"EURUSD": 23.0, "XAUUSD": 0.5})
    w = MainWindow(FakeController(), store=store)
    assert "EURUSD 23" in w.learned_lots_label.text()
    assert w.reset_learned_lots_button.isEnabled() is True


def test_on_learned_lots_updates_the_label(qapp):
    from manager.gui.main_window import MainWindow
    w = MainWindow(FakeController())
    w.on_learned_lots({"EURUSD": 23.0})
    assert "EURUSD 23" in w.learned_lots_label.text()
    w.on_learned_lots({})
    assert "none yet" in w.learned_lots_label.text()


def test_reset_button_calls_controller_and_clears_label(qapp, tmp_path):
    from manager.gui.main_window import MainWindow
    from manager.settings.store import SettingsStore
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save_master_lot_tracking("C:/m/terminal64.exe", {"EURUSD": 23.0})
    c = FakeController()
    w = MainWindow(c, store=store)
    w.reset_learned_lots_button.click()
    assert c.reset_calls == 1
    assert "none yet" in w.learned_lots_label.text()
```

In `manager/tests/test_main_entry.py`, extend `_FakeStore` with
`def load_master_lot_tracking(self): return ("", {})`, then add:

```python
def test_build_app_graph_wires_learned_lots(qapp, monkeypatch):
    import manager.__main__ as entry
    monkeypatch.setattr(entry, "TerminalManager", lambda *a, **k: FakeTerminalManager())
    monkeypatch.setattr(entry, "SettingsStore", lambda *a, **k: _FakeStore())
    w, controller, bridge = entry.build_app_graph(qapp)
    assert hasattr(bridge, "learned")
    assert hasattr(w, "on_learned_lots")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py manager/tests/test_main_entry.py -q`

Expected: FAIL — no `learned_lots_label` / `reset_learned_lots_button` and no bridge signal.

- [ ] **Step 3: Implement the GUI wiring**

- `_build_ui` Master pane, after the terminal row:
  ```python
  self.learned_lots_label = QLabel("Learned master lots: none yet")
  self.learned_lots_label.setWordWrap(True)
  self.reset_learned_lots_button = QPushButton("Reset learned master lots")
  self.reset_learned_lots_button.setEnabled(False)
  mform.addRow("Learned lots", self.learned_lots_label)
  mform.addRow("", self.reset_learned_lots_button)
  self.reset_learned_lots_button.clicked.connect(self._on_reset_learned_lots)
  ```
- After `_load_config()` in `__init__`, call `self._refresh_learned_lots()`.
- `_refresh_learned_lots(self, lots=None)`: when `lots is None`, try
  `self._store.load_master_lot_tracking()[1]` (any exception or missing store →
  `{}`); format `"Learned master lots: " + ", ".join(f"{s} {v:g}" for s, v in sorted(lots.items()))` or `"Learned master lots: none yet"`; set the label; enable the button iff `lots`.
- `on_learned_lots(self, lots)`: `self._refresh_learned_lots(dict(lots))`.
- `_on_reset_learned_lots(self)`: `self._controller.reset_learned_master_lots()`; then `self._refresh_learned_lots({})`.
- `_do_start` success path and `_on_stop`: call `self._refresh_learned_lots()`.
- `manager/__main__.py`: add `learned = Signal(object)` to `_StatusBridge`; pass `on_learned_lots_changed=lambda lots: bridge.learned.emit(lots)` to `CopyController`; connect `bridge.learned.connect(window.on_learned_lots)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_main_window.py manager/tests/test_main_entry.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/main_window.py manager/__main__.py manager/tests/test_main_window.py manager/tests/test_main_entry.py
git commit -m "feat(gui): show and reset learned master lots"
```

---

### Task 7: README and release notes

**Files:**
- Modify: `README.md:85-93` (lot-sizing bullets) and `README.md:229-230` (own-settings summary)
- Modify: `.github/workflows/release.yml:70` (`--notes` string)

**Interfaces:**
- Consumes: nothing (documentation only).

- [ ] **Step 1: Update the README**

After the Fixed lot bullet, add:

```markdown
    Optionally tick **Follow the master's largest lot**: the copier remembers
    the largest lot it has seen per master symbol (persisted across restarts)
    and uses it as the reference base, so a master base that has grown over time
    keeps scaling correctly — once the master's usual lot is 20, a 10-lot trade
    opens half the fixed lot. It never scales up; Max Lots still caps the order.
    The Master pane shows the learned values and offers a reset.
```

Extend the own-settings summary at line 229 so the list mentions the opt-in
follow-master-lot option.

- [ ] **Step 2: Update the release notes**

Prepend to the `--notes` string in `.github/workflows/release.yml`:

```
Master lot tracking (optional): tick "Follow the master's largest lot" on a slave/profile and the copier uses the largest master lot it has observed per symbol as the base, persisted across restarts. Off by default — existing configs are unchanged. The Master pane shows the learned values and has a Reset button.
```

- [ ] **Step 3: Verify the docs**

Run: `git diff -- README.md .github/workflows/release.yml`

Expected: only the intended bullets/notes changed. Then validate YAML:

`"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/release.yml').read_text(encoding='utf-8')); print('YAML OK')"`

- [ ] **Step 4: Run the full suite**

Run: `"C:/Users/a/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md .github/workflows/release.yml
git commit -m "docs: describe optional master lot tracking"
```
