# Research & recommendation system — architecture

Decision support for **personal use** on Indian listed equities. It produces
**candidate ideas, not orders**, and its default output is **NO TRADE**.
Recommending specific stocks to other people, or automating orders through a
broker, needs a separate regulatory (SEBI research-analyst / investment-adviser
rules) and broker review before any of this code is used that way.

```
                 ┌──────────────── shared services ────────────────┐
 public data ──▶ │ data quality · market state · company graph ·   │
 (NSE, FRED/     │ event intelligence · trading calendar           │
  Yahoo, filings)└───────┬──────────────┬─────────────┬────────────┘
                         ▼              ▼             ▼
          ┌──────────────┐ ┌──────────┐ ┌──────────────┐ ┌───────────────┐
          │ investment   │ │ swing    │ │ equity       │ │ intraday      │
          │ months-years │ │ days-wks │ │ derivatives  │ │ short (live)  │
          └──────┬───────┘ └────┬─────┘ └──────┬───────┘ └──────┬────────┘
                 └──── candidates (thesis, plan, evidence, confidence) ────┘
                                        ▼
                     ┌─────────── central risk engine ───────────┐
                     │ deterministic; final veto; sizes from the │
                     │ risk budget and the invalidation distance │
                     └───────────────────┬───────────────────────┘
                                         ▼
                     report (Markdown / Telegram) · audit log · outcomes
```

## Layout

| Path | Role |
|---|---|
| `research_system/run.py` | Orchestrator, one entry point per session phase |
| `research_system/data/` | Fetchers: `indices` (Nifty 500, ~60 sectors, India VIX, G-Sec), `macro` (USD/INR, Brent, gold, US 10Y, S&P 500, DXY), `flows` (FII/DII cash, participant-wise derivatives OI), `fno` (contract universe, daily contracts, IV summary), `events` (results calendar, filings), `calendar` (NSE holidays, phases), `prices` (split-adjusted EOD), `quality` (freshness) |
| `research_system/market_state.py` | Broad / sector / security regimes, risk-appetite composite, measured macro relationships, sector macro betas |
| `research_system/company_graph.py` | Sourced, dated relationship edges and transmission paths |
| `research_system/event_intel.py` | Filing classification, measured price/volume response, expectation-gap flag (only with an explicit estimate) |
| `research_system/engines/` | `investment`, `swing`, `fno`, `intraday_short` |
| `research_system/risk_engine.py` | Central risk engine |
| `v5_modules/allocator.py` | Shared sizing and exposure allocation (risk engine and backtest; not yet the paper ledger) |
| `research_system/candidate.py`, `confidence.py` | Recommendation record and confidence grade |
| `research_system/audit.py` | Audit log, outcome replay, calibration |
| `research_system/setup_registry.json` | Every admitted setup with its historical record |
| `config/risk_config.example.toml` | Risk configuration template |
| `v5_modules/corporate_actions.py` | Split/bonus adjustment used by everything |

## Session schedule

Times come from `config [session]`; holidays come from NSE's published holiday
master, cached in `data/research/nse_holidays.json` and refreshed weekly.
NSE's published equity schedule is a 09:00–09:15 IST pre-open and a 09:15–15:30
IST regular session. Check special sessions against the exchange calendar.

| Phase | What runs | Where |
|---|---|---|
| **pre_market** (before 09:00) | overnight global moves, measured lead relationships, new filings since the last run, re-check of the latest approved ideas, data quality | GitHub Actions 08:15 IST |
| **pre_open** (09:00–09:15) | indicative prices and gap marking. **Needs a live feed.** Indicative prices are never treated as fills | live host only |
| **regular** (09:15–15:30) | intraday engine, invalidation of open ideas. **Needs a live feed.** | live host only |
| **post_close** | official prices, flows, F&O data, filings → market state → engines → risk engine → report → audit → outcome replay | GitHub Actions 23:00 IST |

Without a live feed the system runs the two end-of-day phases. The live phases
return NO TRADE with the reason, so nothing pretends to be real time. A live
deployment needs an always-on host (see `Oracle_Cloud_Setup_Guide.md`) and an
`IntradayFeed` implementation (`engines/intraday_short.py`) for your broker or
data vendor. Credentials stay in that host's environment. This package never
reads them and never places orders.

## Data sources (public, lawfully published)

| Data | Source | Notes |
|---|---|---|
| Equity EOD | NSE `sec_bhavdata_full` | existing V5 downloader |
| Splits / bonuses | NSE corporate-actions API; inferred from the tape only when no circuit band could explain a gap | `data/market_database/corporate_actions.csv` |
| Index closes, P/E | NSE `ind_close_all` | backfilled from 2017 |
| Bonds | Nifty 10 yr Benchmark G-Sec (and composite) index | price/total-return index, not a yield |
| USD/INR, Brent, gold, US 10Y, S&P 500, DXY | FRED (official) with Yahoo fallback | the source is stored on every row |
| FII/DII cash flows | NSE `fiidiiTradeReact` | NSE publishes only the latest day; history accumulates from first run |
| FII derivatives positioning | NSE participant-wise OI archive | backfillable |
| F&O contracts | NSE `fo_mktlots.csv` + UDiFF F&O bhavcopy | full files kept 5 days; summary (ATM IV, basis, OI, PCR) kept forever |
| Results calendar, filings | NSE board-meeting and announcement APIs | filings pruned after 45 days |
| Fundamentals | Screener.in (existing scraper) | current snapshot; PIT vintages archived quarterly |

