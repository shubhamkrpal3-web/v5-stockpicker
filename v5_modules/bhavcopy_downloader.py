"""
bhavcopy_downloader.py
======================
NSE EOD bhavcopy fetcher. Downloads sec_bhavdata_full_DDMMYYYY.csv for
the latest available trading day, parses, and appends new rows to
master_history.csv.

The NSE archive URL pattern (as of 2026):
  https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv

Skips weekends, NSE holidays, and dates already present in master.

Usage from cron / orchestrator:
    python bhavcopy_downloader.py --master ./data/market_database/master_history.csv \\
                                  --max-back-days 7
"""
from __future__ import annotations

import argparse
import io
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests


NSE_BHAV_URL = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)
NSE_HOME = "https://www.nseindia.com/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


def _session_with_cookies() -> requests.Session:
    """NSE needs a cookie from the homepage before allowing archive downloads."""
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


def fetch_one(d: date) -> pd.DataFrame | None:
    ddmmyyyy = d.strftime("%d%m%Y")
    url = NSE_BHAV_URL.format(ddmmyyyy=ddmmyyyy)
    s = _session_with_cookies()
    for attempt in range(3):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200 and len(r.text) > 500:
                df = pd.read_csv(io.StringIO(r.text))
                df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
                # Normalize the columns to what the master schema expects
                if "TRADE_DATE" in df.columns:
                    df["DATE"] = pd.to_datetime(df["TRADE_DATE"], errors="coerce").dt.strftime("%Y%m%d")
                else:
                    df["DATE"] = d.strftime("%Y%m%d")
                rename = {
                    "OPEN_PRICE": "OPEN", "HIGH_PRICE": "HIGH", "LOW_PRICE": "LOW",
                    "CLOSE_PRICE": "CLOSE", "TTL_TRD_QNTY": "VOLUME",
                    "TURNOVER_LACS": "TRADED_VALUE",
                }
                for old, new in rename.items():
                    if old in df.columns and new not in df.columns:
                        df = df.rename(columns={old: new})
                # TURNOVER_LACS is in lakhs of rupees, convert to rupees
                if "TRADED_VALUE" in df.columns and "TURNOVER_LACS" in rename.values():
                    df["TRADED_VALUE"] = pd.to_numeric(df["TRADED_VALUE"], errors="coerce") * 100_000
                df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
                if "SERIES" in df.columns:
                    df["SERIES"] = df["SERIES"].astype(str).str.strip().str.upper()
                    df = df[df["SERIES"] == "EQ"].copy()
                if "CORPORATE_ACTION_FLAG" not in df.columns:
                    df["CORPORATE_ACTION_FLAG"] = 0
                return df
            if r.status_code in (403, 404):
                # 403/404 → likely a holiday or pre-listing date; not retried
                return None
            time.sleep(2 ** attempt)
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def append_to_master(new_rows: pd.DataFrame, master_path: Path) -> int:
    """Append new_rows to master if those dates aren't already present. Returns rows added."""
    master_path.parent.mkdir(parents=True, exist_ok=True)
    if master_path.exists():
        existing = pd.read_csv(master_path, low_memory=False, usecols=lambda c: c.upper() in ("DATE", "SYMBOL"))
        existing.columns = [c.upper() for c in existing.columns]
        existing_key = set(zip(existing["DATE"].astype(str), existing["SYMBOL"].astype(str).str.upper()))
        new_key = set(zip(new_rows["DATE"].astype(str), new_rows["SYMBOL"]))
        truly_new = new_key - existing_key
        if not truly_new:
            return 0
        mask = list(zip(new_rows["DATE"].astype(str), new_rows["SYMBOL"])).__iter__()
        # vectorized version
        key_series = pd.Series(list(zip(new_rows["DATE"].astype(str), new_rows["SYMBOL"])))
        new_rows = new_rows[key_series.isin(truly_new)].copy()
        new_rows.to_csv(master_path, mode="a", header=False, index=False,
                        columns=existing.columns if False else None)
    else:
        new_rows.to_csv(master_path, index=False)
    return len(new_rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", required=True)
    ap.add_argument("--max-back-days", type=int, default=7,
                    help="Try up to this many days back if today's file isn't available yet.")
    args = ap.parse_args()

    master_path = Path(args.master)
    added_total = 0
    tried_dates = []
    for offset in range(args.max_back_days):
        d = date.today() - timedelta(days=offset)
        if d.weekday() >= 5:  # Sat/Sun
            continue
        tried_dates.append(d.strftime("%Y-%m-%d"))
        df = fetch_one(d)
        if df is None:
            continue
        added = append_to_master(df, master_path)
        print(f"[INFO] {d.strftime('%Y-%m-%d')}: fetched {len(df)} rows, appended {added} new to master")
        added_total += added
    if not added_total:
        print(f"[WARN] No new rows added. Tried dates: {tried_dates}")
    else:
        print(f"[DONE] Total rows added to master: {added_total}")


if __name__ == "__main__":
    main()
