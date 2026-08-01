# Task 4 Report: Lot sizing helper

## What was implemented

Created `MQL5/Include/TradeCopier/LotSizer.mqh` with class `CLotSizer` and method:

```cpp
double CalculateLots(double balance, double stepAmount, double stepSize,
                     double maxLot, const string symbol);
```

Behavior implemented exactly per the task brief:
- Validates `stepAmount` and `stepSize` are positive.
- Selects the symbol via `CSymbolInfo`.
- Computes whole balance steps with `MathFloor(balance / stepAmount)`.
- Multiplies steps by `stepSize` to get raw lots.
- Rounds DOWN to the symbol's `SYMBOL_VOLUME_STEP`.
- Clamps to symbol min/max volume.
- Caps at the caller-supplied `maxLot`.
- Returns `NormalizeDouble(lots, 2)`.

## What was tested

- Created a temporary stub EA at `/tmp/LotSizerStubEA.mq5` that includes `<TradeCopier/LotSizer.mqh>` and calls `CalculateLots`. It was **not** committed.
- Manual syntax review of `LotSizer.mqh`: all braces balanced, header guard present, includes correct, method signature matches brief.
- **Compile-check limitation:** MetaEditor / `metaeditor.exe` is not installed in this Linux environment and no Wine/Mono installation was found, so an actual F7 compile could not be performed. The module is syntactically identical to the brief's reference implementation.

## Files changed

- `MQL5/Include/TradeCopier/LotSizer.mqh` (created)

## Commit

```
e1453d0 feat: add balance-step lot sizer
```

## Self-review findings

- No code issues identified. Implementation matches the brief exactly.
- The only concern is the inability to run the MetaEditor compile-check in this environment; this should be done on a Windows workstation with MetaTrader 5 installed.

## Issues or concerns

- Compile verification is pending due to missing MetaEditor in the Linux build environment.
