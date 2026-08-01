# Task 10: README installation and usage guide

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Fill Installation section**

Replace the TODO with:

```markdown
## Installation

1. Copy `MQL5/Experts/TradeCopier/TradeCopier.mq5` and the `MQL5/Include/TradeCopier/*.mqh` files into your MetaTrader 5 data folder.
2. Make sure the MQL5 ZeroMQ binding (`MQL5/Include/Zmq/Zmq.mqh`) is installed.
   - If missing, install the "ZeroMQ" library from the MetaTrader Market or copy a known-good ZMQ include set.
3. Open `TradeCopier.mq5` in MetaEditor and compile (F7).
4. Attach the EA to a chart on the master account; set `CopierMode` to `MASTER`.
5. Attach the EA to a chart on the slave account; set `CopierMode` to `SLAVE` and configure symbol mapping / lot sizing.
6. Both MT5 terminals must be running on the same machine.
```

- [ ] **Step 2: Add Configuration example**

Append:

```markdown
### Example slave configuration

| Input | Value | Result |
|-------|-------|--------|
| `SymbolMap` | `US30=WS30, XAUUSD=GOLD` | master US30 -> slave WS30 |
| `BalanceStepAmount` | `100.0` | one lot step per €100 balance |
| `BalanceStepSize` | `0.01` | each step adds 0.01 lots |
| `MaxLotSize` | `10.0` | never exceed 10 lots |
| `MaxTradeAgeMinutes` | `30` | ignore trades older than 30 min on startup |
| `NormalizeSLTPUsingPoints` | `true` | convert SL/TP using point distances |

With a €5,000 balance and the values above, the slave lot size will be `floor(5000 / 100) * 0.01 = 0.5` lots.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add installation and configuration examples

Co-Authored-By: Claude <noreply@anthropic.com>"
```
