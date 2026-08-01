# Task 9 Report: Main EA Wiring

## What was implemented

Created the main Expert Advisor file:

- `/home/a/copy-trades-MT5/MQL5/Experts/TradeCopier/TradeCopier.mq5`

It wires the existing helper modules based on the `CopierMode` input:

- Includes `CopierConfig.mqh`, `MasterPublisher.mqh`, and `SlaveSubscriber.mqh`.
- Declares global instances `g_master` (`CMasterPublisher`) and `g_slave` (`CSlaveSubscriber`).
- `OnInit()` selects master or slave mode, initializes the corresponding object, and starts a 250 ms millisecond timer.
- `OnDeinit()` kills the timer and deinitializes the active mode object.
- `OnTick()` is intentionally empty; all work is done on the timer.
- `OnTimer()` calls `g_master.PublishChanges(PublishIntervalMs)` in master mode or `g_slave.Poll()` in slave mode.

The file content matches the task brief exactly.

## What was tested and test results

1. **Interface verification** — manually confirmed that the calls in `TradeCopier.mq5` match the public interfaces of the helper classes:
   - `CMasterPublisher::Init(int port, int heartbeatSeconds)`
   - `CMasterPublisher::PublishChanges(int intervalMs)`
   - `CSlaveSubscriber::Init(int port, const string symbolMap, int maxAgeMinutes, int retryCount, int retryDelayMs)`
   - `CSlaveSubscriber::Poll()`
   - `CMasterPublisher::Deinit()` and `CSlaveSubscriber::Deinit()`

2. **Input verification** — confirmed that all inputs referenced (`CopierMode`, `CopierPort`, `HeartbeatSeconds`, `PublishIntervalMs`, `SymbolMap`, `MaxTradeAgeMinutes`, `RetryCount`, `RetryDelayMs`) are declared in `CopierConfig.mqh`.

3. **Stub compile-check** — copied `TradeCopier.mq5` and all helper `.mqh` files into a temporary directory `/tmp/mql5-stub` to verify the include structure. The stub was removed afterward and was not committed.

4. **Actual MetaEditor compilation** — **not possible** in this Linux environment because MetaEditor/MetaTrader 5 is not installed. Consequently, the compiled artifact `TradeCopier.ex5` mentioned in the brief could not be generated or committed.

## Files changed

- `MQL5/Experts/TradeCopier/TradeCopier.mq5` (created)

## Self-review findings

- `TradeCopier.mq5` content matches the task brief verbatim.
- The `g_modeSet` global is declared but not currently used by the EA logic; it is present as specified in the brief.
- `OnTick()` is deliberately empty because all periodic work is handled in `OnTimer()`.
- The 250 ms timer is independent of the master `PublishIntervalMs` (the publisher internally throttles scanning), and the slave `Poll()` uses a 1 ms ZMQ receive timeout, so frequent timer calls are non-blocking.

## Issues or concerns

- **Compilation environment**: MetaEditor is unavailable, so the code could not be compiled to `TradeCopier.ex5`. The `.ex5` artifact from the brief cannot be committed from this environment.
- **Potential forward-declaration issue in `TradeMessage.mqh` (out of scope)**: The helper functions `GetJsonString`, `GetJsonLong`, `GetJsonULong`, `GetJsonInt`, `GetJsonDouble`, and `GetJsonRawValue` are called inside `JsonToEvent()` before their definitions appear in the file. Strict C++ compilation would require forward declarations. Whether the MQL5 compiler accepts this can only be confirmed by compiling in MetaEditor. This was not introduced by Task 9 and is not part of the Task 9 file set.
