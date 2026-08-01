# Task 4 Fix 2 Report: Lot sizing helper

## What changed

`MQL5/Include/TradeCopier/LotSizer.mqh` was updated so `CLotSizer::CalculateLots`:

1. Validates `stepAmount`, `stepSize`, symbol selection, and `lotStep > 0` before any calculation.
2. Fetches `lotStep`, `minLot`, and `maxLotSymbol` before computing lots, and returns `0.0` with an error log if `lotStep <= 0.0`.
3. Computes `steps = MathFloor(balance / stepAmount)` and `lots = steps * stepSize`.
4. Rounds down to the `lotStep` grid once: `lots = MathFloor(lots / lotStep) * lotStep`.
5. Clamps to the minimum/maximum boundaries using `MathMax` and `MathMin` without applying another floor afterward.

This removes the previous final `MathFloor(lots / lotStep) * lotStep` that could round a clamped `minLot` back below the minimum when `minLot` was not an exact multiple of `lotStep`.

## How the fix was verified

- **Syntax check:** A temporary C++ stub was compiled with `g++ -std=c++11 -Wall -Wextra -c`. The stub defined `Print`, `PrintFormat`, `MathFloor`, `MathMax`, `MathMin`, and a minimal `CSymbolInfo` stand-in. Compilation produced only warnings related to the stub (unused parameters, printf format mismatch for `std::string` vs. MQL5 `string`, and unused variable), and no errors.
- **Mental test cases:**
  - `minLot=0.01`, `lotStep=0.01`, balance steps produce `lots=0.009`. `MathFloor(0.009/0.01)*0.01 = 0.00`; `MathMax(0.00, 0.01) = 0.01`. Correct.
  - `lots=0.125`, `lotStep=0.01`. `MathFloor(0.125/0.01)*0.01 = 0.12`; clamping keeps it at `0.12`. Correct.
- The temporary stub and any created support files were deleted and not committed.

## Files changed

- `MQL5/Include/TradeCopier/LotSizer.mqh`

## Concerns

None. The change matches the required calculation order, removes the post-clamp floor, and adds the `lotStep <= 0.0` guard.
