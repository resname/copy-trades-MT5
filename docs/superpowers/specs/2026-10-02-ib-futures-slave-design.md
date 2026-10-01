# Design: IB futures slave support

**Date:** 2026-10-02
**Status:** Draft for review
**Approach:** A — dedicated `ib_worker` implementing the existing IPC protocol

## Problem

The manager copies trades between MetaTrader 5 terminals only. The target
product case is an Interactive Brokers futures account (including
Darwinex-IBKR "DARWIN of futures" accounts) as a slave: a Darwinex IBKR
futures account has no MT5 access at all, so it is unreachable today, and
genuine futures (E-mini YM/ES/NQ…) cannot be traded via MT5.

Darwinex explicitly permits API trading on their IBKR accounts via the TWS
API. IB accounts therefore need a non-MT5 worker speaking the TWS API.

## Goal and non-goals

**Goal:** an IB Gateway-driven slave that receives the same
OPEN / MODIFY / PARTIAL_CLOSE / CLOSE command stream an MT5 slave receives
today, with full SL/TP parity, shipping as a product feature. The worker is
contract-type-agnostic: the symbol map's `secType` selects the asset class
(`FUT`, `STK`, `CASH`, `IND`, `BAG`…), so any asset class IB offers is in
scope as long as it trades as positions.

**Non-goals:**

- No IB *master* support. The master side remains MT5-only.
- No pending-order copying (unchanged product decision: positions only).
- No change to MT5-only installs' behavior. Everything is additive.

## Architecture

The IPC protocol is the platform seam. The engine, snapshot diffing, and
record tables are unchanged.

```
engine/copy_loop  (unchanged semantics)
      |  CommandMsg / SnapshotMsg / AckMsg  (existing pipe framing)
      v
supervisor -- worker subprocess, chosen by slave platform type:
      |-- mt5_worker  -> terminal64.exe              (today, byte-identical behavior)
      +-- ib_worker   -> IB Gateway (or TWS) socket 4001/4002 via ibapi
```

- `SlaveConfig` gains `platform: "mt5" | "ib"` plus an IB sub-config.
  A slave with `platform="mt5"` (or the field missing) flows through every
  existing path untouched.
- The supervisor gains a spawn path for `ib_worker`: connect to the IB
  Gateway socket instead of `mt5.initialize`; the same reconnect/backoff loop.
- All IB-specific knowledge lives in the worker: contract selection,
  tick-size and volume normalization, bracket handling, position-to-ticket
  mapping. The manager keeps sending raw master SL/TP, side, prices, and
  volume fractions, exactly as today.
- Trade-off accepted: some MT5 vocabulary (ticket ids, `point`, lot-based
  volumes) stays in the IPC protocol. Renaming it is not worth the blast
  radius on two production installs.

## Gateway as a managed platform

A slave on IB needs a running, logged-in Gateway (or TWS). The manager
treats it like an MT5 terminal install.

- **Setup.** An "Add Gateway…" action beside "Install MetaTrader": a wizard
  installs IB Gateway (or selects an existing TWS install) and installs IBC
  with an embedded config for auto-restart handling. IB credentials live
  only in IBC's own config, never in manager settings. 2FA stays
  interactive via IB's mobile app.
- **Daily restart.** IBC auto-quits around midnight ET and re-authenticates.
  In that window the `ib_worker` reports `connected=False`; the supervisor
  keeps reconnecting without treating it as fatal. On reconnect the worker
  re-baselines (same recovery path as an MT5 terminal restart).
- **Ports.** Gateway: 4001 live / 4002 paper. TWS: 7496/7497. Selectable in
  the Gateway config.
- **Start gate.** MT5 slaves refuse Start unless Algo Trading is on. IB
  slaves gate on the equivalent: socket connects and `Read-Only API` is
  off. One Start evaluates both gates.

## Symbol map -> IB contract

Master side stays MT5 (`US30`, `US30.m`, regex rows unchanged).

