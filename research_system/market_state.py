"""Market State service: timestamped broad, sector and security regimes plus
measured macro relationships.

Nothing here is a hard-coded claim such as "gold up means equities down".
Each relationship is MEASURED: full-sample correlation with a 95% interval,
the latest rolling correlation and beta, how often the rolling sign agreed
with the full-sample sign, the correlation inside each regime, and lead-lag
correlations. A relationship is labelled:
    stable     interval excludes zero and the rolling sign agreed >= 75% of the time
    unstable   the rolling sign flipped often (< 60% agreement)
    weak       interval includes zero
    insufficient  too little overlapping data

Timing: NSE closes at 15:30 IST, before US and London. A same-day correlation
with a global series (gold, crude, USD/INR, US yields, S&P 500) partly uses
information from after the Indian close, so it is descriptive only. The
"lead" correlations (global series on day t-1 vs India on day t) use only
information available before the Indian open.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research_system import paths
from research_system.data import flows as flows_mod
from research_system.data import indices as idx_mod
from research_system.data import macro as macro_mod
from research_system.data import prices
from research_system.portfolio import _sector_map

ROLL = 126
MIN_OBS = 120


# ------------------------------------------------------------- statistics
def corr_ci(r: float, n: int) -> tuple[float, float]:
    if n < 4 or not np.isfinite(r) or abs(r) >= 1:
        return (np.nan, np.nan)
    z, se = math.atanh(r), 1 / math.sqrt(n - 3)
    return (math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se))


def relationship(y: pd.Series, x: pd.Series, regimes: pd.Series | None = None,
                 vol_state: pd.Series | None = None, lead_only: bool = False) -> dict:
    """Measured relationship between target y (India) and factor x."""
    df = pd.concat({"y": y, "x": x}, axis=1, sort=True).dropna()
    n = len(df)
    if n < MIN_OBS:
        return {"label": "insufficient", "n": n}
    r = float(df["y"].corr(df["x"]))
    lo, hi = corr_ci(r, n)
    roll = df["y"].rolling(ROLL).corr(df["x"]).dropna()
    monthly = roll.resample("ME").last().dropna()
    agree = float((np.sign(monthly) == np.sign(r)).mean()) if len(monthly) else np.nan
    beta_roll = (df["y"].rolling(ROLL).cov(df["x"]) / df["x"].rolling(ROLL).var()).dropna()
    out = {
        "n": n, "since": df.index[0].strftime("%Y-%m-%d"),
        "corr": round(r, 3), "ci95": [round(lo, 3), round(hi, 3)],
        "corr_126d": round(float(roll.iloc[-1]), 3) if len(roll) else None,
        "beta_126d": round(float(beta_roll.iloc[-1]), 4) if len(beta_roll) else None,
        "corr_126d_range_3y": [round(float(roll.tail(756).min()), 3), round(float(roll.tail(756).max()), 3)]
        if len(roll) else None,
        "sign_agreement": round(agree, 2) if np.isfinite(agree) else None,
    }
    if regimes is not None:
        rg = regimes.reindex(df.index)
        out["by_regime"] = {k: {"corr": round(float(g["y"].corr(g["x"])), 3), "n": len(g)}
                            for k, g in df.groupby(rg) if len(g) >= 60}
    if vol_state is not None:
        vs = vol_state.reindex(df.index)
        out["by_volatility"] = {k: {"corr": round(float(g["y"].corr(g["x"])), 3), "n": len(g)}
                                for k, g in df.groupby(vs) if len(g) >= 60}
    # lead-lag: does x on day t-k line up with y on day t?
    lags = {}
    for k in range(1, 6):
        d2 = pd.concat({"y": y, "x": x.shift(k)}, axis=1, sort=True).dropna()
        if len(d2) >= MIN_OBS:
            rk = float(d2["y"].corr(d2["x"]))
            lags[k] = {"corr": round(rk, 3), "significant": abs(rk) > 2 / math.sqrt(len(d2))}
    out["lead_lag"] = lags
    crosses_zero = not (np.isfinite(lo) and (lo > 0 or hi < 0))
    if crosses_zero:
        out["label"] = "weak"
    elif out["sign_agreement"] is not None and out["sign_agreement"] < 0.60:
        out["label"] = "unstable"
    elif out["sign_agreement"] is not None and out["sign_agreement"] >= 0.75:
        out["label"] = "stable"
    else:
        out["label"] = "moderate"
    if lead_only:
        out["note"] = "use lead_lag; same-day value overlaps hours after the Indian close"
    return out


def session_returns(level: pd.Series, sessions: pd.DatetimeIndex, diff: bool = False) -> pd.Series:
    """Change between consecutive sessions only. A gap in the stored series
    (missing days, partial backfill) yields NaN instead of a multi-day jump."""
    level = level.dropna()
    level = level[level.index.isin(sessions)]
    pos = sessions.get_indexer(level.index)
    consecutive = pd.Series(np.diff(pos, prepend=-10) == 1, index=level.index)
    chg = level.diff() if diff else level.pct_change()
    return chg.where(consecutive)


def coverage(level: pd.Series, sessions: pd.DatetimeIndex, last: int = 756) -> float:
    recent = sessions[-last:]
    return float(level.dropna().index.isin(recent).sum() / max(len(recent), 1))


def zscore_last(s: pd.Series, window: int = 252) -> float | None:
    s = s.dropna()
    if len(s) < 60:
        return None
    w = s.tail(window)
    sd = w.std()
    return float((w.iloc[-1] - w.mean()) / sd) if sd > 0 else None


# ------------------------------------------------------------- market build
def _ew_index(C: pd.DataFrame, TV: pd.DataFrame, n: int = 500) -> pd.Series:
    """Equal-weighted index of the n most liquid names, membership from the
    previous session's 20-day turnover (no look-ahead)."""
    ret = C.pct_change(fill_method=None)
    tv20 = TV.rolling(20, min_periods=10).mean().shift(1)
    rank = tv20.rank(axis=1, ascending=False)
    r = ret.where(rank <= n).mean(axis=1)
    return (1 + r.fillna(0)).cumprod()


