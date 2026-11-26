# Research run — post_close — 2026-11-26 01:08 UTC

> Decision support for personal use. Not investment advice. No orders are placed. The default output is NO TRADE.

**Approved candidates: 0** · rejected: 29 · risk config: `example (not configured)`

## Risk budget

**Not configured: every candidate is rejected (NO TRADE).** Copy `config/risk_config.example.toml` to `config/risk_config.toml` and set:

- account.configured is false
- account.capital_inr not set
- limits.per_trade_max_loss_inr not set
- limits.daily_loss_limit_inr not set
- limits.max_drawdown_pct not set
- limits.max_gross_exposure_pct not set
- limits.max_net_exposure_pct not set
- limits.max_position_pct not set
- limits.max_sector_pct not set
- limits.max_open_positions not set

## Data quality

| dataset | status | last | lag (sessions) | allowed lag |
|---|---|---|---|---|
| equity_eod | FRESH | 20261125 | 0 | 0 |
| indices | FRESH | 20261125 | 0 | 1 |
| macro | FRESH | 20261125 | 0 | 3 |
| fno_summary | FRESH | 20261125 | 0 | 0 |
| participant_oi | FRESH | 20261125 | 0 | - |
| fii_dii_cash | FRESH | 20261125 | 0 | - |
| corporate_actions | FRESH | 20261126 | 0 | - |
| fundamentals | FRESH | 20261126 | 0 | 30 |

Expected session: 20261125

## Market state

As of 2026-11-25: **RISK_NEU** · index NSE Nifty 500 · breadth 40% above 50-DMA · realised vol 14.9% · India VIX 17.67 · risk appetite z 0.454

Risk appetite components (z): inr_strength_20d +2.17, fii_index_futures_long_share -0.58, breadth -0.23

Institutional cash flows 2026-11-25: FII +4,479 cr, DII +4,019 cr (history: 4 sessions)

### Measured macro relationships (daily changes vs Nifty 500)

| factor | label | corr [95% CI] | 126d corr | 126d beta | sign agreement | best lead (k: corr) | n |
|---|---|---|---|---|---|---|---|
| gold_inr | weak | +0.021 [-0.02, +0.06] | 0.109 | 0.0569 | 0.51 | 1: +0.077 * | 2249 |
| gsec_10y_index | moderate | +0.098 [+0.06, +0.14] | 0.59 | 2.0972 | 0.73 | 1: -0.037 | 2407 |
| usdinr | stable | -0.106 [-0.15, -0.07] | -0.149 | -0.213 | 0.86 | 2: -0.035 | 2397 |
| brent | moderate | +0.058 [+0.02, +0.10] | -0.422 | -0.1007 | 0.66 | 3: +0.094 * | 2251 |
| us10y_bp | stable | +0.068 [+0.03, +0.11] | -0.157 | -0.0003 | 0.78 | 4: -0.078 * | 2245 |
| sp500 | stable | +0.248 [+0.21, +0.29] | 0.136 | 0.1272 | 0.97 | 1: +0.277 * | 2245 |
| india_vix_change | stable | -0.610 [-0.63, -0.58] | -0.75 | -0.0072 | 1.0 | 2: -0.090 * | 2407 |
| fii_cash_net_cr | insufficient | | | | | | 4 |
| fii_idxfut_net_change | stable | +0.348 [+0.30, +0.39] | 0.402 | 0.0 | 1.0 | 2: +0.071 * | 1311 |

_Relationships are measured, not assumed. Same-day values for global series overlap hours after the Indian close; lead correlations (factor on day t-k vs India on day t) are the decision-relevant ones. * = significant at ~95%._

## Investment

**NO TRADE**

Rejected by the risk engine:

| symbol | instrument | grade | stopped at | reason |
|---|---|---|---|---|
| FORCEMOT | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| SUZLON | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| GODFRYPHLP | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| IRCTC | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| HBLENGINE | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| NATIONALUM | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| POLYCAB | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| OFSS | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| EMMVEE | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| ACE | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |

- NO TRADE note: Financial Services excluded: bank/NBFC metrics (NIM, credit cost, GNPA, capital adequacy) are not yet collected

## Swing

**NO TRADE**

Rejected by the risk engine:

| symbol | instrument | grade | stopped at | reason |
|---|---|---|---|---|
| INDHOTEL | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| TECHM | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| ANANDRATHI | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| KOTAKBANK | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| CASTROLIND | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| ITBEES | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| LIQUIDPLUS | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| CRISIL | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| ITIETF | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| MANOMAY | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| MANBA | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| ITETF | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| ELIQUID | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| IT | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| HDFCNIFIT | NSE EQ (delivery) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |


## Equity derivatives

**NO TRADE**

Rejected by the risk engine:

| symbol | instrument | grade | stopped at | reason |
|---|---|---|---|---|
| INDHOTEL | Bull call spread 29-Dec-2026 910/1000 CE x1 lot (1000) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| ANANDRATHI | Bull call spread 29-Dec-2026 2380/2500 CE x1 lot (250) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| TECHM | Bull call spread 29-Dec-2026 1820/1980 CE x1 lot (600) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |
| KOTAKBANK | Bull call spread 29-Dec-2026 510/555 CE x1 lot (2000) | C | risk_budget | risk budget not configured (10 items; see the Risk budget section) |

- NO TRADE note: views without a suitable contract: MANBA: not in the current NSE F&O contract list; CASTROLIND: not in the current NSE F&O contract list; CRISIL: not in the current NSE F&O contract list; ITETF: not in the current NSE F&O contract list; ITBEES: not in the current NSE F&O contract list; HDFCNIFIT: not in the current NSE F&O contract list; IT: not in the current NSE F&O contract list; ITIETF: not in the current NSE F&O contract list; MANOMAY: not in the current NSE F&O contract list; LIQUIDPLUS: not in the current NSE F&O contract list; ELIQUID: not in the current NSE F&O contract list
- NO TRADE note: bearish expressions: no validated bearish setup feeds this engine yet

## Intraday short

**NO TRADE**

- NO TRADE note: engine disabled in config (needs a live intraday feed)

## Recent material filings for candidate names

| symbol | published | category | subject | day-0 abnormal | CAR 0-4 | turnover x | expectation gap |
|---|---|---|---|---|---|---|---|
| ANANDRATHI | 2026-11-25T14:35:06 | regulatory | Shareholders meeting | 0.0113 | 0.0113 | 1.01 | not assessed |
| KOTAKBANK | 2026-11-24T14:37:46 | capital_raise | ESOP/ESOS/ESPS | 0.0139 | 0.0139 | 0.77 | not assessed |
| MANOMAY | 2026-11-24T18:50:37 | regulatory | Updates | 0.0337 | 0.0337 | 1.73 | not assessed |
| TECHM | 2026-11-21T14:01:51 | capital_raise | ESOP/ESOS/ESPS | 0.0195 | 0.0334 | 1.35 | not assessed |

---
Stops are planned controls, not guaranteed execution prices. Gaps, halts, thin order books and rejected or partial orders can make the realised loss exceed the planned amount.