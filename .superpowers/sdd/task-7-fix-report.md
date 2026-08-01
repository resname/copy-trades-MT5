# Task 7 Fix Report: MasterPublisher ZMQ API Mismatch

## What Changed

Updated `MQL5/Include/TradeCopier/MasterPublisher.mqh` to use the common MQL5 ZeroMQ binding API (`dingmaotu/mql-zmq`, also the MetaTrader Market "ZeroMQ" library):

1. Changed include from `#include <zmq\zmq.mqh>` to `#include <Zmq\Zmq.mqh>` to match the standard installed path.
2. Replaced the non-standard `Publisher` member with the binding's generic `Socket` member, and now construct it as `new Socket(m_context, ZMQ_PUB)`.
3. Fixed the resource leak on bind failure in `Init` by deleting `m_socket` and `m_context` and resetting their pointers to `NULL` before returning `false`.
4. `Deinit` already safely deletes the pointers only when valid (using `CheckPointer(...) != POINTER_INVALID`).
5. `Send` already uses the standard `ZmqMsg` + `Socket::send` pattern, so no further change was needed.

## How Verified

- Reviewed the updated file to confirm all required edits.
- Confirmed there are no remaining references to the `Publisher` class (only the `CMasterPublisher` class/file name remains, which is expected).
- Created a temporary local stub at `MQL5/Include/Zmq/Zmq.mqh` containing minimal `Context`, `Socket`, `ZmqMsg`, and `ZMQ_PUB` declarations for a manual syntactic sanity check; this stub was not committed.
- Ran `git diff` to verify the exact, minimal change set.

## Files Changed

- `MQL5/Include/TradeCopier/MasterPublisher.mqh`

## Concerns

- I could not run the actual MetaEditor/MQL5 compiler in this environment, so full compile-time validation depends on CI or a local MetaEditor run. The code now matches the documented `dingmaotu/mql-zmq` API and the requested pattern.
- The temporary `MQL5/Include/Zmq/Zmq.mqh` stub exists only in the working tree and is excluded from the commit.
