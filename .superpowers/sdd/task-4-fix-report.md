# Task 4 Fix Report: Lot sizing helper

## What changed

Edited `MQL5/Include/TradeCopier/LotSizer.mqh` to keep the final lot size aligned **down** to the symbol's `SYMBOL_VOLUME_STEP`:

- Removed `return NormalizeDouble(lots, 2);` because `NormalizeDouble` can round **up**, moving the lot off the required step boundary.
- Added a final `lots = MathFloor(lots / lotStep) * lotStep;` **after** all clamping/capping so that `MathMax`/`MathMin` cannot introduce values that violate the volume step.
- Removed the now-unused `#include <Math\Math.mqh>`.

The sequence is now:
1. Compute raw lots from balance steps.
2. Floor to volume step.
3. Clamp to symbol min/max and caller `maxLot`.
4. Re-floor to volume step so the returned value never rounds up.

### Example

With `lotStep = 0.01` and a clamped/capped value of `0.125`:

```text
MathFloor(0.125 / 0.01) * 0.01 = 0.12
```

`NormalizeDouble(0.125, 2)` would have produced `0.13`, which is now avoided.

## How verified

- Read the original file to confirm the reviewer issue (`NormalizeDouble` at line 56 / final return).
- Applied the edit and reviewed the resulting diff.
- Ran a C++ syntax-only compile check using a temporary stub:
  - Stub path: `/tmp/mql5stub/LotSizerCheck.cpp`
  - Fake `Trade\SymbolInfo.mqh` path: `/tmp/mql5stub/Trade\\SymbolInfo.mqh`
  - Command: `g++ -std=c++17 -fsyntax-only -I /home/a/copy-trades-MT5/MQL5/Include -I /tmp/mql5stub /tmp/mql5stub/LotSizerCheck.cpp`
  - Result: clean syntax pass; only a stub-related format-string warning because MQL5 `string` was mocked with `std::string`.
- Mental walk-through with `lotStep = 0.01`, `lots = 0.125` confirms it floors to `0.12`, not `0.13`.

## Files changed

- `MQL5/Include/TradeCopier/LotSizer.mqh`

## Commit

```text
b89b2d2 fix(LotSizer): align final lot down to volume step instead of rounding up
```

## Concerns

- Full MetaEditor / `metaeditor.exe` compile was not possible because the Linux environment does not have MetaTrader installed. The syntax-only stub check is a best-effort substitute.
- The stub and its fake MQL5 headers were written under `/tmp` and were not committed.
