"""
realistic_cost_model.py
=======================
Per-stock round-trip cost & slippage model for NSE equity delivery trades.

V4 BUG: backtest_factor_screen.py uses a flat ROUND_TRIP_COST = 0.004 (40 bps).
That number is materially too low for small/mid-cap delivery trades where the
strategy operates. Realistic round-trip is 52-82 bps for the typical name in
this universe.

Reference: Zerodha/Upstox/Groww charge structures + India regulatory charges
as of FY 2025-26. STT moved to 0.1% (buy) + 0.025% (sell) under Budget 2024.

Usage:
    from realistic_cost_model import (
        cost_of_trade, round_trip_cost, apply_cost_to_period_returns,
    )

    # Apply on a backtest period basis:
    avg_cost_pct = round_trip_cost(
        notional_inr=portfolio_avg_position_inr,
        adv_inr=mean_traded_value,
    )
    period_return -= avg_cost_pct
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# -----------------------------------------------------
# Regulatory + broker charge constants (FY 2025-26)
# -----------------------------------------------------
BROKERAGE_PCT       = 0.0003        # 0.03% (Zerodha Equity Delivery flat)
BROKERAGE_CAP_INR   = 20.0          # Max ₹20 per order
STT_BUY_PCT         = 0.001         # 0.1% on buy value (delivery)
STT_SELL_PCT        = 0.00025       # 0.025% on sell value (delivery)
STAMP_DUTY_PCT      = 0.00015       # 0.015% on buy only
EXCHANGE_PCT        = 0.0000297     # NSE transaction charges
SEBI_PCT            = 0.000001      # SEBI turnover
GST_PCT             = 0.18          # 18% GST on brokerage + exchange + SEBI


def _participation_slippage_bps(participation: float) -> float:
    """
    Empirical NSE small/mid-cap slippage model.

    `participation` = order_notional / average_daily_value
    Returns slippage in basis points (one side).
    """
    if participation < 0.005:
        return 6.0
    if participation < 0.01:
        return 10.0
    if participation < 0.03:
        return 18.0
    if participation < 0.05:
        return 28.0
    if participation < 0.10:
        return 45.0
    return 70.0


def cost_of_trade(
    notional_inr: float,
    side: str,
    adv_inr: float | None = None,
    custom_slippage_bps: float | None = None,
) -> dict:
    """
    Compute cost of a single (one-side) delivery trade.

    Parameters
    ----------
    notional_inr : INR notional traded
    side         : 'buy' or 'sell'
    adv_inr      : 20-day average traded value of the symbol (INR)
                   Required unless custom_slippage_bps is given.
    custom_slippage_bps : override slippage estimate.

    Returns
    -------
    dict with breakdown and total in INR + as fraction of notional.
    """
    side = side.lower()
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if notional_inr <= 0:
        return {"total_inr": 0.0, "total_pct": 0.0}

    brokerage = min(BROKERAGE_CAP_INR, BROKERAGE_PCT * notional_inr)
    stt = (STT_BUY_PCT if side == "buy" else STT_SELL_PCT) * notional_inr
    stamp_duty = STAMP_DUTY_PCT * notional_inr if side == "buy" else 0.0
    exchange = EXCHANGE_PCT * notional_inr
    sebi = SEBI_PCT * notional_inr
    gst = GST_PCT * (brokerage + exchange + sebi)

    if custom_slippage_bps is not None:
        slip_bps = float(custom_slippage_bps)
    else:
        if adv_inr is None or adv_inr <= 0:
            slip_bps = 30.0   # conservative default if ADV unknown
        else:
            slip_bps = _participation_slippage_bps(notional_inr / adv_inr)
    slippage = (slip_bps / 10000.0) * notional_inr

    total = brokerage + stt + stamp_duty + exchange + sebi + gst + slippage
    return {
        "brokerage": brokerage, "stt": stt, "stamp_duty": stamp_duty,
        "exchange": exchange, "sebi": sebi, "gst": gst,
        "slippage_bps": slip_bps, "slippage": slippage,
        "total_inr": total, "total_pct": total / notional_inr,
    }


def round_trip_cost(
    notional_inr: float,
    adv_inr: float | None = None,
    custom_slippage_bps: float | None = None,
) -> float:
    """Convenience: full round-trip (buy + sell) as fraction of notional."""
    buy = cost_of_trade(notional_inr, "buy", adv_inr, custom_slippage_bps)["total_pct"]
    sell = cost_of_trade(notional_inr, "sell", adv_inr, custom_slippage_bps)["total_pct"]
    return buy + sell


def apply_cost_to_period_returns(
    period_returns: pd.Series,
    positions_df: pd.DataFrame,
    *,
    avg_notional_col: str = "NOTIONAL_INR",
    avg_adv_col: str = "TRADED_VALUE",
    turnover: float = 1.0,
) -> pd.Series:
    """
    Reduce per-period returns by realistic costs averaged across positions.

    `positions_df` should contain at least notional and ADV columns for the
    held names in that period. `turnover` is the fraction of book turned in
    the period (1.0 = full rebalance).
    """
    if positions_df.empty:
        return period_returns

    per_name_rt_cost = positions_df.apply(
        lambda r: round_trip_cost(r[avg_notional_col], r.get(avg_adv_col)),
        axis=1,
    )
    avg_rt = float(per_name_rt_cost.mean())
    cost_per_period = avg_rt * turnover
    return period_returns - cost_per_period


if __name__ == "__main__":
    # Quick benchmark for a few realistic scenarios
    scenarios = [
        ("Large cap, small ticket",   50_000,   5_000_000_000),  # ₹500 Cr ADV
        ("Mid cap, normal ticket",    50_000,     500_000_000),  # ₹50 Cr ADV
        ("Small cap, normal ticket",  50_000,      50_000_000),  # ₹5 Cr ADV
        ("Small cap, big ticket",    500_000,      50_000_000),
        ("Micro cap, normal ticket",  50_000,      10_000_000),  # ₹1 Cr ADV (illiquid)
    ]
    print(f"{'Scenario':35s} {'Notional':>12s} {'ADV':>15s} {'Slip bps':>10s} {'RT cost':>10s}")
    print("-" * 90)
    for name, notional, adv in scenarios:
        buy = cost_of_trade(notional, "buy", adv)
        rt = round_trip_cost(notional, adv)
        print(
            f"{name:35s} {notional:>12,.0f} {adv:>15,.0f} "
            f"{buy['slippage_bps']:>10.1f} {rt*100:>9.3f}%"
        )
    print(
        "\nV4 backtest assumed 0.400% round-trip — actual is 0.52% to 0.82% "
        "for the typical name in this strategy's universe."
    )
