"""
benchmark_tracker.py
====================
Tracks the official NIFTY 500 index and compares it against the paper-trade
portfolio, so you can answer the ONLY question that decides go-live:
"Is the system actually beating the market?"

Two jobs:
  1. FETCH: download the latest NIFTY 500 closing value from NSE's daily
     "ind_close_all_DDMMYYYY.csv" archive (same source + session trick the
     bhavcopy downloader already uses) and append it to benchmark_history.csv.
     Idempotent — a date already stored is skipped.
  2. COMPARE: align the benchmark to the portfolio's equity_log dates, rebase
     it to the same starting capital, and compute:
        - portfolio return since start
        - benchmark (Nifty 500) return since start
        - excess return (alpha) = portfolio - benchmark
        - Information Ratio = mean(daily excess) / std(daily excess) * sqrt(252)
     Results go to benchmark_summary.json and benchmark_compare.csv.

DESIGN RULE: this module must NEVER crash the pipeline. Every path is wrapped
so the worst case is "no benchmark update today", not a failed daily refresh.
The workflow should still call it with a trailing `|| true` for belt-and-braces.

Usage:
    python benchmark_tracker.py \
        --benchmark-csv data/market_database/benchmark_history.csv \
        --equity-log    data/live_signals/equity_log.csv \
        --output-dir    data/live_signals \
        --portfolio-inr 200000 \
        --max-back-days 7
"""
from __future__ import annotations

import argparse
import io
import json
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import requests
except Exception:  # requests missing shouldn't kill anything
    requests = None


NSE_INDEX_URL = (
    "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"
)
NSE_HOME = "https://www.nseindia.com/"
INDEX_NAME = "nifty 500"  # matched case-insensitively against the "Index Name" column

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


# =========================================================
# Fetch
# =========================================================
def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/csv,application/csv,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_HOME,
    })
    try:
        s.get(NSE_HOME, timeout=15)
    except Exception:
        pass
    return s


def parse_nifty500_close(csv_text: str) -> float | None:
    """Extract the NIFTY 500 'Closing Index Value' from an ind_close_all CSV."""
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception:
        return None
    # Normalize column names: strip, lower, collapse spaces
    df.columns = [str(c).strip().lower() for c in df.columns]
    name_col = next((c for c in df.columns if "index name" in c), None)
    close_col = next((c for c in df.columns if "closing index value" in c), None)
    if name_col is None or close_col is None:
        return None
    m = df[df[name_col].astype(str).str.strip().str.lower() == INDEX_NAME]
    if m.empty:
        return None
    try:
        return float(str(m.iloc[0][close_col]).replace(",", ""))
    except Exception:
        return None


def fetch_one_day(d: date, session=None) -> float | None:
    if requests is None:
        return None
    url = NSE_INDEX_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
    s = session or _session()
    for attempt in range(3):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200 and len(r.text) > 200:
                return parse_nifty500_close(r.text)
            if r.status_code in (403, 404):
                return None
            time.sleep(2 ** attempt)
        except Exception:
            time.sleep(2 ** attempt)
    return None


