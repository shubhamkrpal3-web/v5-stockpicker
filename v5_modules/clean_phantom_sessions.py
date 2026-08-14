"""
clean_phantom_sessions.py
=========================
Removes "phantom sessions" from already-stored price history.

WHAT A PHANTOM SESSION IS
-------------------------
On NSE holidays the archive sometimes serves the PREVIOUS trading day's file with
a 200 response. Earlier versions of the downloaders stamped that file with the
REQUESTED date, inventing a session that is an exact copy of the prior day.

Why it matters: every symbol shows a 0.00% return on that day. That deflates
volatility estimates, drags moving averages, and inflates any day-count logic
(the 25-day time stop, momentum lookbacks, the regime's 20-day volatility
window). Given the regime trigger is volatility-based, this is not cosmetic.

DETECTION
---------
A session is treated as phantom when >=98% of its symbols have a CLOSE
identical to the previous stored session (checked over 200+ overlapping
symbols). With 1,400+ symbols, a genuine session matching the prior day to that
degree is effectively impossible.

SAFETY
------
* Dry-run by default — prints what it WOULD remove and changes nothing.
* Pass --apply to actually rewrite files.
* Removed rows are fabricated data, not real observations; the underlying source
  can always be re-fetched with the (now fixed) downloaders.

USAGE
    python clean_phantom_sessions.py --history-dir data/market_database/history
    python clean_phantom_sessions.py --history-dir data/market_database/history --apply
    python clean_phantom_sessions.py --master data/market_database/master_history.csv --apply
"""
from __future__ import annotations

import argparse
import glob
from pathlib import Path

import pandas as pd


def find_phantoms(df: pd.DataFrame, thresh: float = 0.98, min_overlap: int = 200) -> list:
    """Return the list of DATE values that duplicate the previous session."""
    w = df.pivot_table(index="DATE", columns="SYMBOL", values="CLOSE").sort_index()
    phantoms, prev, prev_date = [], None, None
    for dt, row in w.iterrows():
        if prev is not None:
            cur_ok, prev_ok = row.dropna(), prev.dropna()
            common = cur_ok.index.intersection(prev_ok.index)
            if len(common) >= min_overlap:
                same = (row[common] == prev[common]).mean()
                if same >= thresh:
                    phantoms.append((dt, prev_date, same, len(common)))
                    continue          # don't chain: compare next day to the real one
        prev, prev_date = row, dt
    return phantoms


def clean_parquet(path: Path, apply: bool) -> int:
    df = pd.read_parquet(path)
    ph = find_phantoms(df)
    if not ph:
        print(f"  {path.name}: clean (no phantom sessions)")
        return 0
    dates = [p[0] for p in ph]
    before = len(df)
    print(f"  {path.name}: {len(ph)} phantom sessions -> {', '.join(str(d) for d in dates)}")
    if apply:
        df = df[~df["DATE"].astype(str).isin([str(d) for d in dates])]
        for c in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
        for c in ["SYMBOL", "SERIES"]:
            if c in df.columns:
                df[c] = df[c].astype("category")
        df = df.sort_values(["DATE", "SYMBOL"]).reset_index(drop=True)
        df.to_parquet(path, compression="snappy", index=False)
        print(f"      removed {before - len(df):,} rows -> {len(df):,} remain")
    return len(ph)


def clean_master(path: Path, apply: bool) -> int:
    df = pd.read_csv(path, low_memory=False)
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    sub = df[df["SERIES"].astype(str).str.upper() == "EQ"] if "SERIES" in df.columns else df
    ph = find_phantoms(sub[["DATE", "SYMBOL", "CLOSE"]])
    if not ph:
        print(f"  {path.name}: clean (no phantom sessions)")
        return 0
    dates = [str(p[0]) for p in ph]
    before = len(df)
    print(f"  {path.name}: {len(ph)} phantom sessions -> {', '.join(dates)}")
    if apply:
        df = df[~df["DATE"].astype(str).isin(dates)]
        df.to_csv(path, index=False)
        print(f"      removed {before - len(df):,} rows -> {len(df):,} remain")
    return len(ph)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-dir", default=None, help="Folder of yearly .parquet archives")
    ap.add_argument("--master", default=None, help="Path to master_history.csv (decompressed)")
    ap.add_argument("--apply", action="store_true", help="Actually rewrite files (default: dry run)")
    args = ap.parse_args()

    mode = "APPLYING CHANGES" if args.apply else "DRY RUN (nothing will be modified)"
    print(f"=== clean_phantom_sessions — {mode} ===\n")
    total = 0
    if args.history_dir:
        for f in sorted(glob.glob(str(Path(args.history_dir) / "*.parquet"))):
            total += clean_parquet(Path(f), args.apply)
    if args.master:
        total += clean_master(Path(args.master), args.apply)
    print(f"\nTotal phantom sessions found: {total}")
    if total and not args.apply:
        print("Re-run with --apply to remove them.")


if __name__ == "__main__":
    main()
