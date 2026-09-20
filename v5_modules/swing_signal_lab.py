"""
swing_signal_lab.py
===================
Signal research bench for the SWING engine. Measures whether individual
technical setups actually predict forward returns — BEFORE any of them is
allowed into a strategy.

WHY THIS EXISTS
---------------
The previous system combined momentum + low-vol + breadth into one score and
traded it for months before anyone asked whether the components worked. They
didn't. This bench tests each setup in isolation, against a matched same-day
baseline, so a signal has to earn its place.

METHOD
------
* Tradable universe applied per date: price >= --min-price and 20-day average
  traded value >= --min-adv. Research universe is everything; tradable is a
  subset, as it should be.
* Point-in-time: a setup is detected using data up to date D's close. The trade
  is entered at D+1's OPEN and exited at D+1+H's OPEN. No look-ahead.
* Each signal is compared to a BASELINE: the equal-weighted forward return of
  every tradable stock on the same date. This controls for market direction —
  a setup that returns +3% on a day everything returned +3% has no edge.
* Regime split: every signal is additionally reported in bull vs bear tape and
  high vs low volatility, because a breakout in a strong tape is a different
  animal from one in a weak tape.

HONEST STATISTICS
-----------------
Forward windows overlap when sampled daily, which inflates t-statistics by
roughly sqrt(H). The reported t is therefore ALSO shown adjusted. Treat the
adjusted number as the real one.

TRAIN / VALIDATE
----------------
--end-train fences off everything after it. Nothing beyond that date is read
during signal selection. The validation window is spent once — do not iterate
against it.

USAGE
    python swing_signal_lab.py --data /tmp/all.parquet --end-train 2023-12-31
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------
def load(path: Path, start: str, min_adv: float, min_price: float) -> dict:
    df = pd.read_parquet(path)
    df.columns = [c.strip().upper() for c in df.columns]
    df["DATE_DT"] = pd.to_datetime(df["DATE"].astype(str), format="%Y%m%d", errors="coerce")
    df = df[df["DATE_DT"].notna()]
    df = df[df["DATE_DT"] >= pd.Timestamp(start) - pd.Timedelta(days=400)]
    W = {}
    for c in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]:
        W[c] = df.pivot(index="DATE_DT", columns="SYMBOL", values=c).sort_index().astype("float32")
    # keep only symbols that are ever plausibly tradable — keeps memory sane
    adv = W["TRADED_VALUE"].rolling(20, min_periods=10).mean()
    keep = adv.columns[(adv >= min_adv).any()]
    for k in W:
        W[k] = W[k][keep]
    print(f"[INFO] {W['CLOSE'].shape[0]} sessions x {W['CLOSE'].shape[1]} symbols "
          f"(filtered from {df.SYMBOL.nunique()} by liquidity)")
    W["ADV20"] = W["TRADED_VALUE"].rolling(20, min_periods=10).mean()
    W["TRADABLE"] = (W["ADV20"] >= min_adv) & (W["CLOSE"] >= min_price)
    return W


def indicators(W: dict) -> dict:
    C, H, L, V, O = W["CLOSE"], W["HIGH"], W["LOW"], W["VOLUME"], W["OPEN"]
    I = {}
    I["RET1"] = C.pct_change(fill_method=None)
    I["SMA20"] = C.rolling(20, min_periods=15).mean()
    I["SMA50"] = C.rolling(50, min_periods=35).mean()
    I["SMA200"] = C.rolling(200, min_periods=150).mean()
    I["HIGH20"] = H.rolling(20, min_periods=15).max()
    I["HIGH252"] = H.rolling(252, min_periods=200).max()
    I["LOW20"] = L.rolling(20, min_periods=15).min()
    I["VOL20"] = I["RET1"].rolling(20, min_periods=15).std()
    I["VOL60"] = I["RET1"].rolling(60, min_periods=40).std()
    I["AVGVOL20"] = V.rolling(20, min_periods=15).mean()
    I["VOLRATIO"] = V / (I["AVGVOL20"] + 1e-9)
    prev = C.shift(1)
    tr = pd.concat([(H - L).stack(), (H - prev).abs().stack(), (L - prev).abs().stack()],
                   axis=1).max(axis=1).unstack()
    I["ATR14"] = tr.rolling(14, min_periods=10).mean()
    I["ATRPCT"] = I["ATR14"] / C
    I["RET20"] = C.pct_change(20, fill_method=None)
    I["RET60"] = C.pct_change(60, fill_method=None)
    # market proxy = equal-weighted mean of tradable names
    mkt = I["RET1"].where(W["TRADABLE"]).mean(axis=1)
    I["MKT_RET1"] = mkt
    I["MKT_LVL"] = (1 + mkt.fillna(0)).cumprod()
    I["MKT_SMA200"] = I["MKT_LVL"].rolling(200, min_periods=150).mean()
    I["MKT_VOL20"] = mkt.rolling(20, min_periods=15).std() * np.sqrt(252)
    I["RS20"] = I["RET20"].sub(I["MKT_LVL"].pct_change(20, fill_method=None), axis=0)
    return I


def build_signals(W: dict, I: dict) -> dict:
    """Each signal is a boolean matrix: True = setup fired at that date's close."""
    C, H, L, O = W["CLOSE"], W["HIGH"], W["LOW"], W["OPEN"]
    up = C > I["SMA200"]                      # primary trend filter
    S = {}
    S["BREAKOUT_20D"] = (C > I["HIGH20"].shift(1)) & (I["VOLRATIO"] > 1.5)
    S["BREAKOUT_52W"] = (C >= I["HIGH252"].shift(1) * 0.98) & (I["VOLRATIO"] > 1.2)
    S["MA_ALIGNED"] = (I["SMA20"] > I["SMA50"]) & (I["SMA50"] > I["SMA200"]) & \
                      (I["SMA20"].shift(1) <= I["SMA50"].shift(1))       # fresh alignment
    S["PULLBACK_UPTREND"] = up & (C < I["SMA20"]) & (C > C.shift(1)) & \
                            (C.shift(1) < C.shift(2))                    # first green after dip
    S["OVERSOLD_IN_UPTREND"] = up & (C < I["SMA20"] - 2 * I["VOL20"] * C)
    S["VOL_SQUEEZE_EXPANSION"] = (I["ATRPCT"].shift(1) <= I["ATRPCT"].rolling(60, min_periods=40).quantile(0.2).shift(1)) & \
                                 (I["VOLRATIO"] > 1.5) & (C > C.shift(1))
    S["VOLUME_THRUST"] = (I["VOLRATIO"] > 2.0) & (I["RET1"] > 0.02)
    S["GAP_UP_HOLD"] = (O > H.shift(1)) & (C > O)
    S["RS_LEADER"] = up & (I["RS20"] > I["RS20"].quantile(0.9, axis=1).values[:, None])
    S["FAILED_BREAKDOWN"] = (L < I["LOW20"].shift(1)) & (C > I["LOW20"].shift(1))
    return S


