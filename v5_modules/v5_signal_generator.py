"""
v5_signal_generator.py
======================
Heart of the V5 system. Run daily after the market close.

Sequence:
  1. Load master_history.csv (long-format daily OHLCV)
  2. Compute multifactor V5 score with PIT lag
  3. Apply universe filters (price, liquidity, EQ series)
  4. Apply quality gate (Screener.in cached data)
  5. Apply regime detector (Nifty 500 vs 200-DMA, breadth, VIX proxy)
  6. Select Top-N by V5 score
  7. ATR-based position sizing (1% R)
  8. Risk overlay caps (7% stock, 22% sector)
  9. Compute confidence score 1-10 per pick
 10. Write daily_live_portfolio.csv (the Telegram broadcast input)

Outputs (under --output-dir):
  daily_live_portfolio.csv     ← what telegram_broadcaster reads
  daily_regime.json            ← regime state for the day
  daily_diagnostics.csv        ← every diagnostic (universe size at each stage)

Usage:
  python v5_signal_generator.py \\
      --master ./data/market_database/master_history.csv \\
      --fundamentals ./data/fundamentals/screener.sqlite \\
      --output-dir ./data/live_signals \\
      --top-n 15 --portfolio-inr 200000 --risk-pct 0.01
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

# So that modules in this folder import cleanly when v5_signal_generator
# is invoked from cron with an absolute path.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# =========================================================
# Quality gate thresholds (kept in sync with quality_gate.py)
# =========================================================
QG_MIN_ROCE = 12.0
QG_MAX_DE = 1.0
QG_MAX_PROMOTER_DROP_PP = 2.0
QG_MIN_MARKET_CAP_INR = 5_000_000_000   # ₹500 Cr


# =========================================================
# 1. Master DB loading
# =========================================================
def load_master(master_path: Path, lookback_days: int = 400) -> pd.DataFrame:
    """Load the daily OHLCV master. Keeps only the last N days to be fast."""
    print(f"[INFO] loading {master_path}")
    df = pd.read_csv(master_path, low_memory=False)
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    # Date parsing
    if "DATE_DT" not in df.columns:
        df["DATE_DT"] = pd.to_datetime(df["DATE"].astype(str), format="%Y%m%d", errors="coerce")
    else:
        df["DATE_DT"] = pd.to_datetime(df["DATE_DT"], errors="coerce")
    df = df[df["DATE_DT"].notna()].copy()
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.upper() == "EQ"].copy()
    # numeric coercion
    for c in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "CORPORATE_ACTION_FLAG" not in df.columns:
        df["CORPORATE_ACTION_FLAG"] = 0
    df["CORPORATE_ACTION_FLAG"] = pd.to_numeric(df["CORPORATE_ACTION_FLAG"], errors="coerce").fillna(0).astype(int)
    # Trim to last lookback_days
    cutoff = df["DATE_DT"].max() - pd.Timedelta(days=lookback_days * 1.5)
    df = df[df["DATE_DT"] >= cutoff].sort_values(["SYMBOL", "DATE_DT"]).reset_index(drop=True)
    print(f"[INFO] master rows kept: {len(df):,}  symbols: {df['SYMBOL'].nunique():,}  dates: {df['DATE_DT'].nunique()}")
    return df


# =========================================================
# 2. Factor computation (V5)
# =========================================================
def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("SYMBOL", sort=False)
    df["MOM_3_1"] = g["CLOSE"].shift(21) / g["CLOSE"].shift(63) - 1
    df["MOM_6_1"] = g["CLOSE"].shift(21) / g["CLOSE"].shift(126) - 1
    df["RET_1D"] = g["CLOSE"].pct_change(1)
    df["VOL_90D"] = g["RET_1D"].rolling(90, min_periods=60).std().reset_index(level=0, drop=True) * np.sqrt(252)
    df["VOL_20D"] = g["VOLUME"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["TV_20D"] = g["TRADED_VALUE"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["VOL_RATIO_20D"] = df["VOLUME"] / (df["VOL_20D"] + 1e-9)
    df["TV_RATIO_20D"] = df["TRADED_VALUE"] / (df["TV_20D"] + 1e-9)
    df["BREADTH_RAW"] = 0.5 * df["VOL_RATIO_20D"] + 0.5 * df["TV_RATIO_20D"]
    # ATR(14) for position sizing
    high_low = df["HIGH"] - df["LOW"]
    high_pc = (df["HIGH"] - g["CLOSE"].shift(1)).abs()
    low_pc = (df["LOW"] - g["CLOSE"].shift(1)).abs()
    df["TR"] = pd.concat([high_low, high_pc, low_pc], axis=1).max(axis=1)
    df["ATR14"] = g["TR"].rolling(14, min_periods=10).mean().reset_index(level=0, drop=True)
    return df


def rank_and_score(snap: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional ranks at the snapshot date."""
    def rank(s):
        return s.rank(pct=True, method="average")
    snap = snap.copy()
    for c in ["MOM_3_1", "MOM_6_1", "VOL_90D", "BREADTH_RAW"]:
        snap[c + "_R"] = rank(snap[c])
    snap["MOM_R"] = snap[["MOM_3_1_R", "MOM_6_1_R"]].mean(axis=1)
    snap["LOWVOL_R"] = 1.0 - snap["VOL_90D_R"]
    snap["BREADTH_R"] = snap["BREADTH_RAW_R"]
    snap["V5_SCORE"] = 0.50 * snap["MOM_R"] + 0.30 * snap["LOWVOL_R"] + 0.20 * snap["BREADTH_R"]
    return snap


