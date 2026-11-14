"""NSE equity-derivatives contract universe and end-of-day contract data.

* Contract universe + lot sizes: nsearchives .../content/fo/fo_mktlots.csv
  An equity being listed does NOT mean it has stock futures/options. This file
  is the authority; it is refreshed every run and stored in fno_lots.csv.
* Daily contract data (UDiFF F&O bhavcopy): every future and option with
  settlement price, underlying price, open interest, volume and lot size.
  The latest few sessions are kept in data/research/fno/ (large files); a
  compact per-underlying summary with ATM implied volatility is appended to
  data/research/fno/summary.parquet so IV history accumulates cheaply.

    python -m research_system.data.fno            # refresh lots + latest bhavcopy
    python -m research_system.data.fno --days 5
"""
from __future__ import annotations

import argparse
import glob
import io
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

from research_system import paths
from research_system.data.nse import NSE
from research_system.pricing import implied_vol

LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
KEEP_DAYS = 5
DEFAULT_RATE = 0.065   # annual risk-free proxy for Black-76; configurable in risk_config


# ----------------------------------------------------------------- lots
def update_lots(nse: NSE | None = None) -> pd.DataFrame:
    paths.ensure_dirs()
    nse = nse or NSE()
    txt = nse.text(LOTS_URL)
    if not txt:
        print("[fno] lot-size file unavailable; keeping the stored universe")
        return load_lots()
    df = pd.read_csv(io.StringIO(txt))
    df.columns = [str(c).strip().upper() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    df = df[df["SYMBOL"].notna() & (df["SYMBOL"] != "") & (df["SYMBOL"].str.upper() != "SYMBOL")]
    month_cols = [c for c in df.columns if c not in ("UNDERLYING", "SYMBOL")]

    def first_lot(row):
        for c in month_cols:
            v = pd.to_numeric(row[c], errors="coerce")
            if pd.notna(v) and v > 0:
                return int(v)
        return None

    out = pd.DataFrame({"SYMBOL": df["SYMBOL"].str.upper(), "UNDERLYING": df["UNDERLYING"],
                        "LOT_SIZE": df.apply(first_lot, axis=1)})
    out = out[out["LOT_SIZE"].notna()]
    out["IS_INDEX"] = out["SYMBOL"].isin(INDEX_UNDERLYINGS)
    out["AS_OF"] = date.today().strftime("%Y%m%d")
    out.to_csv(paths.FNO_LOTS, index=False)
    print(f"[fno] contract universe: {len(out)} underlyings ({int((~out.IS_INDEX).sum())} stocks)")
    return out


def load_lots() -> pd.DataFrame:
    if not paths.FNO_LOTS.exists():
        return pd.DataFrame(columns=["SYMBOL", "UNDERLYING", "LOT_SIZE", "IS_INDEX", "AS_OF"])
    return pd.read_csv(paths.FNO_LOTS, dtype={"AS_OF": str})


def eligible(symbol: str) -> tuple[bool, int | None]:
    lots = load_lots()
    row = lots[lots["SYMBOL"] == symbol.upper()]
    if row.empty:
        return False, None
    return True, int(row["LOT_SIZE"].iloc[0])


# ------------------------------------------------------------ bhavcopy
def parse_bhav(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw))
    keep = {"TradDt": "DATE", "FinInstrmTp": "TYPE", "TckrSymb": "SYMBOL", "XpryDt": "EXPIRY",
            "StrkPric": "STRIKE", "OptnTp": "OPT", "ClsPric": "CLOSE", "SttlmPric": "SETTLE",
            "UndrlygPric": "UNDERLYING_PX", "OpnIntrst": "OI", "ChngInOpnIntrst": "OI_CHG",
            "TtlTradgVol": "VOLUME", "TtlTrfVal": "TURNOVER", "NewBrdLotQty": "LOT"}
    df = df[[c for c in keep if c in df.columns]].rename(columns=keep)
    df["DATE"] = pd.to_datetime(df["DATE"]).dt.strftime("%Y%m%d")
    df["EXPIRY"] = pd.to_datetime(df["EXPIRY"]).dt.strftime("%Y%m%d")
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    df["OPT"] = df["OPT"].fillna("").astype(str).str.strip()
    return df


def bhav_path(d: str):
    return paths.FNO_DIR / f"fo_{d}.parquet"


def load_bhav(d: str | None = None) -> pd.DataFrame:
    """Contract data for session `d` (YYYYMMDD) or the latest stored."""
    if d:
        p = bhav_path(d)
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()
    files = sorted(glob.glob(str(paths.FNO_DIR / "fo_*.parquet")))
    return pd.read_parquet(files[-1]) if files else pd.DataFrame()