def update_benchmark_history(bench_csv: Path, max_back_days: int = 7,
                             backfill_days: int = 45, min_history: int = 5) -> int:
    """Append any missing recent NIFTY 500 closes to benchmark_history.csv. Returns rows added.

    On the FIRST run (history missing or shorter than `min_history` rows), automatically
    widens the lookback to `backfill_days` so the whole paper-trade period is seeded in
    one go. Every run after that uses the normal `max_back_days` top-up.
    """
    bench_csv.parent.mkdir(parents=True, exist_ok=True)
    if bench_csv.exists():
        hist = pd.read_csv(bench_csv)
        hist.columns = [c.strip().upper() for c in hist.columns]
        have = set(hist["DATE"].astype(str))
    else:
        hist = pd.DataFrame(columns=["DATE", "NIFTY500_CLOSE"])
        have = set()

    lookback = max_back_days
    if len(have) < min_history:
        lookback = max(max_back_days, backfill_days)
        print(f"[INFO] benchmark history is new/short ({len(have)} rows) — one-time backfill of {lookback} days.")

    # Reuse a single NSE session across the whole (possibly long) backfill loop.
    sess = _session() if requests is not None else None

    new_rows = []
    for offset in range(lookback):
        d = date.today() - timedelta(days=offset)
        if d.weekday() >= 5:  # skip Sat/Sun
            continue
        key = d.strftime("%Y%m%d")
        if key in have:
            continue
        close = fetch_one_day(d, session=sess)
        if close is not None and close > 0:
            new_rows.append({"DATE": key, "NIFTY500_CLOSE": close})
            print(f"[INFO] NIFTY 500 {d.isoformat()} close = {close:,.2f}")
        time.sleep(0.4)  # be polite to NSE during a long backfill

    if not new_rows:
        print("[INFO] benchmark: no new NIFTY 500 rows added (already current, holiday, or fetch unavailable).")
        return 0

    out = pd.concat([hist, pd.DataFrame(new_rows)], ignore_index=True)
    out["DATE"] = out["DATE"].astype(str)
    out = out.drop_duplicates(subset=["DATE"]).sort_values("DATE")
    out.to_csv(bench_csv, index=False)
    print(f"[INFO] benchmark: appended {len(new_rows)} rows -> {bench_csv}")
    return len(new_rows)


# =========================================================
# Compare
# =========================================================
def compute_comparison(bench_csv: Path, equity_log: Path, portfolio_inr: float,
                       from_first_position: bool = True) -> dict:
    """Align benchmark to portfolio equity dates, rebase, and compute return/alpha/IR.

    from_first_position=True (default): start the comparison on the first day the
    portfolio actually held a position (N_OPEN > 0), so the pre-trade cash period
    (when the strategy wasn't invested yet) doesn't unfairly distort the headline
    vs a fully-invested index.
    """
    summary = {"as_of": None, "status": "no_data"}
    if not equity_log.exists() or not bench_csv.exists():
        return summary

    eq = pd.read_csv(equity_log)
    if eq.empty or "EQUITY" not in eq.columns:
        return summary
    eq["DATE"] = pd.to_datetime(eq["DATE"], errors="coerce")
    eq = eq.dropna(subset=["DATE"]).sort_values("DATE")

    # Fair start: drop the pre-trade cash days so we compare invested-vs-index.
    if from_first_position and "N_OPEN" in eq.columns:
        invested = eq[pd.to_numeric(eq["N_OPEN"], errors="coerce").fillna(0) > 0]
        if not invested.empty:
            eq = eq[eq["DATE"] >= invested["DATE"].iloc[0]]

    bench = pd.read_csv(bench_csv)
    bench.columns = [c.strip().upper() for c in bench.columns]
    bench["DATE"] = pd.to_datetime(bench["DATE"].astype(str), format="%Y%m%d", errors="coerce")
    bench = bench.dropna(subset=["DATE"]).sort_values("DATE")
    if bench.empty:
        return summary

    # Merge benchmark onto portfolio dates (as-of: use the latest benchmark <= each equity date)
    merged = pd.merge_asof(
        eq[["DATE", "EQUITY"]], bench[["DATE", "NIFTY500_CLOSE"]],
        on="DATE", direction="backward",
    ).dropna(subset=["NIFTY500_CLOSE"])
    if len(merged) < 2:
        return {"as_of": eq["DATE"].max().strftime("%Y-%m-%d"), "status": "insufficient_overlap"}

    start_eq = float(merged["EQUITY"].iloc[0])
    start_bench = float(merged["NIFTY500_CLOSE"].iloc[0])
    merged["BENCH_NAV"] = portfolio_inr * (merged["NIFTY500_CLOSE"] / start_bench)
    # Rebase portfolio too (in case start_eq != portfolio_inr)
    merged["PORT_NAV"] = portfolio_inr * (merged["EQUITY"] / start_eq)

    port_ret = float(merged["EQUITY"].iloc[-1] / start_eq - 1)
    bench_ret = float(merged["NIFTY500_CLOSE"].iloc[-1] / start_bench - 1)
    excess = port_ret - bench_ret

    # Information Ratio from daily excess returns
    merged["PORT_DRET"] = merged["EQUITY"].pct_change()
    merged["BENCH_DRET"] = merged["NIFTY500_CLOSE"].pct_change()
    merged["EXCESS_DRET"] = merged["PORT_DRET"] - merged["BENCH_DRET"]
    ex = merged["EXCESS_DRET"].dropna()
    if len(ex) >= 2 and ex.std(ddof=1) > 0:
        ir = float(ex.mean() / ex.std(ddof=1) * np.sqrt(252))
    else:
        ir = None

    summary = {
        "as_of": merged["DATE"].iloc[-1].strftime("%Y-%m-%d"),
        "start_date": merged["DATE"].iloc[0].strftime("%Y-%m-%d"),
        "n_days": int(len(merged)),
        "portfolio_return_pct": round(port_ret * 100, 2),
        "benchmark_return_pct": round(bench_ret * 100, 2),
        "excess_return_pct": round(excess * 100, 2),
        "information_ratio": round(ir, 2) if ir is not None else None,
        "beating_market": bool(excess > 0),
        "status": "ok",
    }
    # Persist the aligned series for the Excel/plots
    merged_out = merged[["DATE", "PORT_NAV", "BENCH_NAV"]].copy()
    merged_out["DATE"] = merged_out["DATE"].dt.strftime("%Y-%m-%d")
    summary["_compare_rows"] = merged_out.to_dict(orient="records")
    return summary