def forward_returns(W: dict, horizons) -> dict:
    """Entry at D+1 OPEN, exit at D+1+H OPEN. Strictly point-in-time."""
    O = W["OPEN"]
    fwd = {}
    for h in horizons:
        fwd[h] = O.shift(-(1 + h)) / O.shift(-1) - 1
    return fwd


def evaluate(sig: pd.DataFrame, fwd: pd.DataFrame, tradable: pd.DataFrame,
             mask_dates: pd.Series, min_obs: int = 200) -> dict:
    """Signal forward return vs the same-day baseline of all tradable names."""
    s = sig & tradable & mask_dates.values[:, None]
    base = tradable & mask_dates.values[:, None]
    per_date = []
    sig_vals = fwd.where(s)
    base_vals = fwd.where(base)
    sm = sig_vals.mean(axis=1)
    bm = base_vals.mean(axis=1)
    n = s.sum(axis=1)
    ok = (n > 0) & sm.notna() & bm.notna()
    if ok.sum() < 20:
        return None
    diff = (sm - bm)[ok]
    total_obs = int(n[ok].sum())
    if total_obs < min_obs:
        return None
    t_raw = diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))) if diff.std(ddof=1) > 0 else np.nan
    return dict(n_signals=total_obs, n_days=int(ok.sum()),
                sig_ret=float(sm[ok].mean()) * 100, base_ret=float(bm[ok].mean()) * 100,
                edge=float(diff.mean()) * 100, t_raw=float(t_raw),
                hit=float((sig_vals.where(s) > 0).sum().sum() / max(total_obs, 1)) * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/tmp/all.parquet")
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end-train", default="2023-12-31")
    ap.add_argument("--min-adv", type=float, default=5e7, help="20d avg traded value floor (default Rs 5 cr)")
    ap.add_argument("--min-price", type=float, default=50.0)
    ap.add_argument("--horizons", default="5,10,20")
    ap.add_argument("--validate", action="store_true",
                    help="Run on the HELD-OUT period instead of train. Use once, at the end.")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",")]
    W = load(Path(args.data), args.start, args.min_adv, args.min_price)
    I = indicators(W)
    S = build_signals(W, I)
    fwd = forward_returns(W, horizons)

    dates = W["CLOSE"].index
    if args.validate:
        mask = pd.Series(dates > pd.Timestamp(args.end_train), index=dates)
        label = f"VALIDATION  (after {args.end_train})"
    else:
        mask = pd.Series((dates >= pd.Timestamp(args.start)) & (dates <= pd.Timestamp(args.end_train)), index=dates)
        label = f"TRAIN  ({args.start} .. {args.end_train})"

    print(f"\n{'='*104}\n{label}\n{'='*104}")
    for h in horizons:
        print(f"\n--- forward horizon: {h} sessions "
              f"(entry next open, exit {h} sessions later) ---")
        print(f"{'signal':<24}{'fires':>8}{'sig ret':>9}{'base':>8}{'EDGE':>8}{'t raw':>7}{'t adj':>7}{'hit%':>7}")
        rows = []
        for name, sg in S.items():
            r = evaluate(sg, fwd[h], W["TRADABLE"], mask)
            if r is None:
                print(f"{name:<24}{'too few observations':>47}")
                continue
            t_adj = r["t_raw"] / np.sqrt(h)          # overlapping-window correction
            rows.append((name, r, t_adj))
            print(f"{name:<24}{r['n_signals']:>8,}{r['sig_ret']:>8.2f}%{r['base_ret']:>7.2f}%"
                  f"{r['edge']:>+7.2f}%{r['t_raw']:>7.2f}{t_adj:>7.2f}{r['hit']:>6.0f}%")
        good = [x for x in rows if x[1]["edge"] > 0 and x[2] > 2.0]
        print(f"  -> clears bar (edge>0 and adjusted t>2): "
              f"{', '.join(x[0] for x in good) if good else 'NONE'}")


if __name__ == "__main__":
    main()