def build(save: bool = True) -> dict:
    px = prices.load_panel(start="2017-01-01", with_history=True)
    C = px.pivot(index="DATE_DT", columns="SYMBOL", values="CLOSE").sort_index()
    TV = px.pivot(index="DATE_DT", columns="SYMBOL", values="TRADED_VALUE").sort_index()
    asof = C.index[-1]

    idx = idx_mod.wide("CLOSE")
    mac = macro_mod.wide()
    fl = flows_mod.cash_wide()
    fpos = flows_mod.fii_positioning()

    sessions = C.index
    # Broad index: official Nifty 500 when its history is (nearly) complete, else EW proxy
    if idx_mod.NIFTY500 in idx.columns and coverage(idx[idx_mod.NIFTY500], sessions) >= 0.95:
        n500 = idx[idx_mod.NIFTY500].dropna()
        n500_src = "NSE Nifty 500"
    else:
        n500 = _ew_index(C, TV)
        n500_src = "equal-weighted top-500 proxy (official Nifty 500 history incomplete)"
    n500 = n500[n500.index <= asof]
    r500 = session_returns(n500, sessions).dropna()

    # ---- broad regime (same three triggers as the backtested V5 regime) ----
    sma200 = n500.rolling(200, min_periods=150).mean()
    sma50 = n500.rolling(50, min_periods=40).mean()
    rv20 = r500.rolling(20).std() * np.sqrt(252)
    liquid = TV.rolling(20, min_periods=10).mean() >= 1e7
    above50 = (C > C.rolling(50, min_periods=30).mean()) & liquid
    breadth = above50.sum(axis=1) / liquid.sum(axis=1).replace(0, np.nan)
    trend_ok = (n500 > sma200).reindex(C.index).ffill()
    breadth_ok = breadth > 0.55
    vol_ok = (rv20 < 0.22).reindex(C.index).ffill()
    score = trend_ok.astype(float) + breadth_ok.astype(float) + vol_ok.astype(float)
    regime_series = score.map({3.0: "RISK_ON", 2.0: "RISK_NEU"}).fillna("RISK_OFF")
    vix = idx[idx_mod.VIX].dropna() if idx_mod.VIX in idx.columns else pd.Series(dtype=float)
    vix_ok = len(vix) > 60 and coverage(vix, sessions, 252) >= 0.9
    vol_state = (rv20 > rv20.rolling(756, min_periods=250).median()).map({True: "high_vol", False: "low_vol"})

    # ---- risk appetite composite ----
    comps = {}
    if {idx_mod.SMALLCAP, idx_mod.LARGECAP} <= set(idx.columns):
        if coverage(idx[idx_mod.SMALLCAP], sessions, 252) >= 0.9:
            rel = idx[idx_mod.SMALLCAP].pct_change(20) - idx[idx_mod.LARGECAP].pct_change(20)
            comps["smallcap_vs_largecap_20d"] = zscore_last(rel.tail(252))
    if vix_ok:
        comps["vix_change_20d (inverted)"] = (lambda z: -z if z is not None else None)(zscore_last(vix.diff(20)))
    if "USDINR" in mac.columns:
        comps["inr_strength_20d"] = (lambda z: -z if z is not None else None)(zscore_last(mac["USDINR"].pct_change(20)))
    if not fl.empty and "FII_NET" in fl.columns and fl["FII_NET"].dropna().shape[0] >= 60:
        comps["fii_cash_20d"] = zscore_last(fl["FII_NET"].rolling(20).sum())
    if not fpos.empty:
        comps["fii_index_futures_long_share"] = zscore_last(fpos["FII_IDXFUT_LONG_SHARE"])
    comps["breadth"] = zscore_last(breadth)
    valid = {k: v for k, v in comps.items() if v is not None}
    risk_appetite = float(np.mean(list(valid.values()))) if valid else None

    last_fii = None
    if not fl.empty and "FII_NET" in fl.columns:
        last_fii = {"date": fl.index[-1].strftime("%Y-%m-%d"), "fii_net_cr": float(fl["FII_NET"].iloc[-1]),
                    "dii_net_cr": float(fl["DII_NET"].iloc[-1]) if "DII_NET" in fl.columns else None,
                    "history_sessions": int(fl["FII_NET"].notna().sum())}

    broad = {
        "index_source": n500_src,
        "regime": str(regime_series.iloc[-1]),
        "trend": {"close": float(n500.iloc[-1]), "sma200": float(sma200.iloc[-1]),
                  "above_200dma": bool(n500.iloc[-1] > sma200.iloc[-1]),
                  "sma50_slope_20d": float(sma50.iloc[-1] / sma50.iloc[-21] - 1) if len(sma50.dropna()) > 21 else None},
        "volatility": {"realized_20d": float(rv20.iloc[-1]),
                       "india_vix": float(vix.iloc[-1]) if len(vix) else None,
                       "vix_1y_percentile": float((vix.tail(252) < vix.iloc[-1]).mean()) if vix_ok else None,
                       "state": str(vol_state.dropna().iloc[-1]) if vol_state.notna().any() else None},
        "breadth_above_50dma": float(breadth.iloc[-1]),
        "liquidity_turnover_vs_120d": float(TV.sum(axis=1).rolling(20).mean().iloc[-1] /
                                            TV.sum(axis=1).rolling(120).mean().iloc[-1]),
        "risk_appetite_z": round(risk_appetite, 3) if risk_appetite is not None else None,
        "risk_appetite_components": {k: (round(v, 3) if v is not None else None) for k, v in comps.items()},
        "flows_latest": last_fii,
    }
    if idx_mod.NIFTY500 in idx.columns:
        pe = idx_mod.wide("PE")
        if idx_mod.NIFTY500 in pe.columns and coverage(pe[idx_mod.NIFTY500], sessions) >= 0.95:
            s = pe[idx_mod.NIFTY500].dropna()
            broad["valuation"] = {"nifty500_pe": float(s.iloc[-1]),
                                  "pe_percentile_since": s.index[0].strftime("%Y-%m-%d"),
                                  "pe_percentile": float((s < s.iloc[-1]).mean())}

    # ---- sectors (Nifty 500 industry groups, equal-weighted constituents) ----
    smap = _sector_map()
    ret = C.pct_change(fill_method=None)
    sectors, sector_ret = {}, {}
    for sec in sorted(set(smap.values())):
        mem = [s for s, v in smap.items() if v == sec and s in C.columns]
        if len(mem) < 4:
            continue
        sr = ret[mem].mean(axis=1).dropna()
        sector_ret[sec] = sr
        lvl = (1 + sr).cumprod()
        rs63 = float(lvl.iloc[-1] / lvl.iloc[-64] - 1 - (n500.iloc[-1] / n500.iloc[-64] - 1)) if len(lvl) > 64 else None
        sectors[sec] = {"members": len(mem), "rs_vs_n500_63d": rs63,
                        "above_200dma": bool(lvl.iloc[-1] > lvl.rolling(200).mean().iloc[-1]),
                        "breadth_above_50dma": float(above50[mem].iloc[-1].mean()),
                        "vol_63d": float(sr.tail(63).std() * np.sqrt(252))}
    ranked = sorted([k for k in sectors if sectors[k]["rs_vs_n500_63d"] is not None],
                    key=lambda k: -sectors[k]["rs_vs_n500_63d"])
    for i, k in enumerate(ranked):
        sectors[k]["rs_rank"] = i + 1

    # ---- macro factors (daily changes) ----
    factors = {}
    if "GOLD_INR" in mac.columns:
        factors["gold_inr"] = (mac["GOLD_INR"].pct_change(), True)
    if idx_mod.GSEC10 in idx.columns:
        # bond total-return index; NSE hours, so same-day comparison is valid
        factors["gsec_10y_index"] = (session_returns(idx[idx_mod.GSEC10], sessions), False)
    if "USDINR" in mac.columns:
        factors["usdinr"] = (mac["USDINR"].pct_change(), True)
    if "BRENT" in mac.columns:
        factors["brent"] = (mac["BRENT"].pct_change(), True)
    if "US10Y" in mac.columns:
        factors["us10y_bp"] = (mac["US10Y"].diff() * 100, True)
    if "SPX" in mac.columns:
        factors["sp500"] = (mac["SPX"].pct_change(), True)
    if len(vix):
        factors["india_vix_change"] = (session_returns(vix, sessions, diff=True), False)
    if not fl.empty and "FII_NET" in fl.columns:
        factors["fii_cash_net_cr"] = (fl["FII_NET"], False)
    if not fpos.empty:
        factors["fii_idxfut_net_change"] = (fpos["FII_IDXFUT_NET"].diff(), False)

    rel = {}
    for name, (x, lead_only) in factors.items():
        x = x.replace([np.inf, -np.inf], np.nan)
        rel[name] = relationship(r500, x, regime_series, vol_state, lead_only)

    # ---- sector partial betas: sector return on [Nifty 500, factor], last 252d ----
    sector_macro, sector_mkt = {}, {}
    for sec, sr in sector_ret.items():
        df0 = pd.concat({"s": sr, "m": r500}, axis=1, sort=True).dropna().tail(252)
        if len(df0) > 120:
            sector_mkt[sec] = round(float(np.cov(df0["s"], df0["m"])[0, 1] / df0["m"].var()), 3)
        sector_macro[sec] = {}
        for name in ("gold_inr", "gsec_10y_index", "usdinr", "brent", "us10y_bp"):
            if name not in factors:
                continue
            x, lead_only = factors[name]
            xx = x.shift(1) if lead_only else x      # global series: use the previous day's move
            d = pd.concat({"s": sr, "m": r500, "x": xx}, axis=1, sort=True).dropna().tail(252)
            if len(d) < 120:
                continue
            X = np.column_stack([np.ones(len(d)), d["m"].values, d["x"].values])
            coef, res, *_ = np.linalg.lstsq(X, d["s"].values, rcond=None)
            resid = d["s"].values - X @ coef
            s2 = resid.var(ddof=3)
            cov = s2 * np.linalg.inv(X.T @ X)
            t = coef[2] / math.sqrt(cov[2, 2]) if cov[2, 2] > 0 else 0
            if abs(t) >= 2:          # only statistically distinguishable exposures are kept
                sector_macro[sec][name] = round(float(coef[2]), 4)

    state = {
        "as_of": asof.strftime("%Y-%m-%d"),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "broad": broad,
        "sectors": sectors,
        "macro_relationships": rel,
        "sector_macro_betas": sector_macro,
        "sector_market_beta": sector_mkt,
        "notes": [
            "Relationships are measured, not assumed; see label, interval and sign agreement before using one.",
            "Same-day correlations with global series overlap hours after the Indian close; use lead_lag for decisions.",
            "Sector macro betas are partial (controlling for the Nifty 500) over the last 252 sessions and "
            "kept only when |t| >= 2.",
        ],
    }
    if save:
        paths.ensure_dirs()
        paths.MARKET_STATE.write_text(json.dumps(state, indent=2, default=float))
        hist_row = pd.DataFrame([{"DATE": asof.strftime("%Y%m%d"), "REGIME": broad["regime"],
                                  "RISK_APPETITE_Z": broad["risk_appetite_z"],
                                  "BREADTH": broad["breadth_above_50dma"],
                                  "RV20": broad["volatility"]["realized_20d"],
                                  "VIX": broad["volatility"]["india_vix"]}])
        if paths.MARKET_STATE_HISTORY.exists():
            h = pd.read_parquet(paths.MARKET_STATE_HISTORY)
            hist_row = pd.concat([h[h["DATE"] != hist_row["DATE"].iloc[0]], hist_row], ignore_index=True)
        hist_row.to_parquet(paths.MARKET_STATE_HISTORY, index=False)
    return state


