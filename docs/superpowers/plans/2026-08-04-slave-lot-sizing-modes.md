# Slave Lot-Sizing Modes + Master Base Lot Scaling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each slave three selectable lot-sizing modes — balance-step (existing, plus an optional master-base-lot **down-only** scaling), copy-master (mirror the master's lot), and fixed-lot — plumbed through config, engine, GUI, tests, and README, with no worker/IPC changes and full backward compatibility.

**Architecture:** Refactor `calculate_lots` into a shared `_snap_clamp` tail plus a `calculate_slave_lot` mode-dispatcher in `manager/engine/transform.py`. Carry three new fields (`sizing_mode`, `master_base_lot`, `fixed_lot`) through `AccountSpec` → `SlaveConfig` → the `derive_command` call site → the GUI editor. Sizing stays engine-side; the worker still receives only the computed `CommandMsg.volume`.

**Tech Stack:** Python 3, PySide6 (GUI), pytest (tests run under the app venv at `C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe`).

## Global Constraints

- Tests run with the PySide6 venv interpreter: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest ...` (pytest is not on PATH; the system Python lacks pytest/PySide6). GUI tests `pytest.importorskip("PySide6")` and need the `qapp` fixture.
- Work directly on `main` (standing user instruction: no worktrees, no feature branches).
- `MetaTrader5` is not imported by any test in this plan; all sizing tests are pure-unit.
- New `AccountSpec` / `SlaveConfig` fields MUST have defaults so existing `settings.json` files (no new keys) load with today's behavior: `sizing_mode="balance_step"`, `master_base_lot=0.0` (disabled), `fixed_lot=0.01`. The GUI round-trips via `dataclasses.asdict` + `AccountSpec.__dataclass_fields__` filtering (`main_window.py:190`, `main_window.py:217-220`), so no migration code.
- The worker/IPC layer is NOT touched: sizing runs in the manager process; the worker only receives `CommandMsg.volume`. `ReconfigureMsg` is not extended (lot-sizing edits are engine-side, matching how `step_amount`/`step_size`/`max_lot` work today).
- `max_lot` remains a universal cap across all three modes.
- No placeholders, no "similar to Task N" — each task is self-contained.

---

## File Structure

- `manager/engine/transform.py` — add `SIZING_*` mode constants, `_snap_clamp`, `calculate_slave_lot`; rewrite `calculate_lots` as a thin wrapper. (Task 1)
- `manager/tests/test_transform.py` — unit tests for the new sizer. (Task 1)
- `manager/engine/copy_loop.py` — add 3 fields to `SlaveConfig`; switch the `derive_command` call site to `calculate_slave_lot`; extend `update_slave_config`. (Task 2)
- `manager/app/controller.py` — add 3 fields to `AccountSpec`; pass them in `start`'s `SlaveConfig(...)` and in `apply_slave_edit`. (Task 2)
- `manager/tests/test_copy_loop.py`, `manager/tests/test_controller.py`, `manager/tests/test_settings_store.py` — plumbing tests. (Task 2)
- `manager/gui/slave_editor.py` — mode `QComboBox` + two new line edits + visibility toggle; extend `_spec_from_fields`/`set_spec`/`spec`. (Task 3)
- `manager/tests/test_slave_editor.py` — GUI tests for the new widgets + visibility. (Task 3)
- `README.md` — document the three modes and base-lot scaling. (Task 4)

---

## Task 1: Sizer refactor + mode dispatcher (`transform.py`)

**Files:**
- Modify: `manager/engine/transform.py:49-83` (the `calculate_lots` block)
- Test: `manager/tests/test_transform.py` (append new tests; existing `calculate_lots` tests stay)

**Interfaces:**
- Produces:
  - `SIZING_BALANCE_STEP = "balance_step"`, `SIZING_COPY_MASTER = "copy_master"`, `SIZING_FIXED_LOT = "fixed_lot"` (module-level str constants).
  - `_snap_clamp(lots: float, lot_step: float, min_lot: float, max_lot: float, max_lot_symbol: float) -> float`
  - `calculate_slave_lot(mode: str, master_volume: float, balance: float, step_amount: float, step_size: float, master_base_lot: float, fixed_lot: float, max_lot: float, lot_step: float, min_lot: float, max_lot_symbol: float) -> float`
  - `calculate_lots(balance, step_amount, step_size, max_lot, lot_step, min_lot, max_lot_symbol) -> float` (unchanged signature, now a wrapper).
- Consumes: nothing from other tasks (pure unit).

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_transform.py` (after the existing `calculate_lots` tests, before the `import pytest` block at line 82 — i.e. insert these right after `test_lots_invalid_lot_step_returns_zero` at line 79):

