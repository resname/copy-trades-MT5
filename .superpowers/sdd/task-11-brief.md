# Task 11: Manual testing checklist in README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add manual testing checklist**

Replace the TODO with:

```markdown
## Manual Testing Checklist (use two demo accounts)

- [ ] Install the same EA on two demo charts: one `MASTER`, one `SLAVE`.
- [ ] Open a market order on the master; verify the slave opens the corresponding position within ~1 second.
- [ ] Modify SL/TP on the master; verify the slave position's SL/TP update.
- [ ] Partially close the master position; verify the slave closes the same fraction.
- [ ] Fully close the master position; verify the slave position closes.
- [ ] Restart the slave EA with an open master position older than `MaxTradeAgeMinutes`; verify it is **not** copied.
- [ ] Restart the slave EA with an open master position newer than `MaxTradeAgeMinutes`; verify it is copied/resynced.
- [ ] Use a mapped symbol (e.g. `US30=WS30`) and confirm the slave uses `WS30`.
- [ ] Use an unmapped symbol that exists on both accounts; confirm the slave uses the same name.
- [ ] Use an unmapped symbol that does **not** exist on the slave; confirm the trade is skipped with an error log.
- [ ] Verify lot sizing changes when the slave account balance changes.
- [ ] Verify `MaxLotSize` cap is respected on large balances.
- [ ] Verify SL/TP point normalization works when master and slave quote different decimal precisions (e.g. master `US30` at 2 decimals, slave `WS30` at 0 decimals).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add manual testing checklist

Co-Authored-By: Claude <noreply@anthropic.com>"
```