def summarize(df: pd.DataFrame, rate: float = DEFAULT_RATE) -> pd.DataFrame:
    """Per underlying: spot, near future, annualised basis, ATM IV, OI and PCR."""
    rows = []
    d = pd.Timestamp(df["DATE"].iloc[0])
    for sym, g in df.groupby("SYMBOL"):
        fut = g[g["TYPE"].isin(["STF", "IDF"])].sort_values("EXPIRY")
        opt = g[g["TYPE"].isin(["STO", "IDO"])]
        if fut.empty:
            continue
        near = fut.iloc[0]
        spot = float(near["UNDERLYING_PX"])
        T_f = max((pd.Timestamp(near["EXPIRY"]) - d).days, 1) / 365.0
        basis = (float(near["SETTLE"]) / spot - 1) / T_f if spot > 0 else np.nan
        atm_iv = np.nan
        if not opt.empty:
            exp = opt[opt["EXPIRY"] == near["EXPIRY"]]
            if not exp.empty:
                k = exp.iloc[(exp["STRIKE"] - float(near["SETTLE"])).abs().argsort()]["STRIKE"].iloc[0]
                ivs = []
                for typ in ("CE", "PE"):
                    leg = exp[(exp["STRIKE"] == k) & (exp["OPT"] == typ)]
                    if not leg.empty:
                        iv = implied_vol(float(leg["SETTLE"].iloc[0]), float(near["SETTLE"]), float(k),
                                         T_f, rate, typ == "CE")
                        if iv:
                            ivs.append(iv)
                atm_iv = float(np.mean(ivs)) if ivs else np.nan
        calls, puts = opt[opt["OPT"] == "CE"], opt[opt["OPT"] == "PE"]
        rows.append({"DATE": df["DATE"].iloc[0], "SYMBOL": sym, "SPOT": spot,
                     "FUT_NEAR": float(near["SETTLE"]), "FUT_EXPIRY": near["EXPIRY"],
                     "BASIS_ANN": basis, "ATM_IV": atm_iv,
                     "OI_FUT": float(fut["OI"].sum()), "OI_CALL": float(calls["OI"].sum()),
                     "OI_PUT": float(puts["OI"].sum()),
                     "PCR_OI": float(puts["OI"].sum() / calls["OI"].sum()) if calls["OI"].sum() > 0 else np.nan,
                     "VOL_FUT": float(fut["VOLUME"].sum()), "LOT": float(near.get("LOT", np.nan))})
    return pd.DataFrame(rows)


def load_summary() -> pd.DataFrame:
    p = paths.FNO_DIR / "summary.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def update_bhav(days: int = 5, nse: NSE | None = None, rate: float = DEFAULT_RATE) -> int:
    paths.ensure_dirs()
    nse = nse or NSE()
    summ = load_summary()
    have = set(summ["DATE"].unique()) if not summ.empty else set()
    added, end = 0, date.today()
    for k in range(days):
        d = end - timedelta(days=k)
        key = d.strftime("%Y%m%d")
        if d.weekday() >= 5 or (key in have and bhav_path(key).exists()):
            continue
        raw = nse.zip_member(BHAV_URL.format(yyyymmdd=key))
        if raw is None:
            continue
        df = parse_bhav(raw)
        if df.empty or df["DATE"].iloc[0] != key:
            continue
        df.to_parquet(bhav_path(key), index=False)
        s = summarize(df, rate)
        summ = pd.concat([summ, s], ignore_index=True) if not summ.empty else s
        added += 1
        print(f"[fno] {key}: {len(df):,} contracts, {len(s)} underlyings summarised")
    if added:
        summ = summ.drop_duplicates(["DATE", "SYMBOL"], keep="last").sort_values(["DATE", "SYMBOL"])
        summ.to_parquet(paths.FNO_DIR / "summary.parquet", index=False)
    # keep only the most recent full files; the summary keeps the history
    for f in sorted(glob.glob(str(paths.FNO_DIR / "fo_*.parquet")))[:-KEEP_DAYS]:
        try:
            os.remove(f)
        except OSError:
            pass
    return added


def iv_percentile(symbol: str, lookback: int = 252) -> float | None:
    s = load_summary()
    if s.empty:
        return None
    x = s[s["SYMBOL"] == symbol.upper()].sort_values("DATE")["ATM_IV"].dropna().tail(lookback)
    if len(x) < 20:
        return None
    return float((x < x.iloc[-1]).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    a = ap.parse_args()
    nse = NSE()
    update_lots(nse)
    n = update_bhav(a.days, nse)
    print(f"[fno] stored {n} new session(s)")


if __name__ == "__main__":
    main()
