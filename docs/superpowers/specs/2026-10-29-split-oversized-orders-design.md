# Design: Split oversized slave orders across multiple orders

**Date:** 2026-10-29
**Status:** Approved in chat by the owner (pro-rata partial closes, automatic split, no toggle)

## Intent

Today, when a computed slave order volume exceeds the slave symbol's
`SYMBOL_VOLUME_MAX`, the engine's `_snap_clamp` silently caps it
(`manager/engine/transform.py:120`): a target of 150 lots on a symbol with
max 100 opens only 100 — 50 lots never reach the slave. The owner wants the
full configured volume copied: the worker opens **multiple slave orders**
(100 + 50) when a single order cannot carry the volume.

## Goals / non-goals / anti-goals

**Goals**

- Oversized volume is copied in full, as several slave orders on the same
  symbol/side/magic, each within the symbol's per-order `volume_max`.
- Non-oversized orders behave byte-identically to today (single order,
  unchanged comment/ack path).
- The per-slave **MaxLots config stays a hard cap** on the *total*: sizing
  300 with MaxLots 150 copies 150 (split 100+50 if the symbol max is 100).
- Partial closes split **pro-rata**: each leg shrinks by the master's
  fraction (owner's explicit choice). Full closes close every leg.
- The split survives worker restart / mid-copy update-restart via the
  existing recovery path.

**Non-goals**

- No per-slave toggle, no new GUI/settings fields, no new IPC message
  fields, no `Record`/`RecordTable` shape change.
- No handling for `SYMBOL_VOLUME_LIMIT` (aggregate per-symbol cap) and no
  special netting-account behavior (see constraints).
- No backfilling of already-clamped, currently open single orders.

**Anti-goals (failure modes to avoid)**

- Silently discarding split legs on recovery (the current
  `seed_from_recovery` duplicate-`master_ticket` skip
  `manager/engine/baseline.py:22-25` would do exactly this — it must merge).
- Split legs left with stale SL/TP, unclosed, or untracked after an ack,
  a partial failure, or a restart.
- Duplicating trades on re-derive (existing `table.has` NEW guard must keep
  working: one optimistic record per master ticket).

## Constraints

- Hedge-mode accounts are the target. On netting accounts MT5 merges
  same-symbol orders into one position, so a split there exceeds the
  per-order cap and is broker-rejected — accepted, documented caveat.
- `decode_comment` accepts `CPY#<ticket>|MV<vol>|SV<vol>` and tolerates
  trailing suffixes (`manager/engine/linkage.py:11-13,32-56`); comments
  stay ≤31 chars (Python `order_send` rejects longer with -2).
- Keep the EA-faithful partial-close math: master fraction × original
  open volume, close volume floor-snapped to `volume_step`
  (`manager/worker/mt5_worker.py:205-213`).
- Engine is pure (no I/O); the worker is the only terminal toucher.

## The decided parts (owner + analysis)

**Split location: worker-side (design A).** Analyzed against the
engine-side alternative (per-leg records, per-leg pending, N acks per
trade, engine-emitted per-leg comments + new IPC fields). Worker-side
keeps the engine's bookkeeping (record table, pending/held, optimistic
record, single-ack contract) untouched; the comment-scan fan-out pattern
already exists (`build_recovery_records`, `manager/worker/mt5_worker.py:45-58`).

**1. Engine — remove the symbol-max cap from sizing**
(`manager/engine/transform.py`):
- `_snap_clamp` drops the `lots = min(lots, max_lot_symbol)` line; the
  per-slave `max_lot` (config) cap stays. `calculate_slave_lot`'s
  `max_lot_symbol` parameter goes away or becomes unused — builder's
  choice, but `calculate_lots` (legacy wrapper) and all call sites
  (`manager/engine/copy_loop.py:71-75`) must be updated consistently, and
  the pinned tests (`manager/tests/test_transform.py:155-157`) updated.
- Everything else engine-side is unchanged: one OPEN command per NEW,
  one record per master ticket, acks exactly as today.

**2. Worker — split OPEN** (`manager/worker/mt5_worker.py`, OPEN branch):
- If `cmd.volume <= info.volume_max` → today's single-order path,
  unchanged (including `cmd.comment` used as-is).
- Otherwise split greedily: chunk = min(remaining, `info.volume_max`)
  snapped DOWN to `info.volume_step`, repeat until remaining is 0.
  - If the greedy remainder would fall in (0, `info.volume_min`) —
    possible when `volume_max` is not a step multiple — re-split into
    N = parts+1 equal parts: `base = floor(total_steps / N)` steps each,
    the first `total_steps - N*base` legs get one extra step, so every
    leg is step-aligned and ≤ `volume_max` (total steps T ≤ N·max_steps).
    All parts must also be ≥ `volume_min`; any part < `volume_min`
    (pathological totals) is merged back into neighbors — if that is
    impossible, under-copy and record it in the ack error text.
- Each chunk gets its own comment encoding **its own SV**:
  `encode_comment(master_ticket, MV, chunk_volume)`; MV is decoded from
  the incoming `cmd.comment` (`decode_comment` — part 1's total-SV comment
  is replaced too: per-leg pro-rata math needs per-leg SV). Use the
  manager-side helper by importing from `manager.engine.linkage`
  (already imported in this module).
