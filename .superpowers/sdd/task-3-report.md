# Task 3 Report: Symbol mapping helper

## What was implemented

Created `MQL5/Include/TradeCopier/SymbolMapper.mqh` containing the `CSymbolMapper` class:

- `Init(string symbolMapCsv)` parses a comma-separated list of `master=slave` symbol mappings, trims whitespace, validates pair format, and stores explicit mappings.
- `Resolve(string masterSymbol)` returns:
  1. The mapped slave symbol if an explicit mapping exists.
  2. The master symbol itself if it exists on the slave account.
  3. An empty string if no valid symbol is found.
- `ExistsOnSlave(string symbol)` uses `CSymbolInfo.Name()` and `CSymbolInfo.Select()` to check symbol availability on the slave account.

## What was tested

- Verified the header file matches the task brief exactly and contains balanced preprocessor guards.
- Confirmed `CArrayString` and `CSymbolInfo` includes reference the standard MQL5 library paths used by the brief.
- Attempted a compile-check stub at `MQL5/Experts/TradeCopier/TradeCopier.mq5` including the header.
- **Limitation:** The Linux environment has no MetaEditor/Wine available, so actual F7 compilation could not be executed. The stub was removed as instructed.

## Files changed

- `MQL5/Include/TradeCopier/SymbolMapper.mqh` (created)

## Self-review findings

- None. The implementation follows the provided specification exactly.

## Issues or concerns

- Actual MetaEditor compilation verification is pending because the build environment (MetaTrader 5 / MetaEditor) is not installed on this Linux workstation. The code is syntactically consistent with the task brief and the MQL5 standard library APIs.
