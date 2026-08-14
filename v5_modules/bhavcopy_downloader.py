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

                # Guard against "phantom sessions": on NSE holidays the archive can
                # return the PREVIOUS trading day's file with a 200 response. Stamping
                # it with the requested date would create a duplicate session with zero
                # returns, which deflates volatility and skews moving averages. Trust
                # the file's own date column and reject any mismatch.
                _date_col = next((c for c in ("DATE1", "TRADE_DATE", "TIMESTAMP") if c in df.columns), None)
                if _date_col is not None:
                    _parsed = pd.to_datetime(df[_date_col].astype(str).str.strip(),
                                             errors="coerce", dayfirst=True).dropna()
                    if not _parsed.empty and _parsed.mode().iloc[0].date() != d:
                        print(f"[SKIP] {d}: file actually contains "
                              f"{_parsed.mode().iloc[0].date()} (holiday/stale) — not appended")
                        return None

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
                had_turnover_lacs = "TURNOVER_LACS" in df.columns
                for old, new in rename.items():
                    if old in df.columns and new not in df.columns:
                        df = df.rename(columns={old: new})
                # TURNOVER_LACS is in lakhs of rupees, convert to rupees
                if had_turnover_lacs and "TRADED_VALUE" in df.columns:
                    df["TRADED_VALUE"] = pd.to_numeric(df["TRADED_VALUE"], errors="coerce") * 100_000
                df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
                if "SERIES" in df.columns:
                    df["SERIES"] = df["SERIES"].astype(str).str.strip().str.upper()
                    df = df[df["SERIES"] == "EQ"].copy()
                if "CORPORATE_ACTION_FLAG" not in df.columns:
                    df["CORPORATE_ACTION_FLAG"] = 0
                return df.reset_index(drop=True)
            if r.status_code in (403, 404):
                # 403/404 → likely a holiday or pre-listing date; not retried
                return None
            time.sleep(2 ** attempt)
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def append_to_master(new_rows: pd.DataFrame, master_path: Path) -> int:
    """Append new_rows to master if those (DATE, SYMBOL) keys aren't already present.

    Returns the number of rows actually appended.
    """
    master_path.parent.mkdir(parents=True, exist_ok=True)

    # First-ever write: just dump the file directly with full headers
    if not master_path.exists():
        new_rows.to_csv(master_path, index=False)
        return len(new_rows)

    # Build a set of existing (DATE, SYMBOL) keys from the master so we don't
    # double-insert
    existing = pd.read_csv(master_path, low_memory=False, usecols=["DATE", "SYMBOL"])
    existing.columns = [c.upper() for c in existing.columns]
    existing_keys = set(zip(
        existing["DATE"].astype(str),
        existing["SYMBOL"].astype(str).str.upper(),
    ))

    # Make a clean copy with a fresh RangeIndex to avoid any index-alignment
    # surprises when boolean-indexing below.
    new_rows = new_rows.reset_index(drop=True).copy()
    new_rows["SYMBOL"] = new_rows["SYMBOL"].astype(str).str.upper()
    new_rows["DATE"] = new_rows["DATE"].astype(str)

    # Build the keep mask the simple, explicit way (no Series alignment)
    keep = [
        (d, s) not in existing_keys
        for d, s in zip(new_rows["DATE"], new_rows["SYMBOL"])
    ]
    truly_new = new_rows.loc[keep].copy()
    if truly_new.empty:
        return 0

    # Ensure the appended rows write in the same column order as the master,
    # so the CSV stays readable. Any missing columns get NaN; extra columns
    # get dropped.
    master_cols = pd.read_csv(master_path, low_memory=False, nrows=0).columns.tolist()
    truly_new = truly_new.reindex(columns=master_cols)
    truly_new.to_csv(master_path, mode="a", header=False, index=False)
    return len(truly_new)


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
