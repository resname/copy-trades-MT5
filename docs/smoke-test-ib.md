# Smoke test — IB slave (paper)

Manual walkthrough for one IB-paper slave. This is the tier-3 manual
validation for the IB connector: the unit + fake-gateway suite
(`pytest manager/tests`) covers the copy logic with no Gateway and no GUI,
so this runbook is the only step that touches IB Gateway, and it is
**paper accounts only** — never point a live account at this path.

## Prereqs
- IB Gateway installed (ibkrguides.com/software/gateway — IB Gateway 10.x),
  logged in to a **paper** account.
- Gateway API settings (login → Connection → API → Settings): enable
  "ActiveX and Socket Clients", disable "Read-Only API".
- `pip install -e .` fresh installs `ib_async==2.1.0` from pyproject.
- Windows 11, Python 3.11+; the manager venv already ships ib_async (the
  in-app updater installs it explicitly after its `--no-deps` wheel
  reinstall).

## Setup
1. Launch: `python -m manager`.

## Run
1. In the manager: Add Slave… → platform "IB Gateway (TWS API)"; id `ib1`;
   contract row `US30 → YM / CME / FUT / 1.0`; sizing Balance step,
   step amount 1000, step size 1; Start.
2. Expect: status shows `ib1: YM 202612` (or `ib1: YM 202612 rolling` inside
   the roll window — the current front month). The Start gate only proves
   the socket is connected — if you left "Read-Only API" enabled, that
   surfaces later, not here: the first copy command fails with an error in
   the status (no crash). If the status stays silent after Start with the
   socket up, a wedged-but-alive Gateway may not be answering the contract
   resolve / tick / margin round-trips — check the Gateway log.
3. Open a long on the master (DEMO US_30). Expect the parent + stop + limit
   appear in the Gateway's Orders view tagged `CPY#…`, volume = contracts.
4. Nudge the master's SL/TP; expect the tagged children replaced.
5. Partially close the master; expect one opposite `CPY#…` MKT order and the
   children resized (their quantity ≤ the remaining contracts).
6. Close the master; expect the net position flat and the stop/limit gone.
7. Quit the Gateway mid-run (Tests → Kill). Expect reconnect errors, no
   crash; Start again after Gateway relaunch — recovery records must be
   re-seeded from the surviving `CPY#` tags (no duplicate trades after the
   next open on the master).
8. Set step size so qty floors to 0 for a small master trade: expect a
   logged `skipped: <units> units -> 0 contracts (ratio <r>)` and nothing
   placed.

## Pass criteria
- Steps 2, 3, 4, 5, 6 behave as described; step 7 reconnects without a
  crash and without duplicating an existing master position; step 8 leaves
  no order in the Gateway besides the log line.

## What this runbook does NOT cover (forward-looking)
- Live (non-paper) IB accounts, and any broker whose futures symbols
  differ from the `US30 → YM` mapping used here.
- Multiple IB slaves in one session, and long-running rollover windows
  (a roll during a live copy — the status detail shows `… rolling` when
  the resolver is inside the window, but the switchover behavior itself is
  unit-tested, not walked through here).