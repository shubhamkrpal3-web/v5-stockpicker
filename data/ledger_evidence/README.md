# Ledger evidence

Two separate files. Only the first is read by the paper ledger.

## historical_entry_fees.csv — what the paper ledger actually charged (used)

Each row records the entry fee the paper ledger charged when it opened a position whose row never
had an `entry_cost_inr`. Every row here is a fee of **0**: these entries were made before the cost
model existed (it was added by commit `a007948` on 2026-09-03).

`paper_trade_manager.py` reads this file (default path; `--fee-evidence` overrides it) and writes the
zero into the position's `entry_cost_inr`, with its basis in `entry_fee_source`, only when all of the
following hold at run time:

* the row has `STATUS = verified` and matches the position's symbol, entry date, quantity, entry
  price and notional exactly;
* the position's own row has no recorded entry fee (a recorded fee is never replaced);
* the ledger's equity log has a row for the entry day whose cash equals starting capital − INVESTED +
  REALIZED_PNL exactly, i.e. that run's cash had no fee term, and the day's cash change equals
  `ENTRY_DAY_CASH_CHANGE_INR`.

Otherwise the fee stays "not recorded" and the ledger's cash stays unverified.

Columns: `SYMBOL, ENTRY_DATE, QTY, ENTRY_PRICE, NOTIONAL_INR` (the position);
`HISTORICAL_ENTRY_FEE_INR` (the fee charged); `CODE_IN_FORCE` (the ledger code that ran, and what it
did); `LEDGER_COMMIT` (the commit that first wrote the row); `RUN_ID` (the GitHub Actions run of
V5 Daily Data Refresh that produced that commit); `ENTRY_DAY_CASH_CHANGE_INR` and
`ENTRY_DAY_NOTIONAL_TOTAL_INR` (from the equity log and positions written by that run); `STATUS`; `NOTES`.

### How each row was verified (2026-10-07)

Derived only from source records, each value checked:

1. `LEDGER_COMMIT`: the first commit whose `data/live_signals/positions.csv` contains the row; the
   row has no `entry_cost_inr`, and `qty × entry_price = size_inr`.
2. `CODE_IN_FORCE`: `v5_modules/paper_trade_manager.py` at the parent of that commit (last changed in
   `f800a30`, 2026-07-12) has no cost step (`cost_of_trade` / `entry_cost` absent), fills at the
   entry day's OPEN and keeps `cash = portfolio_inr - invested_capital + realized_pnl`.
3. Run record: in that commit's `equity_log.csv` the entry day's CASH equals starting capital −
   INVESTED + REALIZED_PNL, and the day's cash change equals minus the notional of that day's entries
   (realised P&L unchanged).
4. `RUN_ID`: `gh run list --workflow daily_refresh.yml` — the successful run whose head commit is
   the parent of `LEDGER_COMMIT`; its log prints the same cash and no fee lines.
5. Fill: the entry price equals the entry day's OPEN in `master_history.csv.gz` as committed by the
   same run.

Check 3 is the full no-fee identity: on a day that also had exits (POLYCAB, 2026-07-20) the cash change is
minus the day's entries plus the cost and realised P&L the exits returned, with no fee term.

### Rows

* 2026-10-07: JBCHEPHARM, IPCALAB, DIVISLAB, INDHOTEL (the lots still open on 2026-10-01).
* 2026-10-07, added after a second, independent pass of checks 1-5 on each row, plus the run logs of runs
  29272731977, 29766647327, 30290911056 and 30839100548 (each prints the same cash as the equity log, the same
  number of entries, and no fee or cost lines in the paper-trade step): SAILIFE, POLYCAB, LAURUSLABS,
  PIDILITIND, CIPLA, SUNPHARMA, SONACOMS, RADICO, MARICO, BHARATFORG, ADANIPORTS, AIAENG, MEDANTA, RAINBOW
  (the lots open in the recorded state after the 2026-08-05 session, from which a corrected replay starts).
  Each was also accepted by the ledger's own run-time check against the recorded positions and equity log.

## entry_fee_estimates.csv — ESTIMATES only (never used by the ledger)

The fee today's cost model (`realistic_cost_model.cost_of_trade`, buy side, 20-session average traded
value at entry) would assign to the same entries. These are **estimates**: the paper ledger never
charged them. They are not cash, not part of any cost basis and not in P&L; nothing in the ledger or
its cash check reads this file. `ledger_view.entry_fee_estimates()` shows them next to the fee
actually charged, labelled as estimates.
