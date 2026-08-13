"""
historical_backfill.py
======================
Downloads NSE end-of-day price history back to ~2016 and stores it as one
Parquet file per year, for research and backtesting.

WHY THIS EXISTS
---------------
The live pipeline keeps ~3.5 years in master_history.csv, which is only a bull
market. Judging a defensive strategy on that alone is unfair — it needs to be
tested through the 2018 correction, the 2020 COVID crash and the 2022 drawdown.
This script builds that longer history.

WHY PARQUET, ONE FILE PER YEAR
------------------------------
A single 10-year gzipped CSV would be ~166 MB and exceed GitHub's 100 MB
per-file limit. Parquet is ~1.6x smaller and columnar (much faster to load),
and one file per year keeps each around 10 MB. Historical years never change,
so once written they are committed once and never rewritten — unlike
master_history.csv.gz, which is rewritten daily and bloats repo history.

SAFETY
------
This script NEVER touches master_history.csv or any live signal/ledger file.
It only creates/updates files under --out-dir (default data/market_database/history).

NSE FORMATS HANDLED
-------------------
1. "full" (roughly Jul-2020 onward):
     https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
     Columns: SYMBOL, SERIES, DATE1, OPEN_PRICE ... TTL_TRD_QNTY, TURNOVER_LACS
     TURNOVER_LACS is in LAKHS of rupees -> multiplied by 100,000.
2. "legacy" zip (pre-2020):
     https://nsearchives.nseindia.com/content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip
     Columns: SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, TOTTRDQTY, TOTTRDVAL, TIMESTAMP
     TOTTRDVAL is already in RUPEES.

Because that unit convention is easy to get wrong, every day's frame is passed
through a sanity check that compares TRADED_VALUE against CLOSE x VOLUME and
rescales if the units are clearly off. Getting this wrong would silently corrupt
the liquidity filter, so it is checked rather than assumed.

USAGE (normally run from the GitHub Actions workflow, one year at a time)
    python historical_backfill.py --year 2018 --out-dir data/market_database/history
    python historical_backfill.py --start 2016-01-01 --end 2016-12-31
"""
from __future__ import annotations

import argparse
import io
import random
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    import requests
except Exception:
    requests = None


FULL_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
LEGACY_URL = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/"
              "{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip")
NSE_HOME = "https://www.nseindia.com/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

OUT_COLS = ["DATE", "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/csv,application/zip,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_HOME,
    })
    try:
        s.get(NSE_HOME, timeout=15)
    except Exception:
        pass
    return s


# ------------------------------------------------------------------
# Parsers
# ------------------------------------------------------------------
def parse_full(text: str, d: date) -> pd.DataFrame | None:
    """Parse the modern sec_bhavdata_full CSV."""
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:
        return None
    df.columns = [str(c).strip().upper().replace(" ", "_") for c in df.columns]
    if "SYMBOL" not in df.columns:
        return None
    ren = {"OPEN_PRICE": "OPEN", "HIGH_PRICE": "HIGH", "LOW_PRICE": "LOW",
           "CLOSE_PRICE": "CLOSE", "TTL_TRD_QNTY": "VOLUME", "TURNOVER_LACS": "TRADED_VALUE"}
    for a, b in ren.items():
        if a in df.columns and b not in df.columns:
            df = df.rename(columns={a: b})
    had_lacs = "TURNOVER_LACS" in [str(c).strip().upper().replace(" ", "_") for c in text.splitlines()[0].split(",")]
    df["DATE"] = d.strftime("%Y%m%d")
    if had_lacs and "TRADED_VALUE" in df.columns:
        df["TRADED_VALUE"] = pd.to_numeric(df["TRADED_VALUE"], errors="coerce") * 100_000
    return df