# =========================================================
# Main — fully guarded so it can never fail the pipeline
# =========================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark-csv", default="./data/market_database/benchmark_history.csv")
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--output-dir", default="./data/live_signals")
    ap.add_argument("--portfolio-inr", type=float, default=200000.0)
    ap.add_argument("--max-back-days", type=int, default=7)
    ap.add_argument("--backfill-days", type=int, default=45,
                    help="On the first run (empty history), fetch this many days back to seed the full period.")
    ap.add_argument("--no-fetch", action="store_true", help="Skip the network fetch; only recompute from stored history.")
    ap.add_argument("--include-cash-startup", action="store_true",
                    help="Include the pre-trade cash days in the comparison (default: start from first position for a fair vs-index test).")
    args = ap.parse_args()

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    # --- fetch (guarded) ---
    if not args.no_fetch:
        try:
            update_benchmark_history(Path(args.benchmark_csv), args.max_back_days,
                                     backfill_days=args.backfill_days)
        except Exception as e:
            print(f"[WARN] benchmark fetch failed (non-fatal): {e}")

    # --- compare (guarded) ---
    try:
        summary = compute_comparison(Path(args.benchmark_csv), Path(args.equity_log), args.portfolio_inr,
                                     from_first_position=not args.include_cash_startup)
    except Exception as e:
        print(f"[WARN] benchmark comparison failed (non-fatal): {e}")
        summary = {"status": "error", "error": str(e)}

    # Write compare CSV + summary JSON
    try:
        rows = summary.pop("_compare_rows", None)
        if rows:
            pd.DataFrame(rows).to_csv(outdir / "benchmark_compare.csv", index=False)
        with open(outdir / "benchmark_summary.json", "w") as f:
            json.dump(summary, f, indent=2, default=str)
    except Exception as e:
        print(f"[WARN] benchmark write failed (non-fatal): {e}")

    if summary.get("status") == "ok":
        print(
            f"[DONE] vs NIFTY 500 ({summary['start_date']}→{summary['as_of']}, {summary['n_days']}d): "
            f"portfolio {summary['portfolio_return_pct']:+.2f}%  vs  market {summary['benchmark_return_pct']:+.2f}%  "
            f"→ {'AHEAD' if summary['beating_market'] else 'BEHIND'} by {summary['excess_return_pct']:+.2f}%  "
            f"| IR {summary['information_ratio']}"
        )
    else:
        print(f"[DONE] benchmark status: {summary.get('status')}")


if __name__ == "__main__":
    main()