```python
from manager.engine.transform import (
    _snap_clamp, calculate_slave_lot,
    SIZING_BALANCE_STEP, SIZING_COPY_MASTER, SIZING_FIXED_LOT,
)


def test_snap_clamp_snaps_down_to_lot_step():
    # 0.07 with lot_step 0.02 -> floor(3.5)*0.02 = 0.06
    assert _snap_clamp(0.07, 0.02, 0.01, 10, 100) == 0.06


def test_snap_clamp_clamps_up_to_min():
    assert _snap_clamp(0.0, 0.01, 0.01, 10, 100) == 0.01


def test_snap_clamp_caps_at_max_lot_and_symbol_max():
    assert _snap_clamp(5.0, 0.01, 0.01, 2.0, 100) == 2.0   # max_lot cap
    assert _snap_clamp(5.0, 0.01, 0.01, 99.0, 0.2) == 0.2  # symbol max cap


def test_snap_clamp_invalid_lot_step_returns_zero():
    assert _snap_clamp(0.5, 0.0, 0.01, 10, 100) == 0.0


def test_calculate_slave_lot_balance_step_no_base_equals_calculate_lots():
    # master_base_lot=0 -> disabled -> identical to legacy calculate_lots
    assert calculate_slave_lot(SIZING_BALANCE_STEP, 0.5, 1000, 100, 0.01,
                               0.0, 0.01, 10, 0.01, 0.01, 100) == 0.10


def test_calculate_slave_lot_balance_step_scales_down_below_base():
    # balance 1000 -> raw_balance 0.10; base 0.1, master 0.05 -> *0.5 -> 0.05
    assert calculate_slave_lot(SIZING_BALANCE_STEP, 0.05, 1000, 100, 0.01,
                               0.1, 0.01, 10, 0.01, 0.01, 100) == 0.05


def test_calculate_slave_lot_balance_step_no_scaling_above_base():
    # master 0.2 >= base 0.1 -> down-only -> raw_balance 0.10 (no scale up)
    assert calculate_slave_lot(SIZING_BALANCE_STEP, 0.2, 1000, 100, 0.01,
                               0.1, 0.01, 10, 0.01, 0.01, 100) == 0.10


def test_calculate_slave_lot_balance_step_below_min_opens_min():
    # raw_balance 0.10 * (0.002/0.1)=0.002 -> snapped to 0.0 -> clamped to min 0.01
    assert calculate_slave_lot(SIZING_BALANCE_STEP, 0.002, 1000, 100, 0.01,
                               0.1, 0.01, 10, 0.01, 0.01, 100) == 0.01


def test_calculate_slave_lot_copy_master_mirrors_master_lot():
    assert calculate_slave_lot(SIZING_COPY_MASTER, 0.37, 1000, 100, 0.01,
                               0.0, 0.01, 10, 0.01, 0.01, 100) == 0.37


def test_calculate_slave_lot_copy_master_below_min_opens_min():
    assert calculate_slave_lot(SIZING_COPY_MASTER, 0.005, 1000, 100, 0.01,
                               0.0, 0.01, 10, 0.01, 0.01, 100) == 0.01


def test_calculate_slave_lot_copy_master_capped_at_max_lot():
    assert calculate_slave_lot(SIZING_COPY_MASTER, 5.0, 1000, 100, 0.01,
                               0.0, 0.01, 2.0, 0.01, 0.01, 100) == 2.0


def test_calculate_slave_lot_fixed_lot_uses_fixed_lot():
    assert calculate_slave_lot(SIZING_FIXED_LOT, 0.5, 1000, 100, 0.01,
                               0.0, 0.07, 10, 0.01, 0.01, 100) == 0.07


def test_calculate_slave_lot_fixed_lot_invalid_returns_zero():
    assert calculate_slave_lot(SIZING_FIXED_LOT, 0.5, 1000, 100, 0.01,
                               0.0, 0.0, 10, 0.01, 0.01, 100) == 0.0


def test_calculate_slave_lot_balance_step_invalid_step_returns_zero():
    assert calculate_slave_lot(SIZING_BALANCE_STEP, 0.5, 1000, 0, 0.01,
                               0.0, 0.01, 10, 0.01, 0.01, 100) == 0.0


def test_calculate_slave_lot_unknown_mode_returns_zero():
    assert calculate_slave_lot("nope", 0.5, 1000, 100, 0.01,
                               0.0, 0.01, 10, 0.01, 0.01, 100) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py -k "snap_clamp or calculate_slave_lot" -v`
Expected: FAIL with `ImportError: cannot import name '_snap_clamp'` (or `calculate_slave_lot`).

- [ ] **Step 3: Implement the sizer**

In `manager/engine/transform.py`, REPLACE the entire `calculate_lots` function (lines 49-83) with the constants, `_snap_clamp`, `calculate_slave_lot`, and a wrapper `calculate_lots`:

```python
SIZING_BALANCE_STEP = "balance_step"
SIZING_COPY_MASTER = "copy_master"
SIZING_FIXED_LOT = "fixed_lot"


def _snap_clamp(lots: float, lot_step: float, min_lot: float,
                max_lot: float, max_lot_symbol: float) -> float:
    """Snap `lots` DOWN to the lot-step grid, clamp UP to min_lot, cap at
    min(max_lot_symbol, max_lot), normalize to the lot-step's digit count.
    Returns 0.0 if lot_step <= 0 (cannot snap). Shared tail for every mode."""
    if lot_step <= 0.0:
        return 0.0
    lots = math.floor(lots / lot_step) * lot_step
    lots = max(lots, min_lot)
    lots = min(lots, max_lot_symbol)
    lots = min(lots, max_lot)
    lot_digits = max(0, int(round(-math.log10(lot_step))))
    return round(lots, lot_digits)


def calculate_slave_lot(mode: str, master_volume: float, balance: float,
                        step_amount: float, step_size: float,
                        master_base_lot: float, fixed_lot: float,
                        max_lot: float, lot_step: float, min_lot: float,
                        max_lot_symbol: float) -> float:
    """Per-slave lot sizing across three modes. Each mode yields a raw lot;
    _snap_clamp then snaps/clamps/caps/rounds it. Returns 0.0 on invalid
    config (the engine skips the trade).

    - balance_step: raw = floor(balance/step_amount)*step_size; if
      master_base_lot > 0 and master_volume < master_base_lot, scale DOWN:
      raw *= master_volume / master_base_lot (never scales up).
    - copy_master: raw = master_volume.
    - fixed_lot: raw = fixed_lot.
    """
    if mode == SIZING_BALANCE_STEP:
        if step_amount <= 0.0 or step_size <= 0.0:
            return 0.0
        raw = math.floor(balance / step_amount) * step_size
        if master_base_lot > 0.0 and master_volume < master_base_lot:
            raw *= master_volume / master_base_lot
        return _snap_clamp(raw, lot_step, min_lot, max_lot, max_lot_symbol)
    if mode == SIZING_COPY_MASTER:
        return _snap_clamp(master_volume, lot_step, min_lot, max_lot,
                           max_lot_symbol)
    if mode == SIZING_FIXED_LOT:
        if fixed_lot <= 0.0:
            return 0.0
        return _snap_clamp(fixed_lot, lot_step, min_lot, max_lot, max_lot_symbol)
    return 0.0


def calculate_lots(balance: float, step_amount: float, step_size: float,
                   max_lot: float, lot_step: float, min_lot: float,
                   max_lot_symbol: float) -> float:
    """Balance-step lot sizing (legacy). Thin wrapper over
    calculate_slave_lot(balance_step, master_base_lot=0.0) so existing callers
    and tests keep working unchanged. Ported from CLotSizer::CalculateLots."""
    return calculate_slave_lot(
        SIZING_BALANCE_STEP, 0.0, balance, step_amount, step_size,
        0.0, 0.0, max_lot, lot_step, min_lot, max_lot_symbol)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_transform.py -v`
Expected: PASS — all new tests green AND the existing `test_lots_*` tests still green (the wrapper preserves their behavior).

- [ ] **Step 5: Commit**

```bash
git add manager/engine/transform.py manager/tests/test_transform.py
git commit -m "feat(engine): add calculate_slave_lot dispatcher + _snap_clamp for lot-sizing modes"
```

---

## Task 2: Plumb sizing mode through config + engine

**Files:**
- Modify: `manager/engine/copy_loop.py:14-22` (`SlaveConfig`), `manager/engine/copy_loop.py:55-57` (call site), `manager/engine/copy_loop.py:130-152` (`update_slave_config`)
- Modify: `manager/app/controller.py:33-42` (`AccountSpec`), `manager/app/controller.py:187-193` (`start` SlaveConfig construction), `manager/app/controller.py:254-269` (`apply_slave_edit`)
- Test: `manager/tests/test_copy_loop.py`, `manager/tests/test_controller.py`, `manager/tests/test_settings_store.py`

