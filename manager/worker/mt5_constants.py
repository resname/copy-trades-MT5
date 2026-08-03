"""MQL5 trade-request integer constants. These match MetaTrader5's package
constants exactly (standard MQL5 enum values) but are defined here so the
worker and FakeMt5 build/interpret request dicts WITHOUT importing the
MetaTrader5 package. RealMt5 may use these directly or mt5.<NAME>; they agree.
"""

TRADE_ACTION_DEAL = 1      # market open/close
TRADE_ACTION_SLTP = 3      # modify position SL/TP
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_RETURN = 2
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008