Private, leaked or material non-public information is never an input.

## Market state

Separate, timestamped views:

- **Broad:** trend (Nifty 500 vs 200-DMA), volatility (realised, India VIX percentile), breadth (% liquid names above 50-DMA), liquidity (turnover vs 120-day), valuation (Nifty 500 P/E percentile), the V5 three-trigger regime, and a **risk-appetite composite**. That composite is the mean z-score of small vs large caps, India VIX change (inverted), INR strength, FII cash flow (once 60 sessions exist), FII index-futures long share, and breadth.
- **Sector:** equal-weighted Nifty 500 industry groups. Relative strength vs Nifty 500, trend, breadth, volatility, rank.
- **Security:** computed per candidate.

**Macro relationships are measured, never assumed.** For gold, G-Sec bonds,
USD/INR, Brent, US 10Y, S&P 500, India VIX and FII flows vs daily Nifty 500
returns, each run reports:

- the full-sample correlation with a 95% interval
- the latest 126-day correlation and beta
- **sign agreement**: how often the rolling sign matched the full sample
- correlation inside each regime and volatility state
- lead-lag correlations (factor on day t−k vs India on day t)

Each relationship is then labelled stable / moderate / unstable / weak /
insufficient. Global series close after India, so same-day correlations are
descriptive. The lead correlations are the decision-relevant ones.

Sector **partial betas** to each macro factor (controlling for the Nifty 500,
252 days, kept only when |t| ≥ 2) feed the risk engine's factor-exposure report.

## Engines

| Engine | Setups today | Validation status | Default outcome |
|---|---|---|---|
| Investment | `quality_at_reasonable_value`: quality ≥ 80th pct, P/E ≤ industry median, D/E ≤ 1, positive cash flow | cannot be backtested until point-in-time fundamentals accumulate. Grade capped at C | candidates, risk engine decides |
| Swing | `momentum_lowvol_rank` (corrected V5) | backtest 2018–2026, 776 trades, +2.3%/trade after charges, t 2.9, weaker second half (t 1.2). Grade capped at C until live outcomes exist | candidates, risk engine decides |
| Equity derivatives | `view_expression_bull_call_spread` on swing views | option expression not backtested. Grade capped at C | candidates, risk engine decides |
| Intraday short | `orb_breakdown_relative_weakness` | not validated (no recorded intraday data). Grade capped at D | NO TRADE (needs feed + validation) |

New setups enter only through the registry. Test them first, e.g. with
`v5_modules/swing_signal_lab.py`, using a train/validation split and the
validation window spent once. Then add them with `research_system/setup_stats.py`.

The derivatives engine checks the **current NSE contract list** first: a listed
equity is not automatically F&O-eligible. NSE stock options are physically
settled, so every plan exits three sessions before expiry.

## Confidence

A raw ranking score is never shown as a probability. Each candidate shows a
grade (A–E) and its components, each scored 0–1 with a note:

- signal strength
- agreement among independent evidence
- regime fit
- historical setup results
- liquidity
- data freshness
- model certainty

Missing evidence scores low, not neutral. Hard caps are listed: unvalidated
setup, non point-in-time data, no out-of-sample record. A **percentage** appears
only after `audit.calibration()` has at least 200 finished out-of-sample
outcomes for that setup. It is shown with the event definition, horizon,
sample size and period.

## Central risk engine (final veto)

Deterministic checks, in order. Each is recorded on the candidate as PASS / FAIL / ADJUST / INFO:

1. **Risk budget configured.** Nothing is approved until you set it.
2. Engine enabled.
3. **Data freshness** per engine (allowed lag in `[data]`).
4. **Kill switch:** drawdown and daily loss limits.
5. Confidence floor per engine.
6. **Instrument eligibility:** F&O contract list, price/ADV floors.
7. **Strategy rules:** no naked short options, derivatives need margin configured, intraday shorts never overnight, no overnight cash shorts (SLB not supported).
8. **Event risk:** results/ex-dates near entry (`results_action`) or inside the hold (`in_hold_action`).
9. **Macro overlay:** when risk appetite is below the threshold, size is reduced. It never increases size.
10. **Sizing:** `floor(per-trade loss budget / loss per unit at the invalidation)`. Loss per unit includes the gap allowance and charges. The result is capped by max position %, ADV participation, free capital and free margin. Lots for derivatives.
11. **Exposure:** gross cap scaled by regime (RISK_ON 100% / NEU 70% / OFF 40% of your gross limit, the V5-backtested schedule), net cap, position counts, duplicates.
12. **Sector** concentration.

