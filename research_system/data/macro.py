"""Global and Indian macro series.

Stored long in data/research/macro.parquet: DATE (YYYYMMDD), SERIES, VALUE, SOURCE.

Series and sources (first that works wins, the source is recorded per row):
    USDINR    FRED DEXINUS        | Yahoo INR=X
    BRENT     FRED DCOILBRENTEU   | Yahoo BZ=F
    GOLD_USD  Yahoo GC=F
    US10Y     FRED DGS10          | Yahoo ^TNX        (percent)
    SPX       FRED SP500          | Yahoo ^GSPC
    DXY       Yahoo DX-Y.NYB
Derived:
    GOLD_INR  GOLD_USD x USDINR   (falls back to the GOLDBEES ETF close)

Indian bonds, India VIX and sector indices come from NSE's index file
(research_system.data.indices), FII/DII from research_system.data.flows.

FRED is the official source; Yahoo's chart endpoint is unofficial and is used
as a fallback for personal research only.
"""
from __future__ import annotations

import argparse
import io
import time

import numpy as np
import pandas as pd
import requests

from research_system import paths

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

SERIES = {
    "USDINR": [("fred", "DEXINUS"), ("yahoo", "INR=X")],
    "BRENT": [("fred", "DCOILBRENTEU"), ("yahoo", "BZ=F")],
    "GOLD_USD": [("yahoo", "GC=F")],
    "US10Y": [("fred", "DGS10"), ("yahoo", "^TNX")],
    "SPX": [("fred", "SP500"), ("yahoo", "^GSPC")],
    "DXY": [("yahoo", "DX-Y.NYB")],
}


def _fred(series_id: str, timeout: float = 30) -> pd.Series | None:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        if r.status_code != 200:
            return None
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["DATE", "VALUE"]
        df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce")
        df = df.dropna()
        s = pd.Series(df["VALUE"].values, index=pd.to_datetime(df["DATE"]))
        return s if len(s) else None
    except Exception:
        return None


def _yahoo(ticker: str, rng: str = "10y", timeout: float = 30) -> pd.Series | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={rng}&interval=1d"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        if r.status_code != 200:
            return None
        res = r.json()["chart"]["result"][0]
        ts = res.get("timestamp") or []
        close = res["indicators"]["quote"][0].get("close") or []
        if not ts:
            return None
        # Exchange-local calendar date of each bar
        off = res.get("meta", {}).get("gmtoffset", 0) or 0
        idx = pd.to_datetime(np.array(ts) + off, unit="s").normalize()
        s = pd.Series(close, index=idx, dtype="float64").dropna()
        return s[~s.index.duplicated(keep="last")]
    except Exception:
        return None


def fetch_series(name: str, rng: str = "10y") -> tuple[pd.Series | None, str | None]:
    for src, sid in SERIES[name]:
        s = _fred(sid) if src == "fred" else _yahoo(sid, rng)
        if s is not None and len(s) > 5:
            return s, f"{src}:{sid}"
        time.sleep(0.5)
    return None, None


def _goldbees_inr() -> pd.Series | None:
    """GOLDBEES ETF close (back-adjusted) as an INR gold proxy."""
    try:
        from research_system.data.prices import load_symbols
        df = load_symbols(["GOLDBEES"])
        if df.empty:
            return None
        return df.set_index("DATE_DT")["CLOSE"].sort_index()
    except Exception:
        return None


def update(rng: str = "10y") -> pd.DataFrame:
    paths.ensure_dirs()
    rows, got = [], {}
    for name in SERIES:
        s, src = fetch_series(name, rng)
        if s is None:
            print(f"[macro] {name}: unavailable from all sources")
            continue
        got[name] = s
        rows.append(pd.DataFrame({"DATE": s.index.strftime("%Y%m%d"), "SERIES": name,
                                  "VALUE": s.values, "SOURCE": src}))
        print(f"[macro] {name}: {len(s)} obs via {src}, last {s.index[-1].date()} = {s.iloc[-1]:.4g}")
    if "GOLD_USD" in got and "USDINR" in got:
        g = (got["GOLD_USD"] * got["USDINR"].reindex(got["GOLD_USD"].index).ffill()).dropna()
        rows.append(pd.DataFrame({"DATE": g.index.strftime("%Y%m%d"), "SERIES": "GOLD_INR",
                                  "VALUE": g.values, "SOURCE": "derived:GOLD_USDxUSDINR"}))
    else:
        g = _goldbees_inr()
        if g is not None:
            rows.append(pd.DataFrame({"DATE": g.index.strftime("%Y%m%d"), "SERIES": "GOLD_INR",
                                      "VALUE": g.values, "SOURCE": "nse:GOLDBEES"}))
    if not rows:
        return load()
    new = pd.concat(rows, ignore_index=True)
    old = load()
    out = pd.concat([old, new], ignore_index=True).drop_duplicates(["DATE", "SERIES"], keep="last")
    out = out.sort_values(["SERIES", "DATE"]).reset_index(drop=True)
    out.to_parquet(paths.MACRO, index=False)
    return out


def load() -> pd.DataFrame:
    if not paths.MACRO.exists():
        return pd.DataFrame(columns=["DATE", "SERIES", "VALUE", "SOURCE"])
    return pd.read_parquet(paths.MACRO)


def wide() -> pd.DataFrame:
    df = load()
    if df.empty:
        return pd.DataFrame()
    w = df.pivot_table(index="DATE", columns="SERIES", values="VALUE", aggfunc="last")
    w.index = pd.to_datetime(w.index, format="%Y%m%d")
    return w.sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="10y", help="Yahoo range for the fallback source (e.g. 10y, 1mo).")
    a = ap.parse_args()
    update(a.range)


if __name__ == "__main__":
    main()