**Interfaces:**
- Consumes (from Task 1): `calculate_slave_lot(mode, master_volume, balance, step_amount, step_size, master_base_lot, fixed_lot, max_lot, lot_step, min_lot, max_lot_symbol) -> float` and the `SIZING_*` constants.
- Produces:
  - `SlaveConfig` fields: `sizing_mode: str = "balance_step"`, `master_base_lot: float = 0.0`, `fixed_lot: float = 0.01` (appended after `normalize_sltp`).
  - `AccountSpec` fields: same three, same defaults (appended after `normalize_sltp`).
  - `CopyEngine.update_slave_config(..., sizing_mode="balance_step", master_base_lot=0.0, fixed_lot=0.01)` — three new keyword-only params with defaults (so the four existing `test_update_slave_config_*` tests, which omit them, keep passing; the defaults match `SlaveConfig` defaults so no behavior change for balance-step slaves).

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_copy_loop.py` (after `test_update_slave_config_does_not_affect_open_trades` at line 240 — find the end of that test and append after its last assertion):

```python
def test_new_copy_master_mode_mirrors_master_lot():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="copy_master")
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.37)]), now=NOW)["s1"]
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.37, abs=1e-8)


def test_new_fixed_lot_mode_uses_fixed_lot():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="fixed_lot", fixed_lot=0.07)
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.5)]), now=NOW)["s1"]
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.07, abs=1e-8)


def test_new_balance_step_with_base_scales_down():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="balance_step", master_base_lot=0.1)
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})  # balance 1000
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.05)]), now=NOW)["s1"]
    # raw_balance 0.10 * (0.05/0.1)=0.05
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.05, abs=1e-8)


def test_new_balance_step_with_base_no_scale_above_base():
    cfg = SlaveConfig(slave_id="s1", symbol_map_csv="EURUSD=EURUSD",
                      step_amount=100.0, step_size=0.01, max_lot=10.0,
                      max_trade_age_minutes=10, normalize_sltp=True,
                      sizing_mode="balance_step", master_base_lot=0.1)
    eng = _engine(slaves=(cfg,), infos={"s1": {"EURUSD": SI}})
    cmds = eng.ingest_snapshot(_snap([_pos(42, volume=0.2)]), now=NOW)["s1"]
    # down-only: master 0.2 >= base 0.1 -> raw_balance 0.10
    assert cmds[0].action == "OPEN"
    assert cmds[0].volume == pytest.approx(0.10, abs=1e-8)


def test_update_slave_config_patches_sizing_mode():
    eng = _engine(infos={"s1": {"EURUSD": SI}})  # balance 1000, balance_step
    eng.update_slave_config(
        "s1", step_amount=100.0, step_size=0.01, max_lot=10.0,
        max_trade_age_minutes=10, symbol_map_csv="EURUSD=EURUSD",
        normalize_sltp=True, sizing_mode="copy_master", master_base_lot=0.0,
        fixed_lot=0.01)
    cfg = eng._slaves["s1"].config
    assert cfg.sizing_mode == "copy_master"
    # next NEW mirrors the master lot, ignoring balance
    cmds = eng.ingest_snapshot(_snap([_pos(7, volume=0.42)]), now=NOW)["s1"]
    assert cmds[0].volume == pytest.approx(0.42, abs=1e-8)
```

Append to `manager/tests/test_controller.py` (after `test_apply_slave_edit_noop_when_not_running` at line 224):

```python
def test_account_spec_defaults_sizing_fields():
    s = AccountSpec(id="s1", terminal_path="C:/s/terminal64.exe")
    assert s.sizing_mode == "balance_step"
    assert s.master_base_lot == 0.0
    assert s.fixed_lot == 0.01


def test_old_slave_dict_reconstructs_with_sizing_defaults():
    """main_window._load_config rebuilds each slave via
    AccountSpec(**{k: d[k] for k in __dataclass_fields__ if k in d}). An old
    settings.json slave dict (no new keys) must reconstruct with the defaults."""
    old = {"id": "s1", "terminal_path": "C:/s/terminal64.exe",
           "symbol_map_csv": "", "step_amount": 100.0, "step_size": 0.01,
           "max_lot": 10.0, "max_trade_age_minutes": 10.0, "normalize_sltp": True}
    fields = AccountSpec.__dataclass_fields__
    spec = AccountSpec(**{k: old[k] for k in fields if k in old})
    assert spec.sizing_mode == "balance_step"
    assert spec.master_base_lot == 0.0
    assert spec.fixed_lot == 0.01


