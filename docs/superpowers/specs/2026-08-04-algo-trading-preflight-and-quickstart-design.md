# Algo-Trading Preflight Check + README Quick Start

Date: 2026-08-04
Status: Approved (design)

## Problem

When MetaTrader's **Algo Trading** toolbar button is off, the `MetaTrader5`
Python package's `order_send` is blocked: `mt5.terminal_info().trade_allowed`
returns `False` and trade requests fail. A recent MetaQuotes update made that
button the primary control for `trade_allowed`. The copier currently has no
check for this, so a user with Algo Trading disabled sees trades stop copying
with no on-screen explanation — the same class of silent failure we just
debugged for SLTP/filling-mode. The user also asked for a concise Quick Start
guide in the README that states the Algo Trading requirement.

## Goal

1. **GUI program**: an active preflight check that **blocks Start** when any
   terminal the copier uses has Algo Trading disabled, with an actionable error
   naming the terminal(s). No silent no-copy state.
2. **GitHub README**: a concise Quick Start guide near the top, with "enable
   Algo Trading on each terminal" as an explicit step.

## Signal

`mt5.terminal_info().trade_allowed` (bool) — directly tied to the Algo Trading
toolbar button (also reachable via Tools → Options → Expert Advisors → "Allow
algorithmic trading"). `False` ⇒ API trading blocked even when connected.
Source: MQL5 Python `terminal_info` docs, confirmed by the adapter adding a
`terminal_info()` method that reads this field.

## Non-goals

- No static text label in the GUI (the user chose the active check as the
  surfacing, not "Both"). The block message is the notice.
- No enabling of Algo Trading from the manager (it cannot — it's a terminal UI
  setting). We only detect and block.
- No checking of pending-order or account-level trade permissions beyond
  `trade_allowed`.

## Architecture

Reuse the existing worker lifecycle + supervisor readiness gate (Approach A).
The manager process cannot call `mt5.initialize` itself (one per process; the
workers own the connections), so the check runs inside each worker at startup
and is reported back via the existing `StatusMsg`. The supervisor's readiness
gate already waits for every slave to report ready (SymbolInfo + first status)
before spawning the master; extend it to also require `trade_allowed=True`.

```
Start clicked
  └─ supervisor spawns slaves
       each slave worker: initialize → terminal_info() → StatusMsg(connected, trade_allowed, ...)
  └─ readiness gate
       all slaves reported SymbolInfo + status AND trade_allowed=True ?
         no  → emit Error naming offending terminal(s); tear down workers; DO NOT spawn master  → BLOCK
         yes → spawn master
                master worker: initialize → terminal_info() → StatusMsg(trade_allowed)
                master trade_allowed=True ? no → abort before copy loop → BLOCK
                                            yes → start copy loop
```

"Block start" means no copy is ever attempted: when blocked, the master is never
spawned (slaves) or the copy loop never runs (master), so no `order_send` fires.
A worker is briefly spawned per terminal before a block is reported — acceptable
overhead; the user enables Algo Trading and clicks Start again.

## Components

### `manager/worker/mt5_adapter.py`
- Add `terminal_info() -> dict` to the `Mt5Adapter` Protocol.
- `RealMt5.terminal_info()`: `t = mt5.terminal_info()`; return
  `{"trade_allowed": bool(t.trade_allowed)}` if `t` is not None else
  `{"trade_allowed": False}`.
- `FakeMt5.terminal_info()`: return a scripted value (new `terminal_info`
  constructor kwarg defaulting to `{"trade_allowed": True}`) so tests can force
  `False`.

### `manager/ipc/messages.py`
- Add `trade_allowed: bool = True` to `StatusMsg`. Default `True` keeps existing
  tests and flows (which don't set it) unaffected.

### `manager/worker/mt5_worker.py`
- At init (both master and slave), after `account_info()`, call
  `adapter.terminal_info()` and set `status.trade_allowed` on the initial
  `StatusMsg`.

### `manager/supervisor.py`
- Readiness gate: a slave is ready only when it has reported SymbolInfo + first
  status **and** `trade_allowed=True`.
- If any slave reports `trade_allowed=False`, the gate fails: collect the
  terminal names/ids of offenders, emit an `ErrorMsg` (fatal=False — a user
  action can fix it, not a crash) naming them, shut down all workers, and do
  not spawn the master. Return a distinct failure reason so the controller/GUI
  can show the Algo-Trading-specific message.
- Master: after spawn, if its first `StatusMsg` reports `trade_allowed=False`,
  abort before the copy loop runs with the same kind of error.

### `manager/app/controller.py` + `manager/gui/main_window.py`
- The existing `_StatusBridge` already delivers status/errors to the GUI thread.
- When the preflight blocks, the GUI shows a **modal message box**:
  "Algo Trading is disabled on terminal(s): <names>. Enable the 'Algo Trading'
  button in each MetaTrader terminal (toolbar, or Tools → Options → Expert
  Advisors → Allow algorithmic trading), then click Start." The status/log view
  also records the block line.
- No static label is added (per the chosen option).

### `README.md`
- Add a **Quick Start** section immediately after the intro/arch diagram (before
  "Features"). Concise 6-step numbered list:
  1. Install (one-liner; link to Installation).
  2. Launch (`copytrades` or `python -m manager`).
  3. Log in to each terminal on a DEMO account.
  4. **Enable Algo Trading on each terminal** (toolbar button, or Tools →
     Options → Expert Advisors → Allow algorithmic trading) — called out as the
     step people forget.
  5. Select the master terminal → Add slaves (symbol map / lot sizing).
  6. Click Start.
- Add a one-line Algo Trading note to existing Usage step 3 (master Start) and a
  Troubleshooting row: "Trades don't copy / Start blocked: Algo Trading off →
  enable the Algo Trading button on each terminal."
- Existing Installation and Usage sections remain as the detailed references.

## Testing

- `FakeMt5.terminal_info()` scripted; a slave with `trade_allowed=False` →
  supervisor gate blocks (master never spawned, `ErrorMsg` surfaced naming the
  terminal). A master with `trade_allowed=False` → loop aborted before start.
- `StatusMsg` round-trips `trade_allowed` (default True when unset).
- Existing 264 tests stay green (new field defaults True; FakeMt5 default True).
- GUI test: a blocked preflight raises the modal message box (PySide6 host).

## Risks / notes

- `trade_allowed` can also be `False` for transient connection reasons, but the
  gate already waits for a connected first status; if `trade_allowed` is still
  False after a connected status, the most likely cause is the Algo Trading
  button. The message names that cause first.
- Master only reads positions (`positions_get`), so Algo Trading isn't strictly
  required for it; we check it uniformly so the user-facing rule stays simple
  ("enable Algo Trading on every terminal"). If this proves annoying, the master
  check can be relaxed later without touching the slave gate.