- Chunks are sent sequentially with the existing
  `_order_send_with_retry` (same request template, same SL/TP, same
  price, per-chunk volume; price/tick fetched once).
- Ack on success: `ok=True`, `slave_ticket` = first chunk's order ticket,
  `fill_volume` = sum of filled chunk volumes, `remaining_volume` =
  same sum (matches today's OPEN-ack convention where it is unused).
- **Partial failure semantics** (defined; also improves today's behavior):
  every successfully filled chunk is kept. If ANY chunk filled →
  `ok=True` with the achieved sum in `fill_volume` and a note like
  `"split: 2/2 ok"` / `"split: 1/2 ok (shortfall …)"` in `error`. If NO
  chunk filled → `ok=False` with the first failure's retcode/error, as
  today. The engine treats `ok=True` as: record's `slave_open_volume` =
  achieved sum — subsequent PARTIAL/CLOSE fan-out then works off the
  real live volume.

**3. Worker — fan-out for MODIFY / PARTIAL_CLOSE / CLOSE:**
- Find legs: `adapter.positions_get()`, decode each position's comment,
  keep positions whose master ticket equals `cmd.master_ticket`
  (a leg with no decodable comment cannot be a leg — skip). This scan is
  the same pattern `build_recovery_records` already uses. `cmd.magic`
  may match but is NOT the filter (magic is `master_ticket % MAGIC_MOD`
  and can collide across different master tickets).
- **MODIFY**: normalize SL/TP per leg against that leg's own open price,
  tick-round once, send one `TRADE_ACTION_SLTP` per leg. Ack ok only if
  every leg succeeded (partial failure → ok=False listing the
  failed retcode).
- **PARTIAL_CLOSE** (pro-rata, owner's choice): for each leg, target =
  leg's own original SV (decoded from its comment) ×
  `new_master_volume / master_open_volume`; close
  `floor((current − target)/volume_step + 1e-9)·volume_step`, skipping
  legs with ≤ 0 to close (and the no-op ack shortcut behavior for
  nothing-to-close in aggregate). Ack ok only if every needed leg
  closed. `cmd.slave_open_volume` (the record's aggregate) stays unused
  per-leg — SV per leg is authoritative (decided; safest for the
  fraction math).
- **CLOSE**: close every leg (one deal per leg, today's request shape).
  Ack ok only when all legs closed; if a leg is already gone, that leg
  counts as closed (today's behavior for a vanished position).
- Legacy single-order positions (volume ≤ max, pre-feature) take the
  same fan-out path — the scan finds exactly one leg and behavior
  matches today's per-ticket math. Verify in tests that single-order
  CLOSE/PARTIAL/MODIFY results are unchanged.

**4. Recovery merge** (`manager/engine/baseline.py`):
- The worker's `build_recovery_records` already emits one record per leg
  (N-capable today). `seed_from_recovery` must merge duplicate
  `master_ticket`s into one Record instead of skipping: keep the first
  `slave_ticket`, sum `slave_open_volume` (MV is identical across legs;
  assert/tolerate if not). This single merge also fixes today's silent
  discard of a second same-master recovery record.
- The recovery wire format needs no change (flat record list).

**5. Docs**
- README feature bullets (lines ~83-90, "clamped to its min/max" wording
  ×2) get a sentence: volumes above the symbol's per-order max are split
  into multiple orders (pro-rata partial closes, per-leg CPY comments),
  and MaxLots still caps the total. One README line + netting caveat
  (hedging accounts are the target).

## What's left to the builder

- Exact `_snap_clamp` signature after removing the symbol-max cap, and
  whether `max_lot_symbol` stays as a parameter (builder's discretion;
  keep `calculate_lots` legacy wrapper behavior coherent).
- Split-helper placement (a pure `split_volume(total, volume_max,
  volume_step, volume_min) -> list[float]` function is recommended —
  unit-test it directly).
- Aggregation placement for recovery merge (in `seed_from_recovery` vs a
  `RecordTable` method) and MV-mismatch tolerance policy.
- Whether MODIFY's SL/TP is tick-rounded once and reused per leg or
  rounded per leg (legs share symbol/tick — builder's call; behavior
  must match today for single legs).
- Test doubles: FakeMt5 needs a way to script symbols whose
  `volume_max` is smaller than a request, and multi-position state
  after a split (tests live in `manager/tests/test_mt5_worker.py`,
  `test_copy_loop_integration.py`, plus new/updated
  `test_transform.py`, `test_baseline.py`, `test_recovery.py`).

## Test invariants to protect (pinned behaviors)

- Single-order OPEN/MODIFY/PARTIAL/CLOSE paths unchanged
  (`test_copy_loop_integration.py:79-120`, engine tests green as today).
- Comment ≤31 chars after split re-encoding (`test_linkage.py:30-35`).
- Recovery `does_not_overwrite_existing_record` semantics for genuinely
  conflicting records must not regress while adding the merge.
- The `table.has` NEW guard: no re-NEW for a ticket already in the
  table; failed-open CLOSE cleanup (`copy_loop.py:238-240`) unchanged.
- Ack contract: exactly one AckMsg per command over the wire (messages
  decode requires all fields — no AckMsg changes planned).