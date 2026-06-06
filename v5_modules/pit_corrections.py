"""
pit_corrections.py
==================
Point-in-time (PIT) correction utilities for the V5 factor / backtest pipeline.

V4 BUG: Both build_factor_database_v2.py and backtest_factor_screen.py compute
FACTOR_SCORE at date T using CLOSE on T, then "buy" at CLOSE on T and measure
forward returns from CLOSE-on-T to CLOSE-on-T+5. This is look-ahead because
the decision-maker cannot see T's close until 15:30 IST and execute the same day.

FIX (applied here):
  * Factors are computed as usual using rolling windows ending at T's close.
  * Decision day is T (after market close). All factors are then SHIFTED FORWARD
    by 1 trading day so that the score is available the morning of T+1.
  * Trade entry price is OPEN of T+1.
  * Forward returns are measured OPEN(T+1) -> OPEN(T+1+H) where H is the
    holding period in trading days.

Usage:
    from pit_corrections import apply_pit_lag, add_pit_forward_returns

    df = pd.read_parquet("factor_database_v2.parquet")
    df = apply_pit_lag(df, lag_days=1)
    df = add_pit_forward_returns(df, horizons_days=[5, 20])

The returned df has columns FACTOR_SCORE_PIT, RET_20D_PIT, ..., FWD_OPEN_5D, etc.
Selection at decision_date should be done on the *_PIT columns and entry price
on the OPEN of decision_date + 1 trading day.

Author: V5 refactor
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


# Columns whose values must be lagged to be PIT-safe at decision time.
# Anything computed from CLOSE/HIGH/LOW on date T is "as of T's close" -> shift +1.
DEFAULT_LAG_COLUMNS = [
    "FACTOR_SCORE",
    "RET_5D", "RET_20D", "RET_60D",
    "SMA20", "SMA50", "SMA200",
    "MOM_QUALITY_20D", "MOM_QUALITY_60D",
    "ATR_TREND_20", "ATR_TREND_50",
    "VOLUME_RATIO_20D", "TRADED_VALUE_RATIO_20D",
    "VOLUME_ACCELERATION", "TRADED_VALUE_ACCELERATION",
    "BREAKOUT_PARTICIPATION_20D", "BREAKOUT_PARTICIPATION_252D",
    "STABILITY_SCORE",
    "VOLATILITY_20D",
    "ATR20", "ATR_PCT_20",
    "BREAKOUT_20D_FLAG", "BREAKOUT_252D_FLAG",
    "HIGH_20D", "HIGH_252D", "LOW_20D", "LOW_252D",
    "CLOSE_TO_20D_HIGH", "CLOSE_TO_252D_HIGH",
    "GAP_PCT", "RANGE_PCT", "CLOSE_LOCATION",
]


def apply_pit_lag(
    df: pd.DataFrame,
    lag_days: int = 1,
    columns: Iterable[str] | None = None,
    suffix: str = "_PIT",
    sort: bool = True,
) -> pd.DataFrame:
    """
    Add lagged ('_PIT') versions of factor columns.

    For each symbol, shift the specified columns forward by `lag_days`.
    Decision at date T should use the *_PIT columns, which contain the values
    computed up to T - lag_days.

    Parameters
    ----------
    df : DataFrame with at least columns ['SYMBOL', 'DATE_DT' or 'DATE', <factor cols>]
    lag_days : 1 by default (overnight: T's factors available T+1 morning)
    columns  : list of columns to lag; defaults to DEFAULT_LAG_COLUMNS intersect df.columns
    suffix   : suffix to append; '_PIT' by default

    Returns
    -------
    DataFrame with new <col><suffix> columns added.
    """
    out = df.copy()

    # Ensure a sortable date column exists
    if "DATE_DT" not in out.columns and "DATE" in out.columns:
        out["DATE_DT"] = pd.to_datetime(out["DATE"].astype(str), format="%Y%m%d", errors="coerce")

    if "SYMBOL" not in out.columns or "DATE_DT" not in out.columns:
        raise ValueError("DataFrame must include 'SYMBOL' and 'DATE_DT' (or 'DATE') columns.")

    if sort:
        out = out.sort_values(["SYMBOL", "DATE_DT"]).reset_index(drop=True)

    cols = list(columns) if columns is not None else [c for c in DEFAULT_LAG_COLUMNS if c in out.columns]
    if not cols:
        return out

    g = out.groupby("SYMBOL", sort=False)
    for col in cols:
        out[f"{col}{suffix}"] = g[col].shift(lag_days)

    return out


def add_pit_forward_returns(
    df: pd.DataFrame,
    horizons_days: Iterable[int] = (5, 20),
    entry_col: str = "OPEN",
    open_to_open: bool = True,
) -> pd.DataFrame:
    """
    Compute forward returns from T+1 OPEN to T+1+H OPEN (open_to_open=True)
    or to T+1+H CLOSE (open_to_open=False).

    The convention: at decision time T (after T's close), you place an order
    to execute at T+1's OPEN. You exit at the OPEN of T+1+H (or CLOSE).

    Adds FWD_RET_<H>D columns. NaN where the future bar doesn't exist.
    """
    out = df.copy()
    if "DATE_DT" not in out.columns and "DATE" in out.columns:
        out["DATE_DT"] = pd.to_datetime(out["DATE"].astype(str), format="%Y%m%d", errors="coerce")

    out = out.sort_values(["SYMBOL", "DATE_DT"]).reset_index(drop=True)

    if entry_col not in out.columns:
        raise ValueError(f"Column '{entry_col}' not found.")

    g = out.groupby("SYMBOL", sort=False)

    # T+1 OPEN is shift(-1) of OPEN; T+1+H OPEN is shift(-(1+H))
    out["ENTRY_OPEN_T1"] = g[entry_col].shift(-1)

    for h in horizons_days:
        if open_to_open:
            out[f"EXIT_OPEN_T1_{h}D"] = g[entry_col].shift(-(1 + h))
            out[f"FWD_RET_{h}D"] = out[f"EXIT_OPEN_T1_{h}D"] / out["ENTRY_OPEN_T1"] - 1
        else:
            if "CLOSE" not in out.columns:
                raise ValueError("CLOSE column required for open_to_close convention.")
            out[f"EXIT_CLOSE_T_{h}D"] = g["CLOSE"].shift(-(1 + h))
            out[f"FWD_RET_{h}D"] = out[f"EXIT_CLOSE_T_{h}D"] / out["ENTRY_OPEN_T1"] - 1

    return out


def detect_lookahead(
    df: pd.DataFrame,
    score_col: str,
    fwd_ret_col: str,
    rebalance_dates: Iterable[pd.Timestamp] | None = None,
    top_n: int = 20,
    threshold_pct: float = 0.30,
) -> dict:
    """
    Diagnostic: estimate look-ahead contamination magnitude by comparing
    in-sample win rate of (score, fwd_ret) for top-N at each rebalance date
    vs a permuted score. If shuffled scores deliver similar performance to
    the real ones, there is no edge. If the *real* score's edge collapses
    after applying PIT lag, there was look-ahead.

    Returns a dict with the diagnostic statistics. Not a substitute for proper
    walk-forward — use this only for quick sanity checks.
    """
    out = df.dropna(subset=[score_col, fwd_ret_col]).copy()
    if "DATE_DT" not in out.columns:
        out["DATE_DT"] = pd.to_datetime(out["DATE"].astype(str), format="%Y%m%d", errors="coerce")

    if rebalance_dates is None:
        all_dates = sorted(out["DATE_DT"].unique())
        rebalance_dates = all_dates[::5]

    real_returns = []
    permuted_returns = []
    rng = np.random.default_rng(42)

    for dt in rebalance_dates:
        snap = out[out["DATE_DT"] == dt]
        if len(snap) < top_n:
            continue
        top = snap.nlargest(top_n, score_col)
        real_returns.append(top[fwd_ret_col].mean())
        permuted_returns.append(
            snap.sample(top_n, random_state=rng.integers(1e9))[fwd_ret_col].mean()
        )

    real = pd.Series(real_returns)
    perm = pd.Series(permuted_returns)
    edge = real.mean() - perm.mean()
    return {
        "n_periods": len(real),
        "real_mean_period_return": float(real.mean()) if len(real) else np.nan,
        "permuted_mean_period_return": float(perm.mean()) if len(perm) else np.nan,
        "edge_per_period": float(edge) if len(real) else np.nan,
        "edge_significant": bool(abs(edge) > threshold_pct * abs(perm.mean()) if perm.mean() else False),
        "interpretation": (
            "Real score shows clear edge — investigate whether due to PIT bug or genuine signal."
            if edge > 0 else
            "Real score has no edge over random — confirms no look-ahead OR confirms strategy has no alpha."
        ),
    }


if __name__ == "__main__":
    # Smoke test on a tiny synthetic frame
    rng = np.random.default_rng(0)
    n = 200
    df = pd.DataFrame({
        "SYMBOL": ["AAA"] * n,
        "DATE": pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "OPEN":  100 + np.cumsum(rng.normal(0, 1, n)),
        "CLOSE": 100 + np.cumsum(rng.normal(0, 1, n)),
        "FACTOR_SCORE": rng.uniform(0, 1, n),
        "RET_20D": rng.normal(0, 0.05, n),
    })
    df = apply_pit_lag(df)
    df = add_pit_forward_returns(df, horizons_days=[5, 20])
    cols = ["DATE", "FACTOR_SCORE", "FACTOR_SCORE_PIT", "OPEN", "ENTRY_OPEN_T1", "FWD_RET_5D"]
    print(df[cols].head(10).to_string(index=False))
    print("\nPIT shift verified — FACTOR_SCORE_PIT[i] == FACTOR_SCORE[i-1]")