def parse_legacy(content: bytes, d: date) -> pd.DataFrame | None:
    """Parse the legacy cmDDMONYYYYbhav.csv.zip archive."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
        df = pd.read_csv(io.BytesIO(zf.read(name)))
    except Exception:
        return None
    df.columns = [str(c).strip().upper() for c in df.columns]
    if "SYMBOL" not in df.columns:
        return None
    ren = {"TOTTRDQTY": "VOLUME", "TOTTRDVAL": "TRADED_VALUE"}
    for a, b in ren.items():
        if a in df.columns and b not in df.columns:
            df = df.rename(columns={a: b})
    df["DATE"] = d.strftime("%Y%m%d")   # TOTTRDVAL already in rupees
    return df


def normalise(df: pd.DataFrame, d: date) -> pd.DataFrame | None:
    """Clean columns, keep EQ series, and sanity-check the turnover units."""
    if df is None or df.empty:
        return None
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    if "SERIES" in df.columns:
        df["SERIES"] = df["SERIES"].astype(str).str.strip().str.upper()
        df = df[df["SERIES"] == "EQ"].copy()
    else:
        df["SERIES"] = "EQ"
    for c in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]:
        if c not in df.columns:
            df[c] = pd.NA
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["CLOSE"])
    if df.empty:
        return None

    # ---- turnover unit sanity check -------------------------------
    # TRADED_VALUE should be close to CLOSE * VOLUME. If the median ratio is
    # off by ~1e5 the file used lakhs; if off by ~1e7 it used crores. Rescale
    # rather than trusting the header, because a silent unit error would
    # corrupt every liquidity filter downstream.
    exp = (df["CLOSE"] * df["VOLUME"]).replace(0, pd.NA)
    ratio = (df["TRADED_VALUE"] / exp).median(skipna=True)
    if ratio and ratio == ratio and ratio > 0:
        for scale, label in ((1e5, "lakhs"), (1e7, "crores")):
            if 0.5 / scale < ratio < 2.0 / scale:
                df["TRADED_VALUE"] = df["TRADED_VALUE"] * scale
                print(f"      [units] {d} turnover looked like {label}; rescaled x{scale:,.0f}")
                break

    df["DATE"] = d.strftime("%Y%m%d")
    return df[OUT_COLS].reset_index(drop=True)


# ------------------------------------------------------------------
# Fetch one day (tries modern format, falls back to legacy zip)
# ------------------------------------------------------------------
def fetch_day(d: date, sess) -> pd.DataFrame | None:
    mon = d.strftime("%b").upper()
    attempts = [
        ("full", FULL_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))),
        ("legacy", LEGACY_URL.format(yyyy=d.strftime("%Y"), mon=mon, dd=d.strftime("%d"))),
    ]
    for kind, url in attempts:
        for attempt in range(2):
            try:
                r = sess.get(url, timeout=30)
                if r.status_code == 200 and len(r.content) > 500:
                    df = parse_full(r.text, d) if kind == "full" else parse_legacy(r.content, d)
                    out = normalise(df, d)
                    if out is not None and len(out) > 100:
                        return out
                    break
                if r.status_code in (403, 404):
                    break            # holiday / not in this format — try the other
                time.sleep(1.5 * (attempt + 1))
            except Exception:
                time.sleep(1.5 * (attempt + 1))
    return None


# ------------------------------------------------------------------
# Year runner
# ------------------------------------------------------------------
def backfill(start: date, end: date, out_dir: Path, sleep_s: float = 0.6) -> None:
    if requests is None:
        raise RuntimeError("requests is not installed")
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = make_session()

    by_year: dict[int, list] = {}
    have: dict[int, set] = {}
    for y in range(start.year, end.year + 1):
        f = out_dir / f"nse_{y}.parquet"
        if f.exists():
            try:
                ex = pd.read_parquet(f, columns=["DATE"])
                have[y] = set(ex["DATE"].astype(str))
                print(f"[INFO] {f.name}: {len(have[y])} dates already stored")
            except Exception:
                have[y] = set()
        else:
            have[y] = set()

    d, fetched, skipped, missing = start, 0, 0, 0
    while d <= end:
        if d.weekday() >= 5:
            d += timedelta(days=1); continue
        key = d.strftime("%Y%m%d")
        if key in have.get(d.year, set()):
            skipped += 1; d += timedelta(days=1); continue
        df = fetch_day(d, sess)
        if df is None:
            missing += 1
        else:
            by_year.setdefault(d.year, []).append(df)
            fetched += 1
            if fetched % 25 == 0:
                print(f"   ... {fetched} sessions fetched (latest {d})")
        time.sleep(sleep_s)
        d += timedelta(days=1)

    for y, frames in by_year.items():
        f = out_dir / f"nse_{y}.parquet"
        new = pd.concat(frames, ignore_index=True)
        if f.exists():
            try:
                old = pd.read_parquet(f)
                new = pd.concat([old, new], ignore_index=True)
            except Exception:
                pass
        new = new.drop_duplicates(subset=["DATE", "SYMBOL"], keep="last")
        new = new.sort_values(["DATE", "SYMBOL"]).reset_index(drop=True)
        for c in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]:
            new[c] = pd.to_numeric(new[c], errors="coerce").astype("float32")
        new["SYMBOL"] = new["SYMBOL"].astype("category")
        new["SERIES"] = new["SERIES"].astype("category")
        new.to_parquet(f, compression="snappy", index=False)
        mb = f.stat().st_size / 1048576
        print(f"[DONE] {f.name}: {len(new):,} rows, {new['DATE'].nunique()} sessions, {mb:.1f} MB")

    print(f"\n[SUMMARY] fetched {fetched} | already had {skipped} | unavailable {missing} "
          f"(holidays/pre-listing are expected)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, help="Backfill a single calendar year (simplest).")
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--out-dir", default="./data/market_database/history")
    ap.add_argument("--sleep", type=float, default=0.6, help="Delay between requests (be polite to NSE).")
    args = ap.parse_args()

    if args.year:
        start, end = date(args.year, 1, 1), date(args.year, 12, 31)
    elif args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        raise SystemExit("Provide --year YYYY, or --start and --end.")
    end = min(end, date.today() - timedelta(days=1))
    print(f"[INFO] backfilling {start} -> {end} into {args.out_dir}")
    backfill(start, end, Path(args.out_dir), sleep_s=args.sleep)


if __name__ == "__main__":
    main()