- **Map row (IB slave):** the row's slave side names a contract:
  `{master_pattern: {symbol, exchange, secType}}` — `secType` selects the
  asset class (`FUT`, `STK`, `CASH`, `IND`, …) — plus `master_point_value`
  (the master CFD's dollar value per point per lot, e.g. `$1` for US_30).
  The manager sends only the resolved slave symbol; the worker owns full
  contract resolution, as it owns `symbol_info` today.
- **Front-month selection (expiry-bearing secTypes):** on first use the
  worker queries valid months (contract details) and picks the most
  liquid, cached.
- **Rollover (expiry-bearing secTypes):** within a configurable rollover
  window (default 5 trading days) of expiry the worker re-resolves,
  freezes the old contract for *new* orders, and routes new OPENs to the
  next front month. Existing positions keep their contract until closed.
  The GUI shows contract state (active / frozen / rolling). Cash and
  perpetual secTypes (`CASH`, `STK`, `IND`) skip this machinery entirely.
- **Front-month measurement:** liquidity = IB open interest from contract
  details, falling back to nearest expiry when interest data is
  unavailable. A GUI override may pin a month later if needed; not in v1.

## SL/TP parity (brackets)

- **OPEN** = parent market order with attached children — stop-loss (`STP`)
  and take-profit (`LMT`) — tagged `orderRef = "ct:{master_ticket}:{magic}"`.
  Children activate on parent fill (standard bracket).
- **MODIFY** = locate tagged children and replace them (modify by same
  orderId; cancel+new fallback). Raw master SL/TP arrive at the worker; the
  worker normalizes to contract tick size and validates against current
  price, rejecting nonsensical stops with `ok=False` (the MT5 retcode
  equivalent).
- **OCA.** SL and TP children share an OCA group. Our CLOSE/PARTIAL orders
  are plain reduce orders, *not* in that group, so a bracket firing
  concurrently with an out-of-band close cannot double-close: worst case a
  reduce order finds no position and nets to a tolerated no-op.
- **Read-back.** `SnapshotMsg.positions` for the IB slave derives from IB
  `positions` + `reqAllOpenOrders` filtered to our tags. A derived
  Position's sl/tp come from the tagged children's prices. The engine's
  existing "is slave SL/TP in sync with master?" MODIFY-detection works
  unchanged.

## Partial close and close

- **PARTIAL_CLOSE:** target = `slave_open_volume * (master_open_volume -
  new_master_volume) / master_open_volume` (the same fraction rule as
  today), converted to whole contracts, sent as an opposite-side tagged
  order, and the tagged SL/TP children are re-sized to the new remaining
  volume at unchanged prices (IB child orders fill for their stated
  quantity; leaving them stale would let a stop fire past the remaining
  position and open a reverse trade). Fills update the `Record` via the
  existing RecoveryMsg path.
- **CLOSE:** opposite-side market order for the full remaining position.
  The worker cancels the tagged children once it observes the position
  fully closed; it never relies on IB's implicit auto-cancel of attached
  orders, which is not guaranteed once the *order* has finished its life.
- **Race tolerance:** a CLOSE arriving after the bracket fired reports
  `ok=True, remaining=0` so the engine retires the record, matching
  today's unknown-position tolerance.

## Volume sizing

Master trade lots (CFD lots) do not map to whole futures contracts
directly.

- **Ratio:** `ratio = master_point_value / contract_point_value`, the
  contract point value coming from the contract's `multiplier` in details.
  Slave contracts = `floor(master_volume * ratio * sizing_factor)`.
  Small master trades that floor to 0 are skipped and logged — matching
  today's sub-min-lot handling, not an error.
- **Sizing modes are the existing two.** *Balance step* steps contract
  counts per slave-balance tier as today, with master-base-lot
  proportional shrink applying before the contract floor; *copy master
  lot* mirrors the trade through the ratio. No new sizing modes.
- **Margin guard.** One YM/ES contract's margin can dwarf a CFD account's
  balance. The worker computes a margin estimate from account-updates
  values; OPEN would exceed free funds reports a clean `ok=False`,
  surfaced in the GUI as "insufficient margin" — never silent.

## ID mapping and recovery

- IB has no position tickets. `slave_ticket` = a deterministic synthetic id
  from contract + side, stable for the position's life;
  `orderRef` tags carry `master_ticket` so a restarted worker relinks
  bracket children after a Gateway restart.
- On worker (re)connect, the worker reports records recovered from its open
  tagged orders + positions: the existing `RecoveryMsg` mechanism.

## Acknowledgement model

`ibapi` is callback-driven; MT5's order_send is synchronous. The
`ib_worker` keeps a small pending-command queue so every `CommandMsg` still
produces exactly one `AckMsg` (ok / retcode / error / fill data), with a
timeout reporting failure rather than hanging the pipe.

## GUI / settings

- **Add Slave…** gains a platform picker: *MetaTrader terminal* (existing
  flow) / *IB Gateway*. IB slaves skip the terminal picker and MT5 symbol
  validation and show contract-map rows instead.
- **Status view:** per-IB-slave cards show Gateway connection, front month
  + roll state, tagged-order count, and rejected commands with reasons.
- **Persistence:** `platform` and IB config live in the same settings JSON
  via `SettingsStore`; migration is additive (missing `platform` =>
  `"mt5"`), so both production installs upgrade with no action.

## Testing

- **Unit:** engine tests keep `FakeMt5`. The `ib_worker` gets a fake
  `ibapi` transport (record/replay callbacks: fills, partials, reconnects,
  rollover, margin refusals).
- **Integration:** a paper-gateway smoke test doc (port 4002,
  open / modify / partial / close), paralleling `docs/smoke-test.md`.
- **Regression guarantee:** MT5-path tests stay untouched except for the
  additive `platform` field.