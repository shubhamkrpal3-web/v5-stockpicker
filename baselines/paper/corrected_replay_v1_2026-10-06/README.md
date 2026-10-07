# Corrected paper-ledger replay v1 (41 sessions, 2026-08-07 to 2026-10-06)

**What this is.** A corrected reconstruction of the V5 *paper ledger* for 41 sessions, 7 Aug to 6 Oct 2026, rebuilt
one session at a time from the ledger exactly as recorded after the 5 Aug 2026 session, with known accounting
errors corrected (listed below) and the exit rules and fees that were in force on each date.

**What this is not.** It is not a strategy-performance result, not a backtest and not evidence of trading alpha:
41 sessions of one paper portfolio say nothing reliable about the strategy's edge. It does not replace the recorded
paper ledger, which remains the original record (`data/live_signals/` in git; origin/main 52fd2fe when this archive
was made). Nothing in the project reads this folder: not the paper ledger, reports, the risk engine or the allocator.

**Status.** Created 2026-10-07 for review on branch `fix/backtest-integrity` (HEAD 2329573); not committed, not
tagged. Treat as immutable: never edit files here. A new fact (for example the Torrent fraction's sale proceeds)
means a new version (`corrected_replay_v2_...`), not an edit.

## What the replay corrects

1. **JBCHEPHARM merger** into Torrent Pharma (verified, 51 Torrent shares per 100 JB shares; record date 17 Jul):
   the 3 JB shares become 1 Torrent share plus a 0.53 fractional entitlement. The recorded ledger never applied
   it and carried JB at cost. Under the rules then in force, the Torrent share left on 7 Aug by the 25-calendar-day
   time stop at the close, 4,924.00 (no fees before 3 Sep). The 0.53 fraction awaits the scheme's trustee sale.
2. **BLSE 2:1 split** (face value Rs 10 to Rs 5, ex-date 6 Oct 2026; NSE corporate-actions feed and the
   company's NSE filings of 6 Aug and 18 Sep). The recorded ledger booked a false stop exit at 299.81.
3. **Execution mechanics**: each session's stop is checked against the stop set at the previous close (the old
   code raised it with the same day's close and low first); a gap through the stop fills at the open; the
   initial stop is checked, and the trailing stop raised, on the entry day; quantity is fixed from the signal
   close; every session is processed (the recorded ledger skipped 6 Aug); a holding with no prints turns stale.
4. **Exit rules and fees by date**: to the 2 Sep session, highest close - 3 x ATR14 plus the 22-session low, a
   25-calendar-day time stop and no fees; from 3 Sep, highest close - 4 x ATR14, no time stop, fees on both legs.
5. **Historical entry fees**: the 18 lots entered before the cost model carry the fee the ledger actually charged
   at entry, zero, established by `data/ledger_evidence/historical_entry_fees.csv`.

## Value at 6 Oct 2026 (Rs)

Holdings are marked at the 6 Oct close. The 0.53 Torrent fraction is a **pending entitlement**, shown at
Torrent's 6 Oct close of 4,772.00 as a reference only. It is **not spendable
cash**: the trustee-sale proceeds (due by about 18 Oct 2026, net of expenses, rounded up to the next rupee per
holder) are not yet disclosed.

| | Spendable cash | Listed holdings | Pending entitlement (not cash) | Total economic value |
|---|---|---|---|---|
| Recorded ledger, as reported | 100,124.82 | 91,212.70 + JB carried at cost 7,245.60 | - | 198,583.12 |
| Recorded ledger, like for like (JB as its Torrent entitlement) | 100,124.82 | 95,984.70 | 2,529.16 | 198,638.68 |
| **This replay (B1)** | **98,893.88** | **96,195.03** | **2,529.16** (0.53 Torrent) | **197,618.07** |

The ledger-reported equity of this replay is 195,088.91 = cash + listed holdings; it leaves the pending fraction
out. Like for like, the replay is 1,020.61 below the recorded ledger.

*JB at cost.* With no JB price after 16 Jul, the original code valued the unconverted JB lot at its entry cost,
7,245.60. What the lot actually was on 6 Oct is 1 listed Torrent share (4,772.00) plus the 0.53 fraction
(2,529.16), 7,301.16 - the like-for-like row uses that. In this replay that share was sold on 7 Aug for 4,924.00.

## Value bridge, recorded to corrected (like for like, Rs)

| Step | Total economic value | Change |
|---|---|---|
| Recorded ledger | 198,638.68 | |
| Old-code emulator on the same inputs (validation: reproduces the recorded ledger exactly) | 198,638.68 | 0.00 |
| + the 6 Aug session the recorded ledger skipped (CIPLA's trailing high) | 198,646.48 | +7.80 |
| + corrected execution mechanics | 196,028.65 | -2,617.83 |
| + historical fee basis | 196,217.37 | +188.72 |
| + BLSE split | 198,750.22 | +2,532.85 |
| + JBCHEPHARM merger = this replay | 197,618.07 | -1,132.15 |

The order matters for two items: without the split data, the corrected engine fills BLSE's split-day "gap" at
163.70 (-2,175.06 of the mechanics step), which the split step then undoes; net, BLSE adds 357.79 against the
recorded ledger. Of the merger step, the lot itself adds 152.00 (sold at 4,924.00 against 4,772.00 held) and the
trades its freed slot and cash led to cost 1,284.15. Detail: `reports/value_bridge_recorded_to_corrected.csv`.

## Fees: three treatments, kept separate (7 Aug to 6 Oct, Rs)

| Treatment | What it represents | At entry | Entry fee at exit | Exit | Total | Economic value 6 Oct |
|---|---|---|---|---|---|---|
| Original recorded ledger (its own trades) | what the paper ledger booked: none before 3 Sep; from 3 Sep the cost model, and lots entered earlier were charged their entry fee when they exited | 275.76 | 131.36 | 154.24 | 561.36 | 198,638.68 |
| **This replay, historical basis (headline)** | the fee charged at the time of each trade: zero before 3 Sep (verified), the cost model from 3 Sep; nothing charged at exit for entry | 294.84 | 0.00 | 190.62 | 485.46 | 197,618.07 |
| Sensitivity: same trades, original at-exit practice | this replay's trades plus the original habit of charging older lots' entry fee at exit | 294.84 | 209.94 | 190.62 | 695.40 | 197,408.13 |
| Scenario, **not history**: today's cost model on every leg | this replay's trades priced as if today's cost model applied on every date, including the entry legs (171.60) of the lots open at the start | 717.78 | 0.00 | 388.29 | 1,277.67 | 196,825.87 |

The headline figures use the historical basis. The other rows are shown for comparison only; they are not added
to, or netted against, the headline. Detail: `reports/fee_treatments.csv`.

## Trade differences against the recorded ledger

26 lots differ (34 are identical). By cause: corrected execution mechanics 7 (APOLLOHOSP 10 Aug, LUPIN, UNITDSPR
31 Aug, GRANULES 7 Sep, NESTLEIND, SUNDRMFAST, NYKAA 5 Oct); merger 6 (Torrent share, Torrent fraction, GRANULES 10
Aug, UNITDSPR 24 Aug, ALKEM, SHINDL); fee basis only 6 (APOLLOHOSP 17 Aug, KAJARIACER, MINDACORP, NYKAA 24 Aug,
SUNPHARMA, MARICO); the 6 Aug session 1 (CIPLA); combined causes 6 (JBCHEPHARM, OBEROIRLTY, GRANULES 24 Aug,
SONACOMS 24 Aug, DIVISLAB, BLSE). Every lot, with quantities, prices, exit dates, P&L before and after, cause and
whether it changes cash, slots or open positions: `reports/trade_differences_recorded_vs_corrected.csv`. The same
changes step by step (37 rows, including SJS lots that move and move back):
`reports/decomposition_trade_differences.csv`.

How the causes were isolated: an emulator of the original ledger code, run on these exact inputs, reproduces the
recorded ledger exactly (all 53 lots and all 41 equity-log rows); one factor was then changed at a time. No
difference is left unexplained. The control scripts are not part of this archive.

## Reproduce

Requirements: Python 3.12.10, pandas 3.0.3, numpy 2.2.6 (the versions it was verified with); no network access.

    python reproduce.py

checks `SHA256SUMS`, runs the replay with the archived code and inputs into a new folder outside this archive and
compares the result with `outputs/`. The ledger writes CSV files with the operating system's line ending and
`outputs/` was produced on Windows, so on another system the check reports a match "except line endings".
Every file in this folder is stored byte for byte (`baselines/.gitattributes` turns off line-ending conversion). The underlying command, from this folder:

    python v5_modules/corrected_replay.py --base inputs/start_state --master data/market_database/master_history.csv.gz --signals inputs/signals --out <new folder outside this archive> --start 20260807 --fee-evidence data/ledger_evidence/historical_entry_fees.csv --corporate-actions data/market_database/corporate_actions.csv

## Contents

| Path | What |
|---|---|
| `outputs/` | the replay: `positions.csv`, `realized_trades.csv`, `equity_log.csv` (rows from 7 Aug say SOURCE = corrected replay) |
| `inputs/start_state/` | the recorded ledger after the 5 Aug session (git cea8737) |
| `inputs/signals/` | the signal file each Monday's ledger run read (git, one commit per Monday) |
| `data/market_database/` | prices (git 52fd2fe), corporate actions, security events, supplementary prices, quarantine and symbol-change data |
| `data/ledger_evidence/` | the 18 verified historical entry-fee rows |
| `v5_modules/` | the exact code that produced the replay (7 files) |
| `reference/recorded_ledger_52fd2fe/` | the original recorded ledger at 6 Oct, for comparison only |
| `reports/` | trade differences, value bridge, fee treatments |
| `REPRODUCTION_MANIFEST.json` | every file's role, origin (git commit, or branch working tree), git blob id and SHA-256; configuration; environment |
| `SHA256SUMS`, `reproduce.py` | integrity list and reproduction check |

## Known limitations

- The 0.53 Torrent fraction is pending; this version values it only as a reference mark.
- The code, corporate-action, security-event, supplementary-price, quarantine, symbol-change and fee-evidence
  files were uncommitted branch work when archived; their exact bytes are in this folder and pinned by hash.
- Reproduction is verified only with the versions above.
- `data/market_database/corporate_actions.csv` also lists four announced future events (BUILDPRO, MOLDTECH,
  MOLDTKPAC, BANSALWIRE); none affects any session to 6 Oct.
