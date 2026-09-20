"""
pit_fundamentals.py
===================
Point-in-time (PIT) fundamentals archive.

THE PROBLEM THIS SOLVES
-----------------------
`screener.sqlite` holds ONE snapshot — whatever the scraper last fetched. Using
it to rank stocks in 2019 means the ranking already knows how each company
turned out. That contamination is not theoretical: measured on this data, a
"value" score built from today's P/E correlated +0.54 with the subsequent
2023-2026 return. It was mostly just reading the future.

Consequence: every fundamental backtest so far has been untrustworthy in the
positive direction. (Negative results still count — a strategy that fails while
cheating really does fail.)

THE FIX
-------
Append a dated vintage of the cache every time this runs. Each row records the
date that company's data was actually fetched. A backtest at decision date D
then uses only rows with FETCHED_AT <= D — which is what the world genuinely
knew at D.

This does not repair the past. It starts the clock. After ~18 months of
quarterly snapshots there is enough history to test a fundamental strategy
honestly for the first time.

WHY PER-SYMBOL FETCHED_AT, NOT ONE SNAPSHOT DATE
------------------------------------------------
The scraper works through the universe over days or weeks (the current cache
spans Sep 2-17). Stamping every row with a single "snapshot date" would claim
knowledge slightly before or after it existed. Storing each row's own fetch
timestamp is strictly more accurate and costs nothing.

FILES
-----
    data/fundamentals/pit_fundamentals.parquet   append-only, one row per
                                                 (SNAPSHOT_DATE, SYMBOL)

USAGE
    # archive the current cache as a new vintage
    python pit_fundamentals.py --archive

    # what did we know on a given date?
    python pit_fundamentals.py --as-of 2027-03-31

    # inventory of vintages collected so far
    python pit_fundamentals.py --list
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

NUMERIC = [
    "roce_pct", "roe_pct", "debt_to_equity", "opm_pct", "stock_pe", "book_value_inr",
    "market_cap_inr", "cfo_last_year", "cfo_prior_year",
    "sales_growth_3y_pct", "sales_growth_10y_pct", "pat_growth_3y_pct",
    "promoter_pct_latest", "promoter_pct_prev_q", "promoter_drop_qoq_pp",
]
TEXT = ["screener_pros", "screener_cons"]


def read_cache(db: Path) -> pd.DataFrame:
    """Flatten the scraper's SQLite cache into a tidy frame."""
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT symbol, fetched_at, payload FROM sc").fetchall()
    finally:
        con.close()
    recs = []
    for sym, fetched_at, payload in rows:
        try:
            d = json.loads(payload)
        except Exception:
            continue
        r = {"SYMBOL": str(sym).strip().upper()}
        # per-row vintage: prefer the payload's own stamp, fall back to the column
        ts = d.get("fetched_at") or fetched_at
        r["FETCHED_AT"] = (datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                           if isinstance(ts, (int, float)) and ts else None)
        r["FETCHED_OK"] = bool(d.get("_fetched_ok", False))
        for k in NUMERIC:
            v = d.get(k)
            r[k.upper()] = pd.to_numeric(v, errors="coerce") if v is not None else None
        for k in TEXT:
            v = d.get(k)
            r[k.upper()] = (str(v)[:2000] if v else None)
        recs.append(r)
    return pd.DataFrame(recs)


def archive(db: Path, store: Path, force: bool = False) -> int:
    snap_date = datetime.now().date().isoformat()
    cur = read_cache(db)
    if cur.empty:
        print("[WARN] cache is empty — nothing archived")
        return 0
    cur.insert(0, "SNAPSHOT_DATE", snap_date)

    store.parent.mkdir(parents=True, exist_ok=True)
    if store.exists():
        old = pd.read_parquet(store)
        if (old["SNAPSHOT_DATE"] == snap_date).any() and not force:
            print(f"[SKIP] a vintage for {snap_date} already exists "
                  f"({(old['SNAPSHOT_DATE'] == snap_date).sum()} rows). Use --force to replace.")
            return 0
        old = old[old["SNAPSHOT_DATE"] != snap_date]
        out = pd.concat([old, cur], ignore_index=True)
    else:
        out = cur
    out = out.sort_values(["SNAPSHOT_DATE", "SYMBOL"]).reset_index(drop=True)
    out.to_parquet(store, compression="snappy", index=False)
    ok = int(cur["FETCHED_OK"].sum())
    print(f"[DONE] archived vintage {snap_date}: {len(cur):,} symbols ({ok:,} with good data)")
    print(f"       store now holds {out['SNAPSHOT_DATE'].nunique()} vintage(s), "
          f"{len(out):,} rows, {store.stat().st_size/1048576:.2f} MB")
    return len(cur)


def load_as_of(store: Path, as_of: str) -> pd.DataFrame:
    """Fundamentals as they were genuinely known on `as_of`.

    Uses each row's own FETCHED_AT, keeping the most recent observation per
    symbol that was already available. Rows fetched after `as_of` are excluded —
    that exclusion is the entire point.
    """
    if not store.exists():
        raise FileNotFoundError(f"no PIT store at {store} — run --archive first")
    df = pd.read_parquet(store)
    df = df[df["FETCHED_AT"].notna()]
    df = df[df["FETCHED_AT"] <= as_of]
    if df.empty:
        return df
    df = df.sort_values(["SYMBOL", "FETCHED_AT"]).groupby("SYMBOL", as_index=False).tail(1)
    return df.reset_index(drop=True)


def inventory(store: Path) -> None:
    if not store.exists():
        print("[INFO] no vintages archived yet."); return
    df = pd.read_parquet(store)
    g = df.groupby("SNAPSHOT_DATE").agg(symbols=("SYMBOL", "nunique"),
                                        good=("FETCHED_OK", "sum"),
                                        earliest_fetch=("FETCHED_AT", "min"),
                                        latest_fetch=("FETCHED_AT", "max"))
    print(g.to_string())
    n = df["SNAPSHOT_DATE"].nunique()
    print(f"\n{n} vintage(s). A fundamental strategy needs roughly 6-8 before it can be "
          f"tested honestly (~18-24 months of quarterly snapshots).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="./data/fundamentals/screener.sqlite")
    ap.add_argument("--store", default="./data/fundamentals/pit_fundamentals.parquet")
    ap.add_argument("--archive", action="store_true", help="Append the current cache as a new vintage.")
    ap.add_argument("--force", action="store_true", help="Replace an existing vintage for today.")
    ap.add_argument("--as-of", default=None, help="Print what was known on this date (YYYY-MM-DD).")
    ap.add_argument("--list", action="store_true", help="Show vintages collected so far.")
    args = ap.parse_args()

    store = Path(args.store)
    if args.archive:
        archive(Path(args.db), store, args.force)
    if args.as_of:
        d = load_as_of(store, args.as_of)
        print(f"\nKnown as of {args.as_of}: {len(d):,} symbols")
        if len(d):
            cols = ["SYMBOL", "FETCHED_AT", "ROCE_PCT", "DEBT_TO_EQUITY", "STOCK_PE", "MARKET_CAP_INR"]
            print(d[[c for c in cols if c in d.columns]].head(10).to_string(index=False))
    if args.list or not (args.archive or args.as_of):
        inventory(store)


if __name__ == "__main__":
    main()
