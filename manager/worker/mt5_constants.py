"""MetaTrader5 trade-request integer constants. These mirror the values the
MetaTrader5 *Python package* exposes (NOT the standard MQL5 enum values, which
differ -- e.g. the package's TRADE_ACTION_SLTP is 6, while the MQL5 enum's
SLTP is 2 and PENDING_SLTP is 3). They are defined here so the worker and
FakeMt5 build/interpret request dicts WITHOUT importing the MetaTrader5
package. test_mt5_worker pins them to the real package to catch drift.
"""

TRADE_ACTION_DEAL = 1      # market open/close
TRADE_ACTION_SLTP = 6      # modify position SL/TP (MetaTrader5 package value)
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0       # Fill or Kill
ORDER_FILLING_IOC = 1       # Immediate or Cancel
ORDER_FILLING_RETURN = 2   # Return (wait for quote; the legacy default)
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008

# SYMBOL_FILLING_* bitmap bits reported by symbol_info.filling_mode:
# bit 0 (value 1) = FOK enabled, bit 1 (value 2) = IOC enabled. RETURN is the
# implicit fallback when neither bit is set (market/execution symbols).
_FILLING_FOK_BIT = 1
_FILLING_IOC_BIT = 2


def select_filling_mode(filling_mode: int) -> int:
    """Pick an order filling mode the symbol supports, from its filling_mode
    bitmap. Was hardcoded ORDER_FILLING_RETURN, which the server rejects with
    retcode 10030 (TRADE_RETCODE_INVALID_FILL) for symbols that only allow
    FOK/IOC (e.g. WS30 on Darwinex: filling_mode=3 -> FOK)."""
    if filling_mode & _FILLING_FOK_BIT:
        return ORDER_FILLING_FOK
    if filling_mode & _FILLING_IOC_BIT:
        return ORDER_FILLING_IOC
    return ORDER_FILLING_RETURN