Steps 10–12 are one function, `size_one()` in `v5_modules/allocator.py`, shared
with the backtest (`backtest_regime.py --entry-allocation shared`). Each engine
states its loss per unit in three parts: stop distance, gap allowance and costs
(`Candidate.unit_loss_parts`). The allocator rejects a candidate when any part is
not stated (the investment engine has no gap allowance configured), when the
sector, 20-day traded value or market regime is unknown, or when the policy or
book is incomplete. It never substitutes a default. The regime changes size in
one place only: the gross cap. Candidates are taken by confidence, and ties are
broken by engine, symbol and id. The backtest's earlier rules (`rank_order`,
`pro_rata_leftover`) stay available, unchanged, for comparison. The paper ledger
still sizes from the signal file and is not connected.
13. **Correlation** with open positions (max pairwise and average).
14. **Factor exposure:** beta-weighted gross and sector macro betas. Caps are optional; exposures are always reported.

Candidates are evaluated in confidence order against a running book, so later
ideas see the exposure that earlier approvals would add. The book is
`data/research/portfolio.csv` if you maintain one, otherwise the V5 paper
ledger. From the ledger, every active state counts (`v5_modules/ledger_view.py`):
open, stale and exit-offer-locked shares and pending entitlements are share
exposure at their last valid mark; cash owed (dividends, exit or merger
consideration) is committed capital that cannot be spent yet; slots and
duplicates follow the ledger's own rules; available cash is the ledger's last
logged cash, used only if it reconciles with the positions file. If shares have
no valid mark or the cash does not reconcile, the engine approves nothing and
says why (check `book_view`).

The cash check rebuilds the logged cash of every equity-log date from the dated
movements in the positions file (purchases with the entry fee actually charged,
and the net cash of every closed row). An entry fee of zero counts only with its
stated basis: entries made before the cost model (3 Sep 2026) were charged no
fee, and that zero is written only where a verified record in
`data/ledger_evidence/historical_entry_fees.csv` (code in force, the run that
wrote the row) and the entry day's equity-log row both establish it. Fees
estimated with today's cost model for those entries are kept apart in
`data/ledger_evidence/entry_fee_estimates.csv`, labelled as estimates, and never
enter cash, cost or P&L. A fractional entitlement is never cash: it carries its
share of the cost until its event records the sale proceeds (then owed) and the
pay date (then cash).

A holding touched by a verified event the ledger had not applied is replayed
from its original entry with the exit rules and fees in force on each date
(`PAPER_RULES` in `paper_trade_manager.py`): to the 2 Sep 2026 session, highest
close − 3×ATR14 and the 22-session low, a 25-calendar-day time stop and no fees;
from the 3 Sep 2026 session, highest close − 4×ATR14, no time stop, fees on both
legs. A stop is never lowered when the rule changes, and ATR, the rolling low and
stops are all in each session's as-traded prices. If the replay's exit (or a
change in slot use) falls on a session the recorded history already covers, its
rows are provisional and the cash is not verified: the later entries, exits, cash
and slots were produced without it. `v5_modules/corrected_replay.py` then rebuilds
that history session by session into a separate, labelled copy ("corrected
replay"); the recorded ledger stays the original history. Applying today's rules
to earlier dates is available only as a labelled counterfactual preview that is
never written (`--counterfactual-today-rules`).

An open holding becomes stale after three consecutive sessions without a valid
print (open and close present and positive); the count is rebuilt from the price
data on every run, so it carries across separate daily runs. The ledger stops
before writing anything if the supplementary price file is missing (a header-only
file states that there are none).

Stops are planned controls, not guaranteed execution prices. Gaps, halts, thin
order books, and rejected or partial orders can make the realised loss exceed
the planned amount.

## Configuring

```bash
cp config/risk_config.example.toml config/risk_config.toml
# edit: [account] configured = true, capital, and every REQUIRED limit
python -m research_system.run --phase post_close --no-refresh   # dry run on stored data
```

The values are yours to choose; the example file deliberately ships with zeros.
GitHub Actions reads `config/risk_config.toml` from the repository, so commit it
if you want the scheduled runs to approve anything.

## Audit and outcomes

Every candidate (approved and rejected) and every NO TRADE is appended to
`data/research/audit/YYYYMMDD.jsonl`. Each post-close run replays past cash
candidates: next-open fill if inside the entry zone, then first touch of
stop or target, gap-through fills at the open. Outcomes go to
`audit/outcomes.parquet`. They are out of sample by construction, and they are
what eventually removes the "no out-of-sample record" cap and calibrates a
probability.

## Known limits

- Fundamentals are a current snapshot. Investment ideas cannot be validated until about 6–8 quarterly PIT vintages exist.
- Financials (banks, NBFCs, insurers) are excluded from the investment engine. Their metrics (NIM, credit cost, GNPA, CAR) are not collected yet.
- FII cash-flow history starts on the first run; the derivatives positioning series covers the gap.
- Option candidates use end-of-day settlement prices, not live bid/ask, and margin is an estimate.
- The intraday engine has no validated setup and no feed.
- The company graph is seeded only with industry links. Disclosed supplier/customer edges must be added from documents (`company_graph.py --import`).
- The swing setup's edge has weakened (second half t = 1.2). Treat it as a hypothesis that live outcomes will confirm or reject.
