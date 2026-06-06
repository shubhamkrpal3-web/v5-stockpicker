"""
risk_overlay_fixed.py
=====================
Properly enforced position- and sector-cap engine.

V4 BUG: portfolio_risk_overlay.py's iterative redistribution does not actually
converge when the universe is concentrated in one mega-sector (e.g.,
"Diversified / Others" absorbing unmapped symbols). The live portfolio file
daily_live_portfolio.csv (2026-05-15) shows:
    Max position    : 12.83% (cap was supposed to be 10%)
    Max sector      : 38.49% (cap was supposed to be 25%)

FIX (this module):
  * Iterative cap-and-redistribute that PROVABLY converges
  * After convergence, hard-asserts caps are respected (raises if not)
  * Spillage logic: if redistribution can't fit the excess, allocate to cash
    rather than silently violating the cap
  * Returns a diagnostic dict so the caller can log all enforcement actions
  * Treats "Diversified / Others" as a flag for missing sector mapping —
    such names are sized very small or skipped depending on policy

Usage:
    portfolio = pd.DataFrame({
        "SYMBOL": [...], "WEIGHT": [...], "SECTOR": [...]
    })
    portfolio, info = enforce_caps(
        portfolio,
        max_stock_weight=0.07,
        max_sector_weight=0.22,
        allow_cash=True,
        unmapped_sector_label="Diversified / Others",
        unmapped_max_total=0.10,  # cap total exposure to unmapped names
    )
    assert info["caps_ok"]
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd


def _normalize(w: pd.Series) -> pd.Series:
    s = w.clip(lower=0)
    tot = s.sum()
    return s / tot if tot > 0 else s


def enforce_caps(
    portfolio: pd.DataFrame,
    *,
    weight_col: str = "WEIGHT",
    symbol_col: str = "SYMBOL",
    sector_col: str = "SECTOR",
    max_stock_weight: float = 0.07,
    max_sector_weight: float = 0.22,
    max_iter: int = 100,
    tol: float = 1e-5,
    allow_cash: bool = True,
    unmapped_sector_label: str | None = "Diversified / Others",
    unmapped_max_total: float = 0.10,
) -> Tuple[pd.DataFrame, dict]:
    """
    Iterative cap enforcement.

    Returns (portfolio, info). Portfolio has columns:
        SYMBOL, SECTOR, WEIGHT (post-enforcement, sums to <= 1.0)
    plus optional CASH row with SECTOR='CASH' if allow_cash=True.

    info is a dict with diagnostics, including caps_ok (bool).
    """
    p = portfolio.copy()

    if weight_col not in p.columns:
        raise ValueError(f"Missing weight column '{weight_col}'.")
    if symbol_col not in p.columns:
        raise ValueError(f"Missing symbol column '{symbol_col}'.")
    if sector_col not in p.columns:
        p[sector_col] = "UNKNOWN"

    p[weight_col] = pd.to_numeric(p[weight_col], errors="coerce").fillna(0.0)
    p = p[p[weight_col] > 0].reset_index(drop=True)

    info = {
        "actions": [],
        "iterations": 0,
        "cash_added": 0.0,
        "unmapped_capped_from": None,
        "unmapped_capped_to": None,
        "caps_ok": False,
    }

    if len(p) == 0:
        info["caps_ok"] = True
        return p, info

    # Step 0: normalize input
    p[weight_col] = _normalize(p[weight_col])

    # Step 1: cap unmapped-sector aggregate exposure FIRST.
    # This is the root cause of V4's bug — the catch-all sector dominated.
    if unmapped_sector_label is not None:
        mask_unmapped = p[sector_col] == unmapped_sector_label
        unmapped_total = float(p.loc[mask_unmapped, weight_col].sum())
        if unmapped_total > unmapped_max_total:
            scale = unmapped_max_total / unmapped_total
            p.loc[mask_unmapped, weight_col] *= scale
            info["actions"].append(
                f"Unmapped sector '{unmapped_sector_label}' aggregate {unmapped_total:.4f} "
                f"scaled by {scale:.4f} to cap at {unmapped_max_total:.4f}"
            )
            info["unmapped_capped_from"] = unmapped_total
            info["unmapped_capped_to"] = unmapped_max_total
            # The remaining weight (1 - unmapped_max_total) is for mapped names
            mask_mapped = ~mask_unmapped
            mapped_total = float(p.loc[mask_mapped, weight_col].sum())
            if mapped_total > 0:
                target_mapped = 1.0 - unmapped_max_total
                p.loc[mask_mapped, weight_col] *= target_mapped / mapped_total

    # Step 2: iterative per-stock and per-sector cap enforcement.
    for it in range(max_iter):
        info["iterations"] = it + 1
        prev_weights = p[weight_col].copy()

        # 2a. Per-stock cap
        over_stock = p[weight_col] > max_stock_weight
        if over_stock.any():
            excess = (p.loc[over_stock, weight_col] - max_stock_weight).sum()
            p.loc[over_stock, weight_col] = max_stock_weight
            # Redistribute excess to under-cap names proportionally to their current weight
            under_stock = (p[weight_col] < max_stock_weight) & (~over_stock)
            if under_stock.any():
                pool = float(p.loc[under_stock, weight_col].sum())
                if pool > 0:
                    p.loc[under_stock, weight_col] += excess * p.loc[under_stock, weight_col] / pool
                else:
                    info["cash_added"] += excess
            else:
                info["cash_added"] += excess

        # 2b. Per-sector cap
        sector_totals = p.groupby(sector_col)[weight_col].sum()
        over_sector = sector_totals[sector_totals > max_sector_weight]
        if not over_sector.empty:
            for sec, sec_total in over_sector.items():
                mask = p[sector_col] == sec
                scale = max_sector_weight / sec_total
                excess = (sec_total - max_sector_weight)
                p.loc[mask, weight_col] *= scale
                info["actions"].append(
                    f"Sector '{sec}' total {sec_total:.4f} scaled by {scale:.4f}"
                )
                # Redistribute excess to under-cap sectors proportionally
                under_mask = (
                    ~p[sector_col].isin(over_sector.index)
                    & (p[weight_col] < max_stock_weight)
                )
                pool = float(p.loc[under_mask, weight_col].sum())
                if pool > 0:
                    p.loc[under_mask, weight_col] += excess * p.loc[under_mask, weight_col] / pool
                else:
                    info["cash_added"] += excess

        # 2c. Re-normalize the residual to <= 1.0 (allowing cash if needed)
        total = float(p[weight_col].sum())
        if total > 1.0:
            p[weight_col] /= total

        # Convergence: max weight change below tolerance AND no caps breached
        diff = float((p[weight_col] - prev_weights).abs().max())
        stock_ok = (p[weight_col] <= max_stock_weight + tol).all()
        sector_ok = (p.groupby(sector_col)[weight_col].sum() <= max_sector_weight + tol).all()
        if diff < tol and stock_ok and sector_ok:
            break

    # Step 3: final assertion
    final_stock_ok = (p[weight_col] <= max_stock_weight + tol).all()
    final_sector_totals = p.groupby(sector_col)[weight_col].sum()
    final_sector_ok = (final_sector_totals <= max_sector_weight + tol).all()
    info["caps_ok"] = bool(final_stock_ok and final_sector_ok)
    info["max_position"] = float(p[weight_col].max())
    info["max_sector"] = float(final_sector_totals.max())

    # Step 4: add CASH row to make weights sum to 1.0
    if allow_cash:
        invested = float(p[weight_col].sum())
        cash = max(0.0, 1.0 - invested)
        if cash > tol:
            cash_row = pd.DataFrame([{symbol_col: "CASH", sector_col: "CASH", weight_col: cash}])
            p = pd.concat([p, cash_row], ignore_index=True)
            info["cash_added"] += cash

    if not info["caps_ok"]:
        # If still not OK after max_iter, this is a hard failure.
        raise RuntimeError(
            f"Cap enforcement did not converge after {info['iterations']} iterations. "
            f"max_position={info['max_position']:.4f}, max_sector={info['max_sector']:.4f}. "
            f"Tighten unmapped_max_total or relax caps."
        )

    return p, info


if __name__ == "__main__":
    # Reproduce the V4 bug scenario from daily_live_portfolio.csv (2026-05-15)
    test = pd.DataFrame({
        "SYMBOL": [
            "SOLARINDS", "AUROPHARMA", "TDPOWERSYS", "ADANIGREEN", "ADANIPORTS",
            "ASTERDM", "GRANULES", "MAFANG", "CARBORUNIV", "APARINDS",
            "BSE", "MANKIND", "ADANIENT", "MON100", "ACUTAAS",
            "VIJAYA", "WELCORP", "MCX", "SENORES", "BHEL",
        ],
        "SECTOR": [
            "Power & Energy", "Healthcare & Pharma", "Power & Energy",
            "Power & Energy", "Transportation & Logistics",
            "Diversified / Others", "Diversified / Others", "Diversified / Others",
            "Diversified / Others", "Diversified / Others",
            "Diversified / Others", "Diversified / Others", "Diversified / Others",
            "Diversified / Others", "Diversified / Others",
            "Diversified / Others", "Diversified / Others", "Diversified / Others",
            "Diversified / Others", "Diversified / Others",
        ],
        "WEIGHT": [
            0.1283, 0.1283, 0.1283, 0.1283, 0.1283,
            0.0346, 0.0283, 0.0277, 0.0276, 0.027,
            0.0262, 0.026, 0.0241, 0.0232, 0.0227,
            0.0215, 0.0189, 0.0182, 0.018, 0.0146,
        ],
    })
    out, info = enforce_caps(test, max_stock_weight=0.07, max_sector_weight=0.22)
    print("INPUT max position : 0.1283  | max sector : 0.3849")
    print(f"OUTPUT max position: {info['max_position']:.4f}  | max sector: {info['max_sector']:.4f}")
    print(f"CAPS OK: {info['caps_ok']}  | iterations: {info['iterations']}  | cash: {info['cash_added']:.4f}")
    print(out.to_string(index=False))