def test_start_passes_sizing_mode_to_engine():
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    slave = AccountSpec(id="s1", terminal_path="C:/s/terminal64.exe",
                        symbol_map_csv="EURUSD=EURUSD", sizing_mode="fixed_lot",
                        fixed_lot=0.07)
    c.start(_master(), [slave],
            master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                            "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    try:
        cfg = c._engine._slaves["s1"].config
        assert cfg.sizing_mode == "fixed_lot"
        assert cfg.fixed_lot == 0.07
    finally:
        c.stop()


def test_apply_slave_edit_forwards_sizing_fields():
    insts = [TerminalInstance("C:/m", "C:/m/terminal64.exe", "appdata"),
             TerminalInstance("C:/s", "C:/s/terminal64.exe", "appdata")]
    c, _, _ = _controller(insts)
    c.start(_master(), [_slave()],
            master_fake_state={
                "positions": [], "symbol_infos": {"EURUSD": SI},
                "account": {"login": 1, "balance": 0.0, "equity": 0.0,
                            "currency": "USD", "server": "Demo"}},
            slave_fake_state=_slave_state())
    try:
        c._supervisor.reconfigure_slave = lambda sid, csv, norm: None
        new = AccountSpec(id="s1", terminal_path="C:/s/terminal64.exe",
                          symbol_map_csv="EURUSD=EURUSD", step_amount=100.0,
                          step_size=0.01, max_lot=10.0,
                          max_trade_age_minutes=10.0, normalize_sltp=True,
                          sizing_mode="copy_master", master_base_lot=0.2,
                          fixed_lot=0.3)
        c.apply_slave_edit("s1", new)
        cfg = c._engine._slaves["s1"].config
        assert cfg.sizing_mode == "copy_master"
        assert cfg.master_base_lot == 0.2
        assert cfg.fixed_lot == 0.3
    finally:
        c.stop()
```

Append to `manager/tests/test_settings_store.py` (after `test_save_then_load_config_round_trip` at line 83):

```python
def test_save_then_load_config_round_trip_with_sizing_fields(tmp_path):
    from manager.settings.store import SettingsStore
    s = SettingsStore(path=tmp_path / "settings.json")
    cfg = {"master": {"terminal_path": "C:/t/terminal64.exe"},
           "slaves": [{"id": "s1", "terminal_path": "C:/s1/terminal64.exe",
                       "symbol_map_csv": "", "step_amount": 100.0,
                       "step_size": 0.01, "max_lot": 10.0,
                       "max_trade_age_minutes": 10.0, "normalize_sltp": True,
                       "sizing_mode": "copy_master", "master_base_lot": 0.1,
                       "fixed_lot": 0.05}]}
    s.save_config(cfg)
    assert s.load_config() == cfg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_copy_loop.py manager/tests/test_controller.py manager/tests/test_settings_store.py -k "copy_master or fixed_lot or base or sizing or old_slave_dict or sizing_fields" -v`
Expected: FAIL — `SlaveConfig`/`AccountSpec` don't accept `sizing_mode`/`master_base_lot`/`fixed_lot` (`TypeError: unexpected keyword argument`).

- [ ] **Step 3: Extend `SlaveConfig` + the call site + `update_slave_config`**

In `manager/engine/copy_loop.py`:

(a) Update the import at line 7:
```python
from manager.engine.transform import (
    SymbolMapper, calculate_slave_lot, SIZING_BALANCE_STEP,
)
```

(b) Replace `SlaveConfig` (lines 14-22) with:
```python
@dataclass
class SlaveConfig:
    slave_id: str
    symbol_map_csv: str
    step_amount: float
    step_size: float
    max_lot: float
    max_trade_age_minutes: int
    normalize_sltp: bool
    sizing_mode: str = "balance_step"
    master_base_lot: float = 0.0
    fixed_lot: float = 0.01
```

(c) Replace the sizing call in `derive_command` (lines 55-57):
```python
        lots = calculate_slave_lot(cfg.sizing_mode, pos.volume, state.balance,
                                   cfg.step_amount, cfg.step_size,
                                   cfg.master_base_lot, cfg.fixed_lot,
                                   cfg.max_lot, info.volume_step,
                                   info.volume_min, info.volume_max)
```

(d) Extend `update_slave_config` (lines 130-152) — add three keyword-only params (with defaults matching `SlaveConfig`) and patch them. Replace the signature + body:
```python
    def update_slave_config(self, slave_id: str, *, step_amount: float,
                            step_size: float, max_lot: float,
                            max_trade_age_minutes: int,
                            symbol_map_csv: str,
                            normalize_sltp: bool,
                            sizing_mode: str = "balance_step",
                            master_base_lot: float = 0.0,
                            fixed_lot: float = 0.01) -> bool:
        """Live-update a running slave's config in place. Returns whether
        symbol_map_csv changed (caller may then ask the worker to re-report
        SymbolInfo). Safe for open trades: derive_command routes
        MODIFY/PARTIAL_CLOSE/CLOSE via the RecordTable (slave_ticket + stored
        open volumes), and only NEW reads these fields / the mapper."""
        state = self._slaves[slave_id]
        cfg = state.config
        map_changed = cfg.symbol_map_csv != symbol_map_csv
        cfg.step_amount = step_amount
        cfg.step_size = step_size
        cfg.max_lot = max_lot
        cfg.max_trade_age_minutes = max_trade_age_minutes
        cfg.normalize_sltp = normalize_sltp
        cfg.symbol_map_csv = symbol_map_csv
        cfg.sizing_mode = sizing_mode
        cfg.master_base_lot = master_base_lot
        cfg.fixed_lot = fixed_lot
        if map_changed:
            state.mapper = SymbolMapper(
                symbol_map_csv, lambda s: s in state.symbol_infos)
        return map_changed
```

- [ ] **Step 4: Extend `AccountSpec` + `start` + `apply_slave_edit`**

In `manager/app/controller.py`:

(a) Replace `AccountSpec` (lines 33-42) with:
```python
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
    sizing_mode: str = "balance_step"
    master_base_lot: float = 0.0
    fixed_lot: float = 0.01
```

(b) In `start` (lines 187-193), replace the `SlaveConfig(...)` construction with:
```python
            self._engine.add_slave(SlaveConfig(
                slave_id=s.id, symbol_map_csv=s.symbol_map_csv,
                step_amount=s.step_amount, step_size=s.step_size,
                max_lot=s.max_lot,
                max_trade_age_minutes=s.max_trade_age_minutes,
                normalize_sltp=s.normalize_sltp,
                sizing_mode=s.sizing_mode, master_base_lot=s.master_base_lot,
                fixed_lot=s.fixed_lot))
```

(c) In `apply_slave_edit` (lines 264-267), replace the `update_slave_config(...)` call with:
```python
        self._engine.update_slave_config(
            slave_id, step_amount=spec.step_amount, step_size=spec.step_size,
            max_lot=spec.max_lot, max_trade_age_minutes=spec.max_trade_age_minutes,
            symbol_map_csv=spec.symbol_map_csv, normalize_sltp=spec.normalize_sltp,
            sizing_mode=spec.sizing_mode, master_base_lot=spec.master_base_lot,
            fixed_lot=spec.fixed_lot)
```

- [ ] **Step 5: Run the affected tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_copy_loop.py manager/tests/test_controller.py manager/tests/test_settings_store.py -v`
Expected: PASS — all new tests green AND the existing `test_update_slave_config_*`, `test_apply_slave_edit_*`, `test_start_*`, and `test_save_then_load_config_round_trip` tests still green.

- [ ] **Step 6: Commit**

```bash
git add manager/engine/copy_loop.py manager/app/controller.py manager/tests/test_copy_loop.py manager/tests/test_controller.py manager/tests/test_settings_store.py
git commit -m "feat(config): plumb sizing_mode/master_base_lot/fixed_lot through AccountSpec, SlaveConfig, engine, controller"
```

---

## Task 3: GUI slave editor — mode combo + show/hide fields

**Files:**
- Modify: `manager/gui/slave_editor.py:58-69` (sizing form), `manager/gui/slave_editor.py:122-129` (`_spec_from_fields`), `manager/gui/slave_editor.py:131-155` (`set_spec`), `manager/gui/slave_editor.py:157-165` (`spec`)
- Test: `manager/tests/test_slave_editor.py` (new tests + one existing test edited)

**Interfaces:**
- Consumes (from Task 2): `AccountSpec.sizing_mode`, `AccountSpec.master_base_lot`, `AccountSpec.fixed_lot`.
- Produces: a `SlaveEditor` with `self.sizing_mode` (`QComboBox`, item data = mode strings), `self.master_base_lot` / `self.fixed_lot` (`QLineEdit`), visibility toggled by `self._update_sizing_visibility()`. `spec()` returns an `AccountSpec` carrying the three new fields.

- [ ] **Step 1: Write the failing tests**

Append to `manager/tests/test_slave_editor.py` (at the end of the file):

```python
def test_slave_editor_has_sizing_mode_combo(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    assert dlg.sizing_mode is not None
    assert dlg.sizing_mode.count() == 3
    datas = [dlg.sizing_mode.itemData(i) for i in range(dlg.sizing_mode.count())]
    assert set(datas) == {"balance_step", "copy_master", "fixed_lot"}


def test_slave_editor_balance_step_shows_step_fields_hides_fixed(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.sizing_mode.setCurrentIndex(0)  # balance_step
    assert not dlg.step_amount.isHidden()
    assert not dlg.step_size.isHidden()
    assert not dlg.master_base_lot.isHidden()
    assert dlg.fixed_lot.isHidden()


def test_slave_editor_copy_master_hides_step_and_base_fields(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    idx = dlg.sizing_mode.findData("copy_master")
    dlg.sizing_mode.setCurrentIndex(idx)
    assert dlg.step_amount.isHidden()
    assert dlg.step_size.isHidden()
    assert dlg.master_base_lot.isHidden()
    assert dlg.fixed_lot.isHidden()
    assert not dlg.max_lot.isHidden()  # cap always visible


def test_slave_editor_fixed_lot_shows_fixed_field_hides_step(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    idx = dlg.sizing_mode.findData("fixed_lot")
    dlg.sizing_mode.setCurrentIndex(idx)
    assert not dlg.fixed_lot.isHidden()
    assert not dlg.max_lot.isHidden()
    assert dlg.step_amount.isHidden()
    assert dlg.step_size.isHidden()
    assert dlg.master_base_lot.isHidden()


def test_slave_editor_spec_carries_sizing_mode(qapp):
    from manager.gui.slave_editor import SlaveEditor
    dlg = SlaveEditor(FakeController([_inst("C:/i0/terminal64.exe")]))
    dlg.id_edit.setText("s1")
    dlg.terminal.setCurrentIndex(0)
    idx = dlg.sizing_mode.findData("copy_master")
    dlg.sizing_mode.setCurrentIndex(idx)
    dlg.master_base_lot.setText("0.2")
    dlg.fixed_lot.setText("0.3")
    dlg.accept()
    spec = dlg.spec()
    assert spec.sizing_mode == "copy_master"
    assert spec.master_base_lot == 0.2
    assert spec.fixed_lot == 0.3


def test_set_spec_pre_populates_sizing_fields(qapp):
    from manager.gui.slave_editor import SlaveEditor
    from manager.app.controller import AccountSpec
    dlg = SlaveEditor(FakeController([_inst("C:/s1/terminal64.exe")]))
    spec = AccountSpec(id="s1", terminal_path="C:/s1/terminal64.exe",
                       symbol_map_csv="EURUSD=EURUSD", step_amount=100.0,
                       step_size=0.01, max_lot=10.0, max_trade_age_minutes=10.0,
                       normalize_sltp=True, sizing_mode="fixed_lot",
                       master_base_lot=0.1, fixed_lot=0.07)
    dlg.set_spec(spec, lock_identity=True)
    assert dlg.sizing_mode.currentData() == "fixed_lot"
    assert dlg.fixed_lot.text() == "0.07"
    assert dlg.master_base_lot.text() == "0.1"
    # fixed_lot mode -> fixed field visible, step fields hidden
    assert not dlg.fixed_lot.isHidden()
    assert dlg.step_amount.isHidden()
```

Also EDIT the existing `test_slave_editor_symbol_table_round_trips_into_csv` (lines 48-56) — `_spec_from_fields` gains three new required params. Replace the call:

```python
    spec = dlg._spec_from_fields("s2", "C:/i0/terminal64.exe", "100", "0.01",
                                 "10", "10", True, "balance_step", "0.1", "0.01")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_slave_editor.py -v`
Expected: FAIL — `SlaveEditor` has no `sizing_mode` attribute (`AttributeError`), and the edited `_spec_from_fields` call fails with `TypeError: takes 7 positional arguments but 10 were given`.

- [ ] **Step 3: Implement the GUI changes**

In `manager/gui/slave_editor.py`:

(a) Replace the sizing form block (lines 58-69) with:
```python
        self._sizing = QFormLayout()
        self.sizing_mode = QComboBox()
        self.sizing_mode.addItem("Balance step (lots step)", "balance_step")
        self.sizing_mode.addItem("Copy master lot", "copy_master")
        self.sizing_mode.addItem("Fixed lot", "fixed_lot")
        self.step_amount = QLineEdit("100")
        self.step_size = QLineEdit("0.01")
        self.max_lot = QLineEdit("10")
        self.max_trade_age_minutes = QLineEdit("10")
        self.master_base_lot = QLineEdit("0.1")
        self.fixed_lot = QLineEdit("0.01")
        self.normalize_sltp = QCheckBox("Normalize SL/TP to slave open price")
        self.normalize_sltp.setChecked(True)
        self._sizing.addRow("Lot sizing mode", self.sizing_mode)
        self._sizing.addRow("Master base lot size", self.master_base_lot)
        self._sizing.addRow("Fixed lot size", self.fixed_lot)
        self._sizing.addRow("Step amount", self.step_amount)
        self._sizing.addRow("Step size", self.step_size)
        self._sizing.addRow("Max lots", self.max_lot)
        self._sizing.addRow("Max trade age (min)", self.max_trade_age_minutes)
        root.addLayout(self._sizing)
        root.addWidget(self.normalize_sltp)
        self.sizing_mode.currentIndexChanged.connect(self._update_sizing_visibility)
        self._update_sizing_visibility()
```

(b) Add the visibility helper method immediately after `_build_ui` (after the `_on_launch_terminal` connection at line 80, i.e. right after the `_build_ui` method ends). Insert:
```python
    def _update_sizing_visibility(self) -> None:
        """Show only the lot-sizing fields relevant to the chosen mode.
        Widgets stay constructed in every mode (so tests/old code can read
        them); only visibility toggles. max_lot + max_trade_age are always
        relevant (cap + age apply to all modes)."""
        mode = self.sizing_mode.currentData()
        def show_row(widget, visible: bool) -> None:
            widget.setVisible(visible)
            lbl = self._sizing.labelForField(widget)
            if lbl is not None:
                lbl.setVisible(visible)
        is_balance = mode == "balance_step"
        is_fixed = mode == "fixed_lot"
        show_row(self.step_amount, is_balance)
        show_row(self.step_size, is_balance)
        show_row(self.master_base_lot, is_balance)
        show_row(self.fixed_lot, is_fixed)
```

(c) Replace `_spec_from_fields` (lines 122-129) with:
```python
    def _spec_from_fields(self, sid, terminal_path, step_amount, step_size,
                          max_lot, max_age, normalize, sizing_mode,
                          master_base_lot, fixed_lot) -> AccountSpec:
        return AccountSpec(
            id=sid, terminal_path=terminal_path or None,
            symbol_map_csv=self._symbol_map_csv(),
            step_amount=float(step_amount), step_size=float(step_size),
            max_lot=float(max_lot), max_trade_age_minutes=float(max_age),
            normalize_sltp=bool(normalize),
            sizing_mode=sizing_mode, master_base_lot=float(master_base_lot),
            fixed_lot=float(fixed_lot))
```

(d) In `set_spec` (lines 131-155), add the three new pre-populations. Insert these three lines right before `self.step_amount.setText(str(spec.step_amount))` (line 151):
```python
        idx = self.sizing_mode.findData(spec.sizing_mode)
        self.sizing_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.master_base_lot.setText(str(spec.master_base_lot))
        self.fixed_lot.setText(str(spec.fixed_lot))
```
(`setCurrentIndex` triggers `currentIndexChanged` → `_update_sizing_visibility`, so the right fields are shown. `findData` returns -1 for an unknown mode → fall back to index 0, balance_step.)

(e) Replace `spec()` (lines 157-165) with:
```python
    def spec(self) -> AccountSpec | None:
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        return self._spec_from_fields(
            self.id_edit.text().strip() or "s1",
            self.terminal.currentText().strip(),
            self.step_amount.text(), self.step_size.text(),
            self.max_lot.text(), self.max_trade_age_minutes.text(),
            self.normalize_sltp.isChecked(),
            self.sizing_mode.currentData(),
            self.master_base_lot.text(), self.fixed_lot.text())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest manager/tests/test_slave_editor.py -v`
Expected: PASS — all new GUI tests green AND the existing editor tests (`test_slave_editor_constructs`, `test_slave_editor_spec_returns_accountspec`, `test_set_spec_pre_populates_and_locks_identity`, `test_set_spec_round_trips_through_spec`, `test_edit_slave_pre_populates_with_locked_identity`, the edited `test_slave_editor_symbol_table_round_trips_into_csv`) green.

- [ ] **Step 5: Commit**

```bash
git add manager/gui/slave_editor.py manager/tests/test_slave_editor.py
git commit -m "feat(gui): lot-sizing mode dropdown + master base lot / fixed lot fields with show/hide"
```

---

## Task 4: README — document the three modes

**Files:**
- Modify: `README.md:70-71` (Features → Lot sizing bullet), `README.md:183-185` (Usage → Slaves step)

**Interfaces:** None (documentation only).

- [ ] **Step 1: Update the Features "Lot sizing" bullet**

In `README.md`, replace lines 70-71:
```markdown
- **Lot sizing** per slave — configurable balance step amount/size and a max
  lot cap; volumes rounded down to the symbol's lot step.
```
with:
```markdown
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
```

- [ ] **Step 2: Update the Usage "Slaves" step**

In `README.md`, replace lines 183-185:
```markdown
   slave's terminal, and set the per-slave symbol map / lot-sizing /
   normalization options. Add as many slaves as you need (one terminal each).
```
with:
```markdown
   slave's terminal, and set the per-slave symbol map / lot-sizing mode
   (balance step with optional master base lot, copy master lot, or fixed lot) /
   normalization options. Add as many slaves as you need (one terminal each).
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document the three slave lot-sizing modes + master base lot scaling"
```

- [ ] **Step 4: Final full-suite verification**

Run the whole suite to confirm no regressions across all tasks:
```bash
& "C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m pytest -q
```
Expected: all green (the pre-existing teardown `PytestUnhandledThreadExceptionWarning` from `_slave_loop` may still appear as a warning — it is not a failure and predates this work).

- [ ] **Step 5: Push so the release workflow builds a new version the in-app updater can pull**

```bash
git push origin main
```

- [ ] **Step 6: Confirm the release published**

```bash
& "C:\Program Files\GitHub CLI\gh.exe" release list -L 3
```
Expected: a new `v0.1.<n>` release (higher than the prior latest) with `manager-latest.whl`.