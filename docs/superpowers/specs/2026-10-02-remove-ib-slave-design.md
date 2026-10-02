# Remove IB slave — Metatrader 5 only

**Date:** 2026-10-02
**Status:** Approved design → spec
**Supersedes (product surface):** `2026-10-02-ib-futures-slave-design.md` (that
spec documents shipped history; the feature it describes is removed by this
change and can be revived from tags `v0.1.26`/`v0.1.27`)

## Motivation

The owner ran the shipped TWS-API IB connection (v0.1.26) against real usage
and judged the IB operational model a net negative: the desktop program has to
run at all times, its session model conflicts with watching the market in a
browser or phone, and the daily auto-restart / weekly re-authentication cycle
is a persistent hassle. The alternative IB surfaces (Client Portal Web API)
were researched and are strictly worse for unattended copying (still a local
program, daily re-auth, officially not positioned for order automation —
research findings of 2026-10-02, spike probe). Decision: the product is
**MetaTrader 5 only** — remove the IB slave feature entirely, plus all
demo-account-only disclaimers (the owner uses their own accounts live; the
demo-only wording is their call to drop).

## Goals

1. No IB/TWS API code, dependency, UI, tests, or user-facing docs remain in
   the product tree.
2. MT5 single- and multi-slave flows (v0.1.25 surface) behave exactly as
   before — the removal must not touch the engine or MT5 worker behavior.
3. A settings.json saved by v0.1.26 (possibly containing an IB slave)
   loads without crashing; IB slaves are dropped with a single logged line.
4. Demo-only disclaimer language is removed from user-visible text (README,
   GUI label, runbooks); functional instructions (e.g. custom install path)
   are preserved.

## Non-goals

- No engine changes: `manager/engine/` never grew IB knowledge and must not
  gain any from this removal.
- No uninstall/cleanup of `ib_async` from already-provisioned venvs (an
  installed venv's leftover package is inert once no code imports it; the
  updater never touches the prod venv beyond its normal wheel install).
- The `spike/` directory (untracked scratch) and the historical
  `docs/superpowers/` archives stay as-is.

## Product surface after removal

- Slave editor: single MT5 platform only — terminal picker, symbol map, lot
  sizing, normalization. No platform combo, no IB host/port/client-id fields,
  no contract table.
- Worker targets: `mt5` only in `supervisor._worker_target`.
- Update path: no `ib_async` install step; `pyproject.toml` has no IB
  dependency.
- README: no IB sections/pointers; TWS/IB Gateway API setup section deleted;
  `docs/smoke-test-ib.md` deleted.

## Changes

### Delete

- `manager/worker/ib/contracts.py`
- `manager/worker/ib/tags.py`
- `manager/worker/ib/adapter.py`
- `manager/worker/ib/worker.py`
- `manager/tests/test_ib_adapter.py`
- `manager/tests/test_ib_worker.py`
- `docs/smoke-test-ib.md`

### Modify — remove IB surface

- `manager/app/controller.py`: drop `platform`, `ib_host`, `ib_port`,
  `ib_client_id`, `contract_map` from the slave spec/config; drop the IB
  forwarding in `apply_slave_edit`; loading settings drops slaves whose saved
  config carried `platform == "ib"` (one status/log line each, no modal).
- `manager/supervisor.py`: `_worker_target` resolves only the MT5 worker
  (unknown platform → treated as mt5 per existing default).
- `manager/gui/slave_editor.py`: platform combo, IB host/port/client-id
  fields, contract table, `_on_platform_changed` and their lock/edit wiring.
- `manager/update_helper.py`: `_ensure_ib_async` and its call site.
- `pyproject.toml`: drop `ib_async==2.1.0`.
- `README.md`: remove the TWS/IB Gateway API setup section, IB pointers in
  Quick Start/Usage, the IB platform combo feature line (if present), and the
  smoke-test-ib references.
- `manager/tests/test_controller.py`, `test_supervisor.py`,
  `test_slave_editor.py`, `test_update_helper.py`: remove IB test bodies;
  keep/add only the drop-at-load compat test.

### Modify — demo disclaimer removal (strip the demo-only language; keep functional text)

- `README.md` Quick Start step 2, Security model bullet, Usage, file-layout/
  testing lines that frame demos as mandatory.
- `manager/gui/main_window.py`: install disclaimer label loses its closing
  "Log in to a DEMO account only." sentence.
- `docs/smoke-test.md`, `docs/TESTING.md`: reframe as "validate before
  trusting real accounts" runbooks without demo-only mandates; update the
  testing-doc line that referenced the IB smoke doc.

## Behavior / correctness rules

1. Engine, IPC, MT5 worker, MT5 adapter, terminal discovery, settings store:
   zero diffs (a removal PR touching these files fails review unless the diff
   is provably IB-adjacent).
2. Settings load must be tolerant: unknown/legacy fields are ignored; a saved
   IB slave is dropped, not fatal. Master selection and remaining MT5 slaves
   load normally.
3. The GUI must import and render with no Qt warnings after the editor
   surgery; the GUI test file must run on a PySide6 venv (gui tests do not
   skip-red on CI).
4. Version-independent: no migration script, no settings rewrite on load —
   dropping IB slaves is in-memory behavior of the loader only, so a file
   saved by the new version simply won't carry them.

## Testing

- Full suite green on the PySide6 venv from the repo root
  (`"C:/Users/s/AppData/Local/CopyTradesMT5/venv/Scripts/python.exe" -m
  pytest manager/tests -q`); expected ≈ the v0.1.26 suite minus IB tests plus
  the drop-at-load compat test.
- New compat test: settings store / controller loads a config containing
  `{platform: "ib", ...}` plus one mt5 slave; expect the mt5 slave intact and
  the IB slave dropped with a log line.
- GUI editor test: editor produces the same MT5 slave config shape as
  v0.1.25 for a normal edit (round-trip through settings load/save).
- Manual smoke: launch the app, add one MT5 slave, run one copy start cycle
  (the v0.1.27 app's Start path is the verification target).

## Release

- Land on `main` via SDD execution, then tag `v0.1.28` on the user's
  go-ahead and verify release assets (install.ps1, manager-latest.whl,
  manager-latest.whl.sha256, version.txt) as for v0.1.27.
- Version bump: none needed beyond the tag (CI stamps the version at build).