"""NSE index closes: broad market, ~60 sectors, India VIX and G-Sec (bond) indices.

Source: nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv,
one file per session with OHLC plus index P/E, P/B and dividend yield.

Stored long in data/research/indices.parquet:
    DATE (YYYYMMDD), INDEX, OPEN, HIGH, LOW, CLOSE, PE, PB, DY

    python -m research_system.data.indices --backfill --start 2017-01-01
    python -m research_system.data.indices --days 10
"""
from __future__ import annotations

import argparse
import io
from datetime import date, timedelta

import pandas as pd

from research_system import paths
from research_system.data.nse import NSE

URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"
COLS = {"index name": "INDEX", "open index value": "OPEN", "high index value": "HIGH",
        "low index value": "LOW", "closing index value": "CLOSE", "p/e": "PE", "p/b": "PB",
        "div yield": "DY", "index date": "INDEX_DATE"}

# Canonical names used by the rest of the system
NIFTY500 = "Nifty 500"
NIFTY50 = "Nifty 50"
VIX = "India VIX"
GSEC10 = "Nifty 10 yr Benchmark G-Sec"
GSEC_COMPOSITE = "Nifty Composite G-sec Index"
SMALLCAP = "Nifty Smallcap 250"
LARGECAP = "Nifty 100"


def parse(text: str, d: date) -> pd.DataFrame | None:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={k: v for k, v in COLS.items() if k in df.columns})
    if "INDEX" not in df.columns or "CLOSE" not in df.columns:
        return None
    # Guard: on holidays the archive can serve the previous file.
    if "INDEX_DATE" in df.columns:
        stamped = pd.to_datetime(df["INDEX_DATE"].astype(str), dayfirst=True, errors="coerce").dropna()
        if not stamped.empty and stamped.mode().iloc[0].date() != d:
            return None
    df["DATE"] = d.strftime("%Y%m%d")
    df["INDEX"] = df["INDEX"].astype(str).str.strip()
    for c in ["OPEN", "HIGH", "LOW", "CLOSE", "PE", "PB", "DY"]:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else float("nan")
    return df[["DATE", "INDEX", "OPEN", "HIGH", "LOW", "CLOSE", "PE", "PB", "DY"]]


def load() -> pd.DataFrame:
    if not paths.INDICES.exists():
        return pd.DataFrame(columns=["DATE", "INDEX", "OPEN", "HIGH", "LOW", "CLOSE", "PE", "PB", "DY"])
    return pd.read_parquet(paths.INDICES)


def wide(field: str = "CLOSE") -> pd.DataFrame:
    """dates x indices matrix (DatetimeIndex)."""
    df = load()
    if df.empty:
        return pd.DataFrame()
    w = df.pivot_table(index="DATE", columns="INDEX", values=field, aggfunc="last")
    w.index = pd.to_datetime(w.index, format="%Y%m%d")
    return w.sort_index()


def _save(frames: list[pd.DataFrame]) -> pd.DataFrame:
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["DATE", "INDEX"], keep="last")
    out = out.sort_values(["DATE", "INDEX"]).reset_index(drop=True)
    out.to_parquet(paths.INDICES, index=False)
    return out


def update(start: date, end: date, nse: NSE | None = None, verbose: bool = True,
           checkpoint: int = 100) -> int:
    """Fetch missing sessions in [start, end]. Saves every `checkpoint` sessions."""
    paths.ensure_dirs()
    base = load()
    have = set(base["DATE"].unique())
    nse = nse or NSE()
    new, total, d = [], 0, start
    while d <= end:
        key = d.strftime("%Y%m%d")
        if d.weekday() < 5 and key not in have:
            txt = nse.text(URL.format(ddmmyyyy=d.strftime("%d%m%Y")))
            if txt and len(txt) > 500:
                p = parse(txt, d)
                if p is not None:
                    new.append(p)
                    total += 1
                    if len(new) >= checkpoint:
                        base = _save([base] + new)
                        new = []
                        if verbose:
                            print(f"[indices] {total} sessions saved (through {d})", flush=True)
        d += timedelta(days=1)
    if new:
        _save([base] + new)
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--days", type=int, default=10)
    a = ap.parse_args()
    end = date.today()
    start = pd.Timestamp(a.start).date() if a.backfill else end - timedelta(days=a.days)
    n = update(start, end)
    print(f"[indices] added {n} sessions -> {paths.INDICES}")


if __name__ == "__main__":
    main()
