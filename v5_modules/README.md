# V5 Modules — Production Build

Drop these on your Oracle Cloud VM under `/home/ubuntu/v5/v5_modules/`. See `Oracle_Cloud_Setup_Guide.md` (one directory up) for end-to-end deployment.

## Production scripts (called by cron)

| File | Purpose | Called by |
|---|---|---|
| `bhavcopy_downloader.py` | Downloads NSE EOD bhavcopy, appends new rows to master_history.csv. Idempotent. | `daily_data_refresh.py` |
| `corporate_actions.py` | Split/bonus events from NSE's corporate-actions feed (validated against the price gap; inferred only for gaps no circuit band allows) -> `data/market_database/corporate_actions.csv`. Prices are back-adjusted in memory at load time; raw data is never rewritten. Held paper positions are rescaled on the ex-date. | `daily_refresh.yml` |
| `screener_scraper.py` | Fetches fundamentals from Screener.in. SQLite-cached, rate-limited 1 req / 2 sec. | `daily_data_refresh.py` (and one-time bootstrap) |
| `v5_signal_generator.py` | The heart. Reads master + fundamentals + master, applies V5 multifactor + quality gate + regime overlay + ATR sizing, writes `daily_live_portfolio.csv`. | `daily_data_refresh.py` |
| `daily_data_refresh.py` | Nightly orchestrator. Calls the three above in sequence, logs everything, alerts via Telegram on failure. | cron at 21:30 IST |
| `telegram_broadcaster.py` | Three message types: weekly_signal (Monday), daily_exit (trading evenings), weekly_summary (Saturday). | cron (separate entries) |

## V5 architecture pieces (libraries imported by the above)

| File | What it provides | Tested |
|---|---|---|
| `pit_corrections.py` | Point-in-time factor shifts and OPEN-to-OPEN forward returns. Removes look-ahead bias. | ✓ (smoke test in `__main__`) |
| `multifactor_score.py` | V5 sleeve composition: 62.5% momentum (skip-month) + 37.5% low-vol (breadth removed 2026-10-04: it predicted the opposite of its intent), with hooks for quality + value when fundamentals are wired. | ✓ |
| `risk_overlay_fixed.py` | Properly enforcing per-stock + per-sector caps. Reproduces V4's cap-violation bug and demonstrates the fix. | ✓ |
| `realistic_cost_model.py` | Per-stock cost: brokerage + STT + stamp duty + exchange + GST + slippage scaled to participation. | ✓ |
| `quality_gate.py` | Mandatory fundamental & event filters. Uses cached Screener data. | ✓ scaffolding, ✓ fetchers via screener_scraper |
| `exit_engine.py` | ATR-based stops, chandelier trailing, 22-day low, time stops, portfolio kill switch. | ✓ |
| `regime_detector.py` | Three-state regime (RISK_ON/NEU/OFF) with debouncing. Embedded directly in v5_signal_generator. | ✓ |

## Templates

| File | Use |
|---|---|
| `positions_template.csv` | Empty schema for the positions ledger. Copy to `~/v5/data/live_signals/positions.csv` on first deploy. |

## Quick verification on Oracle (after deployment)

```bash
source ~/v5env/bin/activate
cd ~/v5

# 1. Test bhavcopy download
python v5_modules/bhavcopy_downloader.py --master ./data/market_database/master_history.csv

# 2. Test signal generation (will work even with empty fundamentals — quality gate becomes permissive)
python v5_modules/v5_signal_generator.py \
    --master ./data/market_database/master_history.csv \
    --fundamentals ./data/fundamentals/screener.sqlite \
    --output-dir ./data/live_signals

# 3. Dry-run a Telegram message (does not actually send)
python v5_modules/telegram_broadcaster.py \
    --type weekly_signal \
    --portfolio ./data/live_signals/daily_live_portfolio.csv \
    --regime-json ./data/live_signals/daily_regime.json \
    --dry-run
```

If step 3 prints a formatted message that looks like the example below, the pipeline is healthy.

## Sample broadcast output

```
*V5 SIGNAL — Mon 09 Jun 2026*
_Regime:_ `RISK_NEU`  (Trend✓  Breadth✓  VIX✗)
_Gross target:_ 70%   _Kill switch:_ clear
_Portfolio YTD:_ +0.00%   _DD from peak:_ +0.00%

*NEW ENTRIES (15):*
1. `LUPIN`       Conv 7.2  Entry@open ₹2276.2  Stop ₹2141.6  Size ₹9,333 (baseline 1.0R)
2. `POWERGRID`   Conv 7.1  Entry@open ₹305.9   Stop ₹293.0   Size ₹9,333 (baseline 1.0R)
...
```

## Honest caveats

**Update 2026-10-04.** Two data/model errors were fixed: prices were not adjusted for splits/bonuses, and the 20% breadth sleeve was predicting the opposite of its intent. The backtest benchmark also had look-ahead (same-day turnover membership). Re-run 2018-2026 on corrected data with the live exit rules: +134% (10.3%/yr, vol 12%, max drawdown -21.7%) vs the equal-weighted benchmark +129% (10.0%/yr, vol 21%, max drawdown -52%). The edge is weaker in the second half of the sample. The new multi-engine research system and central risk engine are described in `docs/ARCHITECTURE.md`.

The earlier variant search (on uncorrected data) concluded that V5 momentum/low-vol/breadth scoring **does not beat benchmark** on its own. The single remaining lever is the fundamental quality gate, which only takes effect after `screener_scraper.py` has bootstrapped a meaningful cache. **Do not trade real money until you've run the system end-to-end for 8 weeks of paper trading with the quality gate active.** See the audit blueprint (one directory up) for the full reasoning.

---

*Last updated: 06 Jun 2026*
