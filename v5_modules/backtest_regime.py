"""
backtest_regime.py
==================
STANDALONE RESEARCH BACKTEST — old vs fixed regime logic, 2023-2026.

Purpose
-------
The live regime detector compared the AVERAGE VOLATILITY OF INDIVIDUAL STOCKS
(~35-44% annualised) against a 22% threshold that actually means INDEX-level
volatility (~13-21%). That units mismatch made the "VIX" trigger impossible to
pass, so RISK_ON was unreachable and the system was permanently capped at 70%
gross exposure. This script quantifies what that cost — and, more importantly,
whether the fix genuinely helps across many market conditions rather than just
the recent rally we happened to notice it in.

SAFETY: this script is READ-ONLY with respect to live state. It never touches
positions.csv, equity_log.csv, realized_trades.csv, daily_regime.json or the
benchmark files. All output goes to a separate --output-dir (default ./backtests).

Method
------
* Point-in-time correct: factors are computed from data up to the PREVIOUS close;
  entries are executed at the NEXT session's OPEN.
* Weekly rebalance on Mondays (matching live behaviour), max 15 concurrent names.
* ATR-based sizing: 1% equity risk per trade, stop at entry - 2*ATR(14),
  per-name weight cap, and total scaled to the regime's gross-exposure target.
* Exits: stop-loss (intraday low pierces stop), chandelier trail (highest close
  - 3*ATR), 22-day low trail, and a 25-trading-day time stop.
* Costs: configurable round-trip cost in basis points applied on entry and exit.
* Benchmark: an equal-weighted index of the liquid universe, rebuilt from the
  same price data (the official Nifty 500 series only exists from mid-2026).

Two variants are run over identical data:
    OLD   - vol trigger = mean of individual-stock 20d vol   (the bug)
    FIXED - vol trigger = 20d vol of the equal-weighted index, plus debounce

Usage
-----
    python backtest_regime.py \
        --master data/market_database/master_history.csv \
        --output-dir ./backtests \
        --start 2023-01-01 --top-n 15 --cost-bps 25
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------
# Load & shape
# ---------------------------------------------------------------
def load_wide(master_path: Path, start: str) -> dict:
    """Load master OHLCV and pivot to wide (dates x symbols) matrices."""
    use = ["DATE", "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"]
    df = pd.read_csv(master_path, low_memory=False)
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    keep = [c for c in use if c in df.columns]
    df = df[keep]
    df["DATE_DT"] = pd.to_datetime(df["DATE"].astype(str), format="%Y%m%d", errors="coerce")
    df = df[df["DATE_DT"].notna()]
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.upper() == "EQ"]
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    for c in ["OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["DATE_DT"] >= pd.Timestamp(start) - pd.Timedelta(days=400)]
    df = df.drop_duplicates(subset=["DATE_DT", "SYMBOL"], keep="last")

    out = {}
    for c in ["OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"]:
        out[c] = df.pivot(index="DATE_DT", columns="SYMBOL", values=c).sort_index()
    print(f"[INFO] loaded {out['CLOSE'].shape[0]} sessions x {out['CLOSE'].shape[1]} symbols")
    return out


# ---------------------------------------------------------------
# Factors & regime inputs (all vectorised on wide matrices)
# ---------------------------------------------------------------
def build_factors(W: dict) -> dict:
    C, H, L, TV = W["CLOSE"], W["HIGH"], W["LOW"], W["TRADED_VALUE"]
    F = {}
    F["MOM_3_1"] = C.shift(21) / C.shift(63) - 1
    F["MOM_6_1"] = C.shift(21) / C.shift(126) - 1
    ret1 = C.pct_change(fill_method=None)
    F["RET1"] = ret1
    F["VOL90"] = ret1.rolling(90, min_periods=60).std() * np.sqrt(252)
    F["VOL20"] = ret1.rolling(20, min_periods=10).std()
    tv20 = TV.rolling(20, min_periods=10).mean()
    F["BREADTH_RAW"] = TV / (tv20 + 1e-9)
    # ATR(14)
    prev_c = C.shift(1)
    tr = pd.concat([(H - L).stack(), (H - prev_c).abs().stack(), (L - prev_c).abs().stack()], axis=1).max(axis=1)
    F["ATR14"] = tr.unstack().rolling(14, min_periods=10).mean()
    F["SMA50"] = C.rolling(50, min_periods=30).mean()
    F["SMA200"] = C.rolling(200, min_periods=120).mean()
    F["LOW22"] = L.rolling(22, min_periods=10).min()
    return F


def regime_series(W: dict, F: dict, variant: str, debounce: int = 2,
                  breadth_thr: float = 0.55, vol_thr: float = 0.22) -> pd.DataFrame:
    """Compute the daily regime for a variant: 'old' or 'fixed'."""
    C, TV = W["CLOSE"], W["TRADED_VALUE"]
    dates = C.index
    rows = []
    prev_state, pending, pcount = None, None, 0

    # Equal-weighted index of the liquid-100 (recomputed daily membership is slow;
    # use a rolling liquid universe based on 20d average traded value)
    tv20 = TV.rolling(20, min_periods=10).mean()
    ret1 = F["RET1"]

    for i, d in enumerate(dates):
        tv_row = tv20.loc[d].dropna()
        if len(tv_row) < 100:
            rows.append((d, None, np.nan, np.nan, None)); continue
        liquid = tv_row.nlargest(100).index

        c_row = C.loc[d, liquid].dropna()
        s200 = F["SMA200"].loc[d, liquid].dropna()
        trend_ok = bool(len(s200) > 30 and c_row.mean() > s200.mean())

        s50_all = F["SMA50"].loc[d].dropna()
        c_all = C.loc[d, s50_all.index].dropna()
        common = s50_all.index.intersection(c_all.index)
        breadth = (c_all[common] > s50_all[common]).mean() if len(common) > 100 else 0.5
        breadth_ok = bool(breadth > breadth_thr)

        if variant == "old":
            v = F["VOL20"].loc[d, liquid].dropna()
            vol = float(v.mean() * np.sqrt(252)) if len(v) > 30 else np.nan
        else:
            # index-level: volatility of the equal-weighted liquid-100 return series
            lo = max(0, i - 25)
            idx_ret = ret1.iloc[lo:i + 1][liquid].mean(axis=1)
            vol = float(idx_ret.tail(20).std() * np.sqrt(252)) if idx_ret.notna().sum() >= 10 else np.nan
        vol_ok = bool(vol == vol and vol < vol_thr)

        score = int(trend_ok) + int(breadth_ok) + int(vol_ok)
        raw = "RISK_ON" if score == 3 else ("RISK_NEU" if score == 2 else "RISK_OFF")

        if variant == "old" or debounce <= 1:
            state = raw
        else:
            if prev_state is None:
                state = raw
            elif raw == prev_state:
                state, pending, pcount = prev_state, None, 0
            else:
                if raw == pending:
                    pcount += 1
                else:
                    pending, pcount = raw, 1
                if pcount >= debounce:
                    state, pending, pcount = raw, None, 0
                else:
                    state = prev_state
        prev_state = state
        rows.append((d, state, breadth, vol, raw))

    r = pd.DataFrame(rows, columns=["DATE", "state", "breadth", "vol", "raw"]).set_index("DATE")
    return r


GROSS = {"RISK_ON": 1.00, "RISK_NEU": 0.70, "RISK_OFF": 0.40}


# ---------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------
def simulate(W: dict, F: dict, reg: pd.DataFrame, start: str,
             top_n: int = 15, max_pos: int = 15, risk_pct: float = 0.01,
             max_w: float = 0.10, cost_bps: float = 25.0,
             min_close: float = 20.0, min_tv: float = 1e7,
             time_stop: int = 25, capital: float = 200000.0) -> dict:
    C, O, H, L, TV = W["CLOSE"], W["OPEN"], W["HIGH"], W["LOW"], W["TRADED_VALUE"]
    dates = [d for d in C.index if d >= pd.Timestamp(start)]
    cost = cost_bps / 10000.0

    cash = capital
    pos = {}           # sym -> dict
    eq_curve, exposure_log, trades = [], [], []

    score = (0.50 * F["MOM_3_1"].rank(axis=1, pct=True).add(F["MOM_6_1"].rank(axis=1, pct=True)).div(2)
             + 0.30 * (1 - F["VOL90"].rank(axis=1, pct=True))
             + 0.20 * F["BREADTH_RAW"].rank(axis=1, pct=True))

    for d in dates:
        # ---------- mark & manage existing ----------
        for sym in list(pos.keys()):
            if sym not in C.columns:
                continue
            c = C.at[d, sym] if d in C.index else np.nan
            lo = L.at[d, sym] if d in L.index else np.nan
            if not (c == c):
                continue
            p = pos[sym]
            p["days"] += 1
            p["hi"] = max(p["hi"], c)
            atr = F["ATR14"].at[d, sym] if d in F["ATR14"].index else np.nan
            cands = [p["stop"]]
            if atr == atr:
                cands.append(p["hi"] - 3.0 * atr)
            l22 = F["LOW22"].at[d, sym] if d in F["LOW22"].index else np.nan
            if l22 == l22:
                cands.append(l22)
            p["stop"] = max([x for x in cands if x == x])

            exit_px, reason = None, None
            if lo == lo and lo <= p["stop"]:
                exit_px, reason = p["stop"], "STOP_LOSS"
            elif p["days"] >= time_stop:
                exit_px, reason = c, "TIME_STOP"
            if exit_px is not None:
                proceeds = exit_px * p["qty"] * (1 - cost)
                cash += proceeds
                pnl = proceeds - p["cost_basis"]
                trades.append(dict(symbol=sym, entry=p["entry"], exit=float(exit_px),
                                   qty=p["qty"], reason=reason, days=p["days"], pnl=pnl))
                del pos[sym]

        # ---------- weekly entries (Mondays) ----------
        if d.weekday() == 0 and len(pos) < max_pos:
            prev = C.index[C.index.get_loc(d) - 1] if C.index.get_loc(d) > 0 else None
            if prev is not None:
                state = reg["state"].get(prev, "RISK_NEU") or "RISK_NEU"
                gross = GROSS.get(state, 0.7)
                s = score.loc[prev].dropna()
                elig = s.index
                elig = elig[(C.loc[prev, elig] >= min_close) & (TV.loc[prev, elig] >= min_tv)]
                elig = [x for x in elig if x not in pos]
                ranked = s[elig].nlargest(top_n)
                # equity for sizing
                mtm = sum(pos[k]["qty"] * (C.at[d, k] if k in C.columns and C.at[d, k] == C.at[d, k] else pos[k]["entry"]) for k in pos)
                equity = cash + mtm
                slots = max_pos - len(pos)
                picks = list(ranked.index)[:slots]
                if picks:
                    sizes = {}
                    for sym in picks:
                        atr = F["ATR14"].at[prev, sym]
                        op = O.at[d, sym] if d in O.index else np.nan
                        if not (atr == atr and atr > 0 and op == op and op > 0):
                            continue
                        size = (equity * risk_pct / (2.0 * atr)) * op
                        sizes[sym] = min(size, equity * max_w)
                    tot = sum(sizes.values())
                    target = equity * gross - sum(pos[k]["qty"] * C.at[d, k] for k in pos if k in C.columns and C.at[d, k] == C.at[d, k])
                    target = max(target, 0)
                    if tot > target and tot > 0:
                        sizes = {k: v * target / tot for k, v in sizes.items()}
                    for sym, size in sizes.items():
                        op = O.at[d, sym]
                        qty = int(size / op)
                        if qty < 1:
                            continue
                        outlay = qty * op * (1 + cost)
                        if outlay > cash:
                            continue
                        cash -= outlay
                        atr = F["ATR14"].at[prev, sym]
                        pos[sym] = dict(entry=float(op), qty=qty, stop=float(op - 2 * atr),
                                        hi=float(op), days=0, cost_basis=outlay)

        # ---------- record ----------
        mtm = 0.0
        for k, p in pos.items():
            c = C.at[d, k] if k in C.columns else np.nan
            mtm += p["qty"] * (c if c == c else p["entry"])
        eq = cash + mtm
        eq_curve.append((d, eq))
        exposure_log.append((d, mtm / eq if eq > 0 else 0.0, len(pos),
                             reg["state"].get(d, None)))

    eqs = pd.Series(dict(eq_curve)).sort_index()
    exp = pd.DataFrame(exposure_log, columns=["DATE", "exposure", "n_pos", "state"]).set_index("DATE")
    return dict(equity=eqs, exposure=exp, trades=pd.DataFrame(trades))


def benchmark_series(W: dict, start: str) -> pd.Series:
    """Equal-weighted index of the liquid universe (proxy for a broad index)."""
    C, TV = W["CLOSE"], W["TRADED_VALUE"]
    tv20 = TV.rolling(20, min_periods=10).mean()
    ret = C.pct_change(fill_method=None)
    vals, idx = [], []
    lvl = 100.0
    for d in C.index:
        if d < pd.Timestamp(start):
            continue
        row = tv20.loc[d].dropna()
        if len(row) < 100:
            continue
        liquid = row.nlargest(100).index
        r = ret.loc[d, liquid].mean()
        if r == r:
            lvl *= (1 + r)
        vals.append(lvl); idx.append(d)
    return pd.Series(vals, index=idx)


def stats(eq: pd.Series, bench: pd.Series) -> dict:
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    tot = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + tot) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan
    b = bench.reindex(eq.index).ffill()
    br = b.pct_change().dropna()
    btot = b.iloc[-1] / b.iloc[0] - 1
    ex = (r - br.reindex(r.index)).dropna()
    ir = ex.mean() / ex.std() * np.sqrt(252) if ex.std() > 0 else np.nan
    return dict(total_return_pct=round(tot * 100, 2), cagr_pct=round(cagr * 100, 2),
                max_drawdown_pct=round(dd * 100, 2), sharpe=round(float(sharpe), 2),
                benchmark_total_return_pct=round(btot * 100, 2),
                excess_vs_benchmark_pct=round((tot - btot) * 100, 2),
                information_ratio=round(float(ir), 2), years=round(yrs, 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="./data/market_database/master_history.csv")
    ap.add_argument("--output-dir", default="./backtests")
    ap.add_argument("--start", default="2023-06-01")
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--max-pos", type=int, default=15)
    ap.add_argument("--cost-bps", type=float, default=25.0)
    ap.add_argument("--capital", type=float, default=200000.0)
    args = ap.parse_args()

    outdir = Path(args.output_dir); outdir.mkdir(parents=True, exist_ok=True)
    W = load_wide(Path(args.master), args.start)
    F = build_factors(W)
    bench = benchmark_series(W, args.start)

    results = {}
    for variant in ["old", "fixed"]:
        print(f"\n[INFO] === regime variant: {variant} ===")
        reg = regime_series(W, F, variant)
        sim = simulate(W, F, reg, args.start, top_n=args.top_n, max_pos=args.max_pos,
                       cost_bps=args.cost_bps, capital=args.capital)
        st = stats(sim["equity"], bench)
        dist = sim["exposure"]["state"].value_counts().to_dict()
        st["regime_days"] = dist
        st["avg_exposure_pct"] = round(float(sim["exposure"]["exposure"].mean() * 100), 1)
        st["n_trades"] = int(len(sim["trades"]))
        if len(sim["trades"]):
            w = sim["trades"]["pnl"] > 0
            st["win_rate_pct"] = round(float(w.mean() * 100), 1)
        results[variant] = st
        sim["equity"].to_csv(outdir / f"equity_{variant}.csv", header=["EQUITY"])
        sim["trades"].to_csv(outdir / f"trades_{variant}.csv", index=False)
        reg.to_csv(outdir / f"regime_{variant}.csv")
        print(json.dumps(st, indent=2, default=str))

    bench.to_csv(outdir / "benchmark_proxy.csv", header=["LEVEL"])
    with open(outdir / "summary.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\n[DONE] outputs written to", outdir)


if __name__ == "__main__":
    main()
