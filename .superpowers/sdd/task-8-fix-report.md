# Task 8 Fix Report: Correct PartialClose Volume Calculation

## What Changed

In `MQL5/Include/TradeCopier/SlaveSubscriber.mqh`, the `PartialClose` method now
follows the task brief's exact calculation order:

1. `fraction = e.volume / m_records[idx].master_open_volume`
2. `targetSlaveVolume = m_records[idx].slave_open_volume * fraction`  
   (intermediate value is **not** floored)
3. `volumeToClose = currentSlaveVolume - targetSlaveVolume`
4. `volumeToClose = MathFloor(volumeToClose / lotStep) * lotStep`
5. `volumeToClose = MathMin(volumeToClose, currentSlaveVolume)`
6. Return without closing if `volumeToClose <= 0.0`

The previous code incorrectly applied `MathFloor` to `targetSlaveVolume` before
calculating `volumeToClose`, and then floored `volumeToClose` again, which could
change the closed volume.

### Diff

```diff
    double fraction = e.volume / masterOpenVolume;
    double targetSlaveVolume = slaveOpenVolume * fraction;
    double lotStep = m_symbolInfo.LotsStep();
-   targetSlaveVolume = MathFloor(targetSlaveVolume / lotStep) * lotStep;
    double volumeToClose = currentSlaveVolume - targetSlaveVolume;
    volumeToClose = MathFloor(volumeToClose / lotStep) * lotStep;
+   volumeToClose = MathMin(volumeToClose, currentSlaveVolume);
 
    if(volumeToClose <= 0.0)
       return;
```

## How It Was Verified

- **Syntax check:** A temporary C++ stub was created outside the repository
  (`/tmp/task8_partial_close_stub.cpp`) that includes the exact `PartialClose`
  method body with stubbed MQL5 types and functions. It was compiled with
  `g++ -fsyntax-only` and reported no errors. The stub was not committed.
- **Walk-through scenario:**
  - Slave opened `0.5` lots, master opened `0.5` lots.
  - Master partially closes to `0.3` lots (event volume = `0.3`).
  - `fraction = 0.3 / 0.5 = 0.6`
  - `targetSlaveVolume = 0.5 * 0.6 = 0.3` (not rounded)
  - `volumeToClose = 0.5 - 0.3 = 0.2`
  - Flooring to a lot step of `0.01` leaves `0.2`.
  - `MathMin(0.2, 0.5)` leaves `0.2`.
  - Result: slave closes `0.2` lots, matching the required proportional close.

## Files Changed

| Path | Change |
|------|--------|
| `MQL5/Include/TradeCopier/SlaveSubscriber.mqh` | Removed premature `MathFloor` of `targetSlaveVolume`; added `MathMin` cap on `volumeToClose`. |

## Commit

- **SHA:** `ef33846`
- **Subject:** `Fix PartialClose lot-step calculation in SlaveSubscriber`
- **Co-Authored-By:** Claude <noreply@anthropic.com>

## Concerns

- None. The fix is minimal, follows the brief's formula exactly, and the
  temporary syntax stub passed compilation.
