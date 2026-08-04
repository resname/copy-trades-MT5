# Startup / restart patience — 90s grace window

## Problem

On first launch and after any restart, the manager is too impatient: an MT5
terminal must **start + log in + send its first messages**, which can take
30–90s+ (longer for first-ever account login). But the supervisor stamps each
worker's `last_msg_ts` (and the master's `_last_snapshot_ts`) at spawn time and
counts against the *steady-state* thresholds immediately, so:

- **"no heartbeat from master"** fires after `heartbeat_seconds * 2 = 10s`
  with no snapshot (`supervisor._read_master`).
- **"restarted master"** — `_health_check` restarts a worker after
  `stale_seconds = 30s` of silence × `consecutive_failures = 3`.
- **"one or more slaves did not become ready"** fires after a `15s` readiness
  gate (`controller.start` → `wait_for_slaves_ready`).

All three trip on a normally-slow terminal launch, not a real failure.

## Goal

Be patient for a worker's **first** message/snapshot after it is (re)spawned,
then revert to the existing tight steady-state thresholds so a genuinely dead
master is still caught fast during normal operation.

Decisions (approved):
- **Approach:** startup/restart grace period (not a global constant bump).
- **Grace length:** 90 seconds.
- **Configurability:** hardcoded constants (not exposed in `settings.json`).

## Design

### Grace window: first message only

Each worker (master + every slave) gets a **90-second grace window** after it is
(re)spawned to deliver its **first message**. During that window none of the
three impatient messages fire. Once the first message arrives, the supervisor
switches that worker to the existing steady-state thresholds.

For the master, the heartbeat grace specifically requires a **position
snapshot** (`SnapshotMsg`), not just a status ping (`StatusMsg`) — a snapshot
proves the master is actually producing positions.

### Constants

`Supervisor` gains a class constant and constructor param (the param exists
only so tests can pass small values):

```python
STARTUP_GRACE_SECONDS = 90.0

def __init__(self, ...,
             startup_grace_seconds: float = STARTUP_GRACE_SECONDS,
             ...):
    self._startup_grace_seconds = startup_grace_seconds
```

Steady-state thresholds are unchanged: `heartbeat_seconds=5` (warn at 2×=10s),
`stale_seconds=30`, `consecutive_failures=3`.

### Change 1 — Master stale → restart (`_health_check`)

Add `first_msg_seen: bool = False` to `WorkerHandle` (default False on every
fresh `_spawn`, so restarts get a fresh window). Set it `True` on the first
message received, in both `_dispatch_slave` (any slave message) and
`_read_master` (any master message).

In `_health_check`, the effective stale threshold per handle becomes:

```python
stale = (self._startup_grace_seconds if not h.first_msg_seen
         else self._stale_seconds)
if self._time_fn() - h.last_msg_ts > stale:
    h.fail_count += 1
    if h.fail_count >= self._consecutive_failures:
        self._restart(name)
else:
    h.fail_count = 0
```

So a freshly-(re)spawned worker must send something within 90s; a worker that
was talking then falls silent is still restarted after 30s.

### Change 2 — "no heartbeat from master" (`_read_master`)

Track `_master_first_snapshot_seen` (False until the first `SnapshotMsg`).
Threshold:

```python
hb = (self._startup_grace_seconds if not self._master_first_snapshot_seen
      else self._heartbeat_seconds * 2)
if self._time_fn() - self._last_snapshot_ts > hb:
    if not self.heartbeat_warning:
        self.heartbeat_warning = True
        self._surface_error("master", "no heartbeat from master")
```

On the first `SnapshotMsg` in `_read_master`, set
`_master_first_snapshot_seen = True` (alongside the existing
`_last_snapshot_ts = now`, `heartbeat_warning = False`).

**Resets on (re)spawn:** `spawn_master` and `_restart` (when `h.role ==
"master"`) reset, so a restarted master gets a fresh 90s to produce its first
snapshot:

```python
self._last_snapshot_ts = self._time_fn()
self._master_first_snapshot_seen = False
self.heartbeat_warning = False
```

### Change 3 — slave readiness gate (`controller.start`)

```python
ready = sup.wait_for_slaves_ready(timeout=90.0)   # was 15.0
```

This gate is one-shot at initial start and polls until ready (returns early),
so fast slaves still come up instantly; slow first-launch terminals get up to
90s to report SymbolInfo + first Status.

## What stays the same

- Fatal-error handling: a worker that reports a FATAL `mt5.initialize` failure
  is never restarted (it would just blink the terminal open/closed).
- Exponential backoff on death storms (`BASE_BACKOFF`/`MAX_BACKOFF`).
- Slave readiness definition: SymbolInfo + first StatusMsg.
- All steady-state thresholds once first message/snapshot has arrived.

## Tests (TDD)

1. **Grace holds stale-restart:** a worker that never sends a message is NOT
   restarted when `stale_seconds` (30s) elapses, but IS restarted once
   `startup_grace_seconds` (90s) elapses (use injected `time_fn`).
2. **Steady-state unchanged:** a worker that sends one message then goes silent
   is restarted after `stale_seconds` (30s), not 90s (i.e. `first_msg_seen`
   flips the threshold back).
3. **Heartbeat grace:** no snapshot within `heartbeat_seconds*2` (10s) of master
   spawn does NOT fire the warning; it DOES fire after the first snapshot + 10s
   of silence.
4. **Heartbeat grace resets on restart:** after a master restart, the warning
   is suppressed again for 90s even though a prior snapshot had been seen.
5. **Readiness timeout is 90s:** `controller.start` passes `timeout=90.0` to
   `wait_for_slaves_ready`.