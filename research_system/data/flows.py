"""Institutional flows and positioning.

1. FII/FPI and DII cash-market net buying (NSE, Rs crore, provisional).
   NSE publishes only the latest session, so history accumulates from the day
   this runs: data/research/fii_dii_cash.csv  (DATE, CATEGORY, BUY, SELL, NET)

2. Participant-wise open interest in equity derivatives (NSE archives, one
   file per session, available for years): FII / DII / Pro / Client long and
   short contracts in index futures, stock futures and options.
   data/research/participant_oi.parquet

    python -m research_system.data.flows                 # daily update
    python -m research_system.data.flows --oi-backfill --start 2018-01-01
"""
from __future__ import annotations

import argparse
import io
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from research_system import paths
from research_system.data.nse import NSE

CASH_API = "https://www.nseindia.com/api/fiidiiTradeReact"
OI_URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"
OI_COLS = {
    "future index long": "FUT_IDX_LONG", "future index short": "FUT_IDX_SHORT",
    "future stock long": "FUT_STK_LONG", "future stock short": "FUT_STK_SHORT",
    "option index call long": "OPT_IDX_CALL_LONG", "option index put long": "OPT_IDX_PUT_LONG",
    "option index call short": "OPT_IDX_CALL_SHORT", "option index put short": "OPT_IDX_PUT_SHORT",
    "option stock call long": "OPT_STK_CALL_LONG", "option stock put long": "OPT_STK_PUT_LONG",
    "option stock call short": "OPT_STK_CALL_SHORT", "option stock put short": "OPT_STK_PUT_SHORT",
    "total long contracts": "TOTAL_LONG", "total short contracts": "TOTAL_SHORT",
}


# ---------------------------------------------------------------- cash flows
def update_cash(nse: NSE | None = None) -> int:
    paths.ensure_dirs()
    nse = nse or NSE()
    j = nse.json(CASH_API)
    if not j:
        print("[flows] FII/DII cash: unavailable")
        return 0
    rows = []
    for x in j:
        d = pd.to_datetime(str(x.get("date", "")), format="%d-%b-%Y", errors="coerce")
        if pd.isna(d):
            continue
        cat = "FII" if "FII" in str(x.get("category", "")).upper() else "DII"
        rows.append({"DATE": d.strftime("%Y%m%d"), "CATEGORY": cat,
                     "BUY": pd.to_numeric(x.get("buyValue"), errors="coerce"),
                     "SELL": pd.to_numeric(x.get("sellValue"), errors="coerce"),
                     "NET": pd.to_numeric(x.get("netValue"), errors="coerce"),
                     "FETCHED_AT": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if not rows:
        return 0
    new = pd.DataFrame(rows)
    old = load_cash()
    out = pd.concat([old, new], ignore_index=True).drop_duplicates(["DATE", "CATEGORY"], keep="last")
    out.sort_values(["DATE", "CATEGORY"]).to_csv(paths.FII_DII_CASH, index=False)
    print(f"[flows] FII/DII cash: {', '.join(f'{r.CATEGORY} {r.NET:+,.0f} cr' for r in new.itertuples())} ({new.DATE.iloc[0]})")
    return len(new)


def load_cash() -> pd.DataFrame:
    if not paths.FII_DII_CASH.exists():
        return pd.DataFrame(columns=["DATE", "CATEGORY", "BUY", "SELL", "NET", "FETCHED_AT"])
    return pd.read_csv(paths.FII_DII_CASH, dtype={"DATE": str})


def cash_wide() -> pd.DataFrame:
    """dates x {FII_NET, DII_NET} in Rs crore."""
    c = load_cash()
    if c.empty:
        return pd.DataFrame()
    w = c.pivot_table(index="DATE", columns="CATEGORY", values="NET", aggfunc="last")
    w.columns = [f"{x}_NET" for x in w.columns]
    w.index = pd.to_datetime(w.index, format="%Y%m%d")
    return w.sort_index()


# ---------------------------------------------------------- participant OI
def parse_oi(text: str, d: date) -> pd.DataFrame | None:
    lines = text.strip().splitlines()
    hdr = next((i for i, ln in enumerate(lines) if ln.lower().startswith("client type")), None)
    if hdr is None:
        return None
    df = pd.read_csv(io.StringIO("\n".join(lines[hdr:])))
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={"client type": "CLIENT_TYPE", **OI_COLS})
    df["CLIENT_TYPE"] = df["CLIENT_TYPE"].astype(str).str.strip().str.upper()
    for c in OI_COLS.values():
        if c in df.columns:
            df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", "").str.strip(), errors="coerce")
    df["DATE"] = d.strftime("%Y%m%d")
    return df[["DATE", "CLIENT_TYPE"] + [c for c in OI_COLS.values() if c in df.columns]]


def load_oi() -> pd.DataFrame:
    if not paths.PARTICIPANT_OI.exists():
        return pd.DataFrame(columns=["DATE", "CLIENT_TYPE"])
    return pd.read_parquet(paths.PARTICIPANT_OI)


def update_oi(start: date, end: date, nse: NSE | None = None, checkpoint: int = 100) -> int:
    paths.ensure_dirs()
    base = load_oi()
    have = set(base["DATE"].unique()) if not base.empty else set()
    nse = nse or NSE()
    new, total, d = [], 0, start
    while d <= end:
        key = d.strftime("%Y%m%d")
        if d.weekday() < 5 and key not in have:
            txt = nse.text(OI_URL.format(ddmmyyyy=d.strftime("%d%m%Y")))
            if txt:
                p = parse_oi(txt, d)
                if p is not None and len(p):
                    new.append(p)
                    total += 1
            if len(new) >= checkpoint:
                base = _save_oi([base] + new)
                new = []
                print(f"[flows] participant OI: {total} sessions saved (through {d})", flush=True)
        d += timedelta(days=1)
    if new:
        _save_oi([base] + new)
    return total


def _save_oi(frames):
    out = pd.concat([f for f in frames if len(f)], ignore_index=True)
    out = out.drop_duplicates(["DATE", "CLIENT_TYPE"], keep="last").sort_values(["DATE", "CLIENT_TYPE"])
    out.to_parquet(paths.PARTICIPANT_OI, index=False)
    return out


def fii_positioning() -> pd.DataFrame:
    """Daily FII derivatives positioning: net index futures (contracts) and the
    long share of index futures, plus net index put-minus-call longs."""
    oi = load_oi()
    if oi.empty:
        return pd.DataFrame()
    f = oi[oi["CLIENT_TYPE"] == "FII"].copy()
    f["DATE_DT"] = pd.to_datetime(f["DATE"], format="%Y%m%d")
    out = pd.DataFrame(index=f["DATE_DT"])
    out["FII_IDXFUT_NET"] = (f["FUT_IDX_LONG"] - f["FUT_IDX_SHORT"]).values
    out["FII_IDXFUT_LONG_SHARE"] = (f["FUT_IDX_LONG"] / (f["FUT_IDX_LONG"] + f["FUT_IDX_SHORT"])).values
    out["FII_STKFUT_NET"] = (f["FUT_STK_LONG"] - f["FUT_STK_SHORT"]).values
    return out.sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oi-backfill", action="store_true")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--days", type=int, default=10)
    a = ap.parse_args()
    nse = NSE()
    update_cash(nse)
    end = date.today()
    start = pd.Timestamp(a.start).date() if a.oi_backfill else end - timedelta(days=a.days)
    n = update_oi(start, end, nse)
    print(f"[flows] participant OI: added {n} sessions")


if __name__ == "__main__":
    main()