# =========================================================
# 3. Quality gate from cached Screener data
# =========================================================
def load_fundamentals(sqlite_path: Path) -> pd.DataFrame:
    """Read Screener.in cache into a flat DataFrame keyed by SYMBOL."""
    if not sqlite_path.exists():
        print(f"[WARN] no fundamentals cache at {sqlite_path}; quality gate will be permissive")
        return pd.DataFrame()
    con = sqlite3.connect(str(sqlite_path))
    rows = con.execute("SELECT symbol, payload FROM sc").fetchall()
    con.close()
    out = []
    for sym, payload in rows:
        try:
            d = json.loads(payload)
            d["SYMBOL"] = sym.upper()
            out.append(d)
        except Exception:
            continue
    fdf = pd.DataFrame(out)
    if fdf.empty: return fdf
    return fdf


def apply_quality_gate(universe: pd.DataFrame, fdf: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Filter the day's universe by the quality gate. Returns (filtered_universe, stats_dict)."""
    stats = {"before": len(universe)}
    if fdf.empty:
        stats["after"] = len(universe)
        stats["reason"] = "no_fundamentals_data"
        return universe, stats
    u = universe.merge(fdf, on="SYMBOL", how="left")
    rules = {
        "ROCE >= 12":   u["roce_pct"].fillna(-1) >= QG_MIN_ROCE,
        "D/E <= 1":     u["debt_to_equity"].fillna(99) <= QG_MAX_DE,
        "CFO last yr +": u["cfo_last_year"].fillna(-1) > 0,
        "Promoter drop <= 2pp": u["promoter_drop_qoq_pp"].fillna(0) <= QG_MAX_PROMOTER_DROP_PP,
        "MarketCap >= ₹500 Cr": u["market_cap_inr"].fillna(0) >= QG_MIN_MARKET_CAP_INR,
        "_fetched_ok":  u["_fetched_ok"].fillna(False),
    }
    passes = pd.DataFrame(rules)
    u["QG_PASS"] = passes.all(axis=1)
    u["QG_FAIL_REASONS"] = passes.apply(
        lambda r: "; ".join([k for k, v in r.items() if not v]), axis=1
    )
    stats["after"] = int(u["QG_PASS"].sum())
    stats["rejection_breakdown"] = {k: int((~v).sum()) for k, v in rules.items()}
    return u[u["QG_PASS"]].copy(), stats


# =========================================================
# 4. Regime detector
# =========================================================
def detect_regime(master: pd.DataFrame, snap_date: pd.Timestamp) -> tuple[str, dict]:
    """
    Proxy regime detector using only OHLCV data (no external API needed):
      Trend  : (mean close of top-100 traded value stocks) > 200-DMA?
      Breadth: pct of all stocks with CLOSE > 50-DMA?  > 55% → ok
      Vol    : top-100 stocks' avg 20-day return std × √252 < 22% → ok
    """
    g = master.groupby("SYMBOL", sort=False)
    master = master.copy()
    master["SMA50"] = g["CLOSE"].transform(lambda x: x.rolling(50, min_periods=30).mean())
    master["SMA200"] = g["CLOSE"].transform(lambda x: x.rolling(200, min_periods=120).mean())
    master["VOL_20D_DAILY"] = g["CLOSE"].pct_change(1).rolling(20, min_periods=10).std().reset_index(level=0, drop=True)

    today = master[master["DATE_DT"] == snap_date].copy()
    if today.empty:
        return "RISK_NEU", {"reason": "no_snapshot"}

    today_liquid = today.nlargest(100, "TRADED_VALUE")
    mean_close = float(today_liquid["CLOSE"].mean())
    mean_200dma = float(today_liquid["SMA200"].dropna().mean()) if today_liquid["SMA200"].notna().sum() > 30 else None
    trend_ok = bool(mean_200dma and mean_close > mean_200dma)

    has_50 = today["SMA50"].notna()
    breadth_above = (today.loc[has_50, "CLOSE"] > today.loc[has_50, "SMA50"]).mean() if has_50.sum() > 100 else 0.5
    breadth_ok = bool(breadth_above > 0.55)

    vol_proxy = float(today_liquid["VOL_20D_DAILY"].mean() * np.sqrt(252))
    vol_ok = bool(vol_proxy < 0.22)

    score = int(trend_ok) + int(breadth_ok) + int(vol_ok)
    if score == 3:
        state = "RISK_ON"
    elif score == 2:
        state = "RISK_NEU"
    else:
        state = "RISK_OFF"

    triggers = {
        "Trend":   "✓" if trend_ok else "✗",
        "Breadth": "✓" if breadth_ok else "✗",
        "VIX":     "✓" if vol_ok else "✗",
    }
    detail = {
        "state": state, "score": score, "triggers": triggers,
        "mean_close": mean_close, "mean_200dma": mean_200dma,
        "breadth_above_50dma": breadth_above, "vol_proxy_ann": vol_proxy,
    }
    return state, detail


def regime_sizing(state: str) -> dict:
    if state == "RISK_ON":
        return {"gross": 1.00, "max_new": 5}
    if state == "RISK_NEU":
        return {"gross": 0.70, "max_new": 3}
    return {"gross": 0.40, "max_new": 0}


# =========================================================
# 5. Sizing & confidence
# =========================================================
def size_positions(
    top: pd.DataFrame, portfolio_inr: float, risk_pct: float, max_w: float, gross: float
) -> pd.DataFrame:
    """ATR-based position sizing: risk = 1% of equity per trade, stop at -2*ATR."""
    top = top.copy()
    atr = top["ATR14"].fillna(top["ATR14"].median())
    risk_per_trade = portfolio_inr * risk_pct
    stop_distance = 2.0 * atr
    # Position size such that stop-loss costs risk_per_trade
    size_inr = (risk_per_trade / stop_distance) * top["CLOSE"]
    # Cap per-stock at max_w
    size_inr = np.minimum(size_inr, portfolio_inr * max_w)
    # Renormalize to fit gross exposure target
    total = size_inr.sum()
    target_total = portfolio_inr * gross
    if total > target_total:
        size_inr = size_inr * (target_total / total)
    top["SIZE_INR"] = size_inr.round(0)
    top["STOP_PRICE"] = (top["CLOSE"] - 2.0 * atr).round(2)
    top["WEIGHT"] = (size_inr / portfolio_inr).round(4)
    return top


def confidence_score(row: pd.Series, state: str) -> float:
    """1-10 conviction composite."""
    factor = row.get("V5_SCORE", 0.5) * 10
    liquidity = min(10.0, np.log10(max(row.get("TRADED_VALUE", 1), 1)) - 5)  # ~7 if 1Cr ADV
    regime_bonus = {"RISK_ON": 1.0, "RISK_NEU": 0.5, "RISK_OFF": 0.0}[state]
    qg_bonus = 1.0  # already passed
    raw = 0.55 * factor + 0.25 * liquidity + 0.15 * regime_bonus * 10 + 0.05 * qg_bonus * 10
    return round(float(np.clip(raw, 0, 10)), 1)


# =========================================================
# 6. Main
# =========================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", required=True)
    ap.add_argument("--fundamentals", default="./data/fundamentals/screener.sqlite")
    ap.add_argument("--output-dir", default="./data/live_signals")
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--portfolio-inr", type=float, default=200000.0)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    ap.add_argument("--max-stock-w", type=float, default=0.10)
    ap.add_argument("--min-close", type=float, default=20.0)
    ap.add_argument("--min-traded-value", type=float, default=10_000_000.0)
    args = ap.parse_args()

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    diag = {"timestamp": datetime.utcnow().isoformat()}

    # Load
    master = load_master(Path(args.master))
    snap_date = master["DATE_DT"].max()
    diag["snapshot_date"] = snap_date.strftime("%Y-%m-%d")
    print(f"[INFO] snapshot date: {snap_date.date()}")

    # Compute factors over full window then take snapshot
    master = compute_factors(master)
    g = master.groupby("SYMBOL", sort=False)
    # PIT shift so factors at snap date reflect data through snap_date - 1
    snap = master[master["DATE_DT"] == snap_date].copy()
    # Universe filter
    diag["universe_raw"] = len(snap)
    snap = snap[snap["CLOSE"] >= args.min_close]
    snap = snap[snap["TRADED_VALUE"] >= args.min_traded_value]
    snap = snap[snap["CORPORATE_ACTION_FLAG"] == 0]
    snap = snap.dropna(subset=["MOM_3_1", "VOL_90D"])
    diag["universe_after_liquidity"] = len(snap)

    # Quality gate
    fdf = load_fundamentals(Path(args.fundamentals))
    snap, qg_stats = apply_quality_gate(snap, fdf)
    diag["quality_gate"] = qg_stats
    diag["universe_after_quality_gate"] = len(snap)

    # Regime detection
    state, regime_detail = detect_regime(master, snap_date)
    diag["regime"] = regime_detail
    sizing_params = regime_sizing(state)

    # If RISK_OFF: emit no new entries
    if sizing_params["max_new"] == 0 or len(snap) == 0:
        print(f"[WARN] No new entries: regime={state}, universe={len(snap)}")
        # Write empty portfolio file
        empty = pd.DataFrame(columns=[
            "DATE", "SYMBOL", "CLOSE", "STOP_PRICE", "WEIGHT", "SIZE_INR",
            "CONFIDENCE", "FACTOR_SCORE", "V5_SCORE", "ACTION", "REGIME"])
        empty.to_csv(outdir / "daily_live_portfolio.csv", index=False)
    else:
        # Rank
        snap = rank_and_score(snap)
        top = snap.nlargest(args.top_n, "V5_SCORE").copy()
        # Size
        top = size_positions(top, args.portfolio_inr, args.risk_pct,
                              args.max_stock_w, sizing_params["gross"])
        top["CONFIDENCE"] = top.apply(lambda r: confidence_score(r, state), axis=1)
        top["ACTION"] = "ENTER"
        top["REGIME"] = state
        top["DATE"] = snap_date.strftime("%Y%m%d")
        cols = ["DATE", "SYMBOL", "CLOSE", "STOP_PRICE", "WEIGHT", "SIZE_INR",
                "CONFIDENCE", "V5_SCORE", "ACTION", "REGIME"]
        top[cols].sort_values("CONFIDENCE", ascending=False).to_csv(
            outdir / "daily_live_portfolio.csv", index=False)
        diag["picks"] = top["SYMBOL"].tolist()

    # Write regime json
    with open(outdir / "daily_regime.json", "w") as f:
        json.dump({"state": state, **regime_detail}, f, indent=2, default=str)
    # Diagnostics CSV
    pd.DataFrame([diag]).to_csv(outdir / "daily_diagnostics.csv", index=False)

    print("\n[DONE] V5 signal generation complete.")
    print(f"  regime: {state}")
    print(f"  universe: {diag['universe_raw']} → {diag['universe_after_liquidity']} → {diag['universe_after_quality_gate']}")
    if "picks" in diag:
        print(f"  picks: {diag['picks']}")
    print(f"  outputs in: {outdir}")


if __name__ == "__main__":
    main()
