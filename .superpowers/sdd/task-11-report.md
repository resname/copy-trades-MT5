# Task 11 Report: Manual testing checklist in README

## What I implemented

Replaced the TODO placeholder in `README.md` with the full manual testing checklist specified in the task brief. The new section is titled `## Manual Testing Checklist (use two demo accounts)` and contains 13 checklist items covering:

- Master/slave EA installation on two demo charts
- Market-order copying latency
- SL/TP mirroring
- Partial and full close mirroring
- `MaxTradeAgeMinutes` old vs new trade behavior on slave restart
- Mapped symbol usage (`US30=WS30`)
- Unmapped symbol pass-through and missing-symbol error handling
- Balance-step lot sizing changes
- `MaxLotSize` cap
- SL/TP point normalization across different decimal precisions

## What I tested and test results

- Verified `README.md` renders successfully as Markdown using Python-Markdown:
  - Command: `python3 -m markdown README.md > /tmp/README_render.html`
  - Result: rendered without errors; output contains the expected HTML structure.
- Verified the checklist item count and text match the task brief exactly.
- No automated test suite exists for documentation changes; no code tests were required.

## Files changed

- `/home/a/copy-trades-MT5/README.md`
  - Replaced `(TODO: fill in after Task 11)` placeholder with the full checklist.

## Self-review findings

- No issues found.
- Checklist text, formatting, and item order match the task brief.
- Commit message follows the requested format.

## Issues or concerns

- The repository is currently on branch `feature/mt5-trade-copier`, not `main`. The task brief stated the main branch is `main`, but the working tree was already on the feature branch when I started. The commit was made on the current branch, which is consistent with the project's ongoing 12-task integration workflow.