def load() -> dict:
    if paths.MARKET_STATE.exists():
        return json.loads(paths.MARKET_STATE.read_text())
    return {}


def security_state(symbol: str, C: pd.DataFrame, n500: pd.Series | None = None) -> dict:
    """Trend / volatility / relative strength for one name (adjusted closes)."""
    s = C[symbol].dropna() if symbol in C.columns else pd.Series(dtype=float)
    if len(s) < 60:
        return {"available": False}
    r = s.pct_change()
    out = {"available": True, "close": float(s.iloc[-1]),
           "above_50dma": bool(s.iloc[-1] > s.tail(50).mean()),
           "above_200dma": bool(len(s) >= 200 and s.iloc[-1] > s.tail(200).mean()),
           "vol_63d": float(r.tail(63).std() * np.sqrt(252)),
           "ret_63d": float(s.iloc[-1] / s.iloc[-64] - 1) if len(s) > 64 else None,
           "dist_52w_high": float(s.iloc[-1] / s.tail(252).max() - 1)}
    if n500 is not None and len(n500) > 64 and out["ret_63d"] is not None:
        out["rs_vs_n500_63d"] = out["ret_63d"] - float(n500.iloc[-1] / n500.iloc[-64] - 1)
    return out


if __name__ == "__main__":
    st = build()
    b = st["broad"]
    print(f"as of {st['as_of']}: {b['regime']} | risk appetite z {b['risk_appetite_z']} | "
          f"breadth {b['breadth_above_50dma']:.0%} | RV20 {b['volatility']['realized_20d']:.1%}")
    for k, v in st["macro_relationships"].items():
        print(f"  {k:24s} {v.get('label'):12s} corr {v.get('corr')} ci {v.get('ci95')} "
              f"126d {v.get('corr_126d')} agree {v.get('sign_agreement')} n {v.get('n')}")
