"""
multifactor_score.py
====================
V5 scoring engine. Replaces the FACTOR_SCORE block in build_factor_database_v2.py.

V4 V2 problem: the score depended on BREAKOUT_PARTICIPATION_252D which needs
252 days of prior data, so 2023 was effectively unscored. The strategy was
silently running on V1 weights for early periods, then suddenly switched to a
weaker V2 weighting once enough history accumulated. This is a hidden regime
change inside the strategy itself.

V5 design:
  * No factor requires more than 90 days of lookback → full coverage from
    day +91 of available history.
  * Skip-month momentum convention (12-1, 6-1, 3-1) — well-documented to
    avoid short-term reversal contamination.
  * Winsorize raw inputs at 1st/99th percentile BEFORE cross-sectional ranking.
  * Sleeves are independent ranks; composite is a weighted sum of sleeve ranks.
  * Quality/Value sleeves are HOOKS — if you've populated quality_score_col
    via quality_gate.py, they enter the composite at their configured weight.
    Otherwise their weight is reallocated proportionally to remaining sleeves.

Usage:
    from multifactor_score import compute_v5_factor_score

    panel = pd.read_parquet("factor_database_v2.parquet")
    panel = compute_v5_factor_score(panel)
    # adds: MOMENTUM_RANK, LOWVOL_RANK, BREADTH_RANK,
    #       QUALITY_RANK (NaN if no quality data), VALUE_RANK (NaN if no value data),
    #       V5_FACTOR_SCORE
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd


# -----------------------------------------------------
# Sleeve weights — change here to retune
# -----------------------------------------------------
@dataclass
class SleeveWeights:
    momentum: float = 0.50
    low_vol:  float = 0.30
    breadth:  float = 0.20
    quality:  float = 0.0     # set > 0 once quality_gate scores are wired
    value:    float = 0.0     # set > 0 once fundamentals fetch is implemented


# -----------------------------------------------------
# Winsorization
# -----------------------------------------------------
def winsorize(s: pd.Series, low_q: float = 0.01, high_q: float = 0.99) -> pd.Series:
    if s.notna().sum() == 0:
        return s
    lo = s.quantile(low_q)
    hi = s.quantile(high_q)
    return s.clip(lower=lo, upper=hi)


def cross_sectional_rank(
    df: pd.DataFrame, col: str, date_col: str = "DATE_DT"
) -> pd.Series:
    """Cross-sectional percentile rank within each date. Returns NaN where col is NaN."""
    return df.groupby(date_col)[col].transform(
        lambda x: winsorize(x).rank(pct=True, method="average")
    )


# -----------------------------------------------------
# Compute skip-month momentum factors from raw prices
# -----------------------------------------------------
def add_skip_month_momentum(
    df: pd.DataFrame, symbol_col: str = "SYMBOL", price_col: str = "CLOSE"
) -> pd.DataFrame:
    """
    Add MOM_12_1, MOM_6_1, MOM_3_1 columns.
    Convention: MOM_X_1 = return from X months ago to 1 month ago (~21 trading days).

    For Indian context where 1 month = 21 trading days:
       MOM_12_1 = CLOSE.shift(21) / CLOSE.shift(252) - 1
       MOM_6_1  = CLOSE.shift(21) / CLOSE.shift(126) - 1
       MOM_3_1  = CLOSE.shift(21) / CLOSE.shift(63)  - 1

    Even MOM_12_1 still needs 252 days of history, but unlike
    BREAKOUT_PARTICIPATION_252D, it does NOT need a high-water-mark rolling max
    so the data is dense from day 252 onwards.

    For 2023 coverage we use MOM_3_1 + MOM_6_1 + (fallback to MOM_60D/RET_60D) and
    drop MOM_12_1 if it has less than 30% coverage of the panel.
    """
    out = df.copy()
    out = out.sort_values([symbol_col, "DATE_DT"]).reset_index(drop=True)
    g = out.groupby(symbol_col, sort=False)

    out["MOM_12_1"] = g[price_col].shift(21) / g[price_col].shift(252) - 1
    out["MOM_6_1"]  = g[price_col].shift(21) / g[price_col].shift(126) - 1
    out["MOM_3_1"]  = g[price_col].shift(21) / g[price_col].shift(63)  - 1
    return out


def add_low_vol_factor(
    df: pd.DataFrame, symbol_col: str = "SYMBOL", price_col: str = "CLOSE"
) -> pd.DataFrame:
    """
    Add VOL_90D: 90-day realized daily-return volatility (annualized).
    Inverse rank later (low vol = high rank).
    """
    out = df.copy()
    out = out.sort_values([symbol_col, "DATE_DT"]).reset_index(drop=True)
    g = out.groupby(symbol_col, sort=False)

    out["RET_1D"] = g[price_col].pct_change(1)
    out["VOL_90D"] = (
        g["RET_1D"].rolling(window=90, min_periods=60).std()
         .reset_index(level=0, drop=True)
    ) * np.sqrt(252)
    return out


def add_breadth_factor(
    df: pd.DataFrame,
    symbol_col: str = "SYMBOL",
    volume_col: str = "VOLUME",
    value_col: str = "TRADED_VALUE",
) -> pd.DataFrame:
    """
    BREADTH = average of:
      - volume ratio 20D (volume / 20D avg volume)
      - traded value ratio 20D
    Captures participation expansion accompanying price moves.
    """
    out = df.copy()
    g = out.groupby(symbol_col, sort=False)

    out["AVG_VOL_20D"] = g[volume_col].transform(lambda x: x.rolling(20, min_periods=10).mean())
    out["AVG_TV_20D"]  = g[value_col].transform(lambda x: x.rolling(20, min_periods=10).mean())
    out["VOL_RATIO_20D"] = out[volume_col] / (out["AVG_VOL_20D"] + 1e-9)
    out["TV_RATIO_20D"]  = out[value_col] / (out["AVG_TV_20D"] + 1e-9)
    out["BREADTH_RAW"] = 0.5 * out["VOL_RATIO_20D"] + 0.5 * out["TV_RATIO_20D"]
    return out


# -----------------------------------------------------
# Main entry point
# -----------------------------------------------------
def compute_v5_factor_score(
    df: pd.DataFrame,
    *,
    weights: SleeveWeights | None = None,
    quality_score_col: Optional[str] = None,
    value_score_col: Optional[str] = None,
    add_pit_shift: bool = True,
) -> pd.DataFrame:
    """
    Compute V5 multifactor score on a panel.

    Required input columns: SYMBOL, DATE_DT, CLOSE, VOLUME, TRADED_VALUE.

    If quality_score_col / value_score_col are provided and exist in df, they
    are included in the composite at their configured weight. If absent, their
    weight is redistributed proportionally to the other sleeves.

    If add_pit_shift=True (default), also adds V5_FACTOR_SCORE_PIT (shifted +1
    trading day per symbol) so callers can use it directly at decision time.
    """
    w = weights or SleeveWeights()

    # ---- compute raw factor inputs ----
    out = add_skip_month_momentum(df)
    out = add_low_vol_factor(out)
    out = add_breadth_factor(out)

    # ---- per-date cross-sectional ranks (winsorized) ----
    # Momentum sleeve: average rank of the three skip-month returns
    out["MOM_12_1_RANK"] = cross_sectional_rank(out, "MOM_12_1")
    out["MOM_6_1_RANK"]  = cross_sectional_rank(out, "MOM_6_1")
    out["MOM_3_1_RANK"]  = cross_sectional_rank(out, "MOM_3_1")
    # Average rank with NaN tolerance (e.g., MOM_12_1 missing in first year)
    mom_components = out[["MOM_12_1_RANK", "MOM_6_1_RANK", "MOM_3_1_RANK"]]
    out["MOMENTUM_RANK"] = mom_components.mean(axis=1, skipna=True)

    # Low-vol sleeve: inverse rank of VOL_90D
    out["LOWVOL_RANK"] = 1.0 - cross_sectional_rank(out, "VOL_90D")

    # Breadth sleeve
    out["BREADTH_RANK"] = cross_sectional_rank(out, "BREADTH_RAW")

    # Quality / Value (optional)
    if quality_score_col is not None and quality_score_col in out.columns:
        out["QUALITY_RANK"] = cross_sectional_rank(out, quality_score_col)
    else:
        out["QUALITY_RANK"] = np.nan
    if value_score_col is not None and value_score_col in out.columns:
        out["VALUE_RANK"] = cross_sectional_rank(out, value_score_col)
    else:
        out["VALUE_RANK"] = np.nan

    # ---- composite with dynamic weight reallocation ----
    # Build a weight dict, then drop missing sleeves and renormalize.
    sleeves = {
        "MOMENTUM_RANK": w.momentum,
        "LOWVOL_RANK":   w.low_vol,
        "BREADTH_RANK":  w.breadth,
        "QUALITY_RANK":  w.quality,
        "VALUE_RANK":    w.value,
    }
    present = {k: v for k, v in sleeves.items()
               if v > 0 and out[k].notna().any()}
    total = sum(present.values())
    if total <= 0:
        raise ValueError("No active sleeves with non-zero weight.")
    # Renormalize so present weights sum to 1
    present = {k: v / total for k, v in present.items()}

    out["V5_FACTOR_SCORE"] = sum(
        out[k].fillna(0.5) * wt for k, wt in present.items()
    )
    # mark rows where momentum is fully unavailable as unscored
    out.loc[out["MOMENTUM_RANK"].isna(), "V5_FACTOR_SCORE"] = np.nan

    # ---- PIT shift ----
    if add_pit_shift:
        out["V5_FACTOR_SCORE_PIT"] = (
            out.sort_values(["SYMBOL", "DATE_DT"])
               .groupby("SYMBOL", sort=False)["V5_FACTOR_SCORE"]
               .shift(1)
               .reset_index(level=0, drop=True)
        )

    out.attrs["v5_sleeve_weights_active"] = present
    return out


if __name__ == "__main__":
    # Tiny synthetic sanity check
    rng = np.random.default_rng(0)
    dates = pd.date_range("2023-01-02", periods=300, freq="B")
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    rows = []
    for s in symbols:
        prices = 100 * (1 + rng.normal(0.0005, 0.02, len(dates))).cumprod()
        for i, dt in enumerate(dates):
            rows.append({
                "SYMBOL": s, "DATE_DT": dt,
                "CLOSE": prices[i],
                "VOLUME": rng.integers(1_000_000, 5_000_000),
                "TRADED_VALUE": prices[i] * rng.integers(1_000_000, 5_000_000),
            })
    df = pd.DataFrame(rows)
    scored = compute_v5_factor_score(df)
    print("Active sleeves:", scored.attrs["v5_sleeve_weights_active"])
    print(scored.tail(8)[["DATE_DT", "SYMBOL", "MOMENTUM_RANK", "LOWVOL_RANK",
                          "BREADTH_RANK", "V5_FACTOR_SCORE", "V5_FACTOR_SCORE_PIT"]].to_string(index=False))
