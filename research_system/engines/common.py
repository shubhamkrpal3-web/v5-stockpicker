"""Helpers shared by the engines: setup statistics, evidence, outcome caps."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research_system.candidate import Evidence
from research_system.confidence import pct_score


def setup_stats(ctx, setup: str) -> dict:
    return (ctx.registry.get(setup) or {})


def historical_component(stats: dict) -> tuple[float | None, str]:
    s = stats.get("stats") or {}
    if not s:
        return None, "no historical record for this setup"
    t = s.get("t_stat_ret")
    return pct_score(t, 0.0, 4.0), (f"{stats.get('status')}: n={s.get('n')} trades {s.get('period')}, "
                                    f"mean {s.get('mean_ret', 0):+.2%}/trade, win {s.get('win_rate', 0):.0%}, "
                                    f"t={t:.1f}")


def regime_component(stats: dict, regime: str) -> tuple[float | None, str]:
    br = (stats.get("by_regime") or {}).get(regime)
    if not br or br.get("n", 0) < 30:
        return None, f"too few historical trades entered in {regime}"
    return pct_score(br.get("t_stat_ret"), -1.0, 3.0), (
        f"{regime}: n={br['n']}, mean {br['mean_ret']:+.2%}/trade, win {br['win_rate']:.0%}, t={br['t_stat_ret']:.1f}")


def liquidity_component(adv_inr: float) -> tuple[float, str]:
    if not adv_inr or adv_inr <= 0:
        return 0.0, "no turnover history"
    return pct_score(math.log10(adv_inr), 7.0, 9.5), f"20d ADV Rs{adv_inr / 1e7:,.1f} Cr"


def freshness_component(ctx, *datasets) -> tuple[float, str]:
    d = ctx.quality.get("datasets", {})
    bad = [f"{k}:{d.get(k, {}).get('status', 'MISSING')}" for k in datasets
           if d.get(k, {}).get("status") != "FRESH"]
    return (1.0, "inputs current") if not bad else (0.2, "stale/missing: " + ", ".join(bad))


def outcome_cap(stats: dict) -> list:
    """No out-of-sample (live) record yet -> grade capped at C."""
    if stats.get("oos", {}).get("n", 0) >= 100:
        return []
    return [("C", "no out-of-sample record yet (backtest only)")]


def sector_evidence(ctx, sector: str) -> tuple[Evidence | None, dict]:
    sec = (ctx.ms.get("sectors") or {}).get(sector)
    if not sec:
        return None, {}
    rs = sec.get("rs_vs_n500_63d")
    txt = (f"{sector}: 63d return vs Nifty 500 {rs:+.1%} (rank {sec.get('rs_rank')}), "
           f"{'above' if sec.get('above_200dma') else 'below'} its 200-day average, "
           f"{sec.get('breadth_above_50dma', 0):.0%} of members above 50-day average")
    return Evidence("sector", txt, "market_state.sectors", ctx.ms.get("as_of", "")), sec


def macro_evidence(ctx, sector: str) -> list:
    out = []
    betas = (ctx.ms.get("sector_macro_betas") or {}).get(sector) or {}
    for f, b in betas.items():
        out.append(Evidence("macro", f"{sector} partial beta to {f}: {b:+.3f} (252d, controls for Nifty 500, |t|>=2)",
                            "market_state.sector_macro_betas", ctx.ms.get("as_of", "")))
    b = ctx.ms.get("broad") or {}
    if b:
        out.append(Evidence("macro", f"market regime {b.get('regime')}, risk appetite z {b.get('risk_appetite_z')}, "
                                     f"breadth {b.get('breadth_above_50dma', 0):.0%}",
                            "market_state.broad", ctx.ms.get("as_of", "")))
    return out


def upcoming_events(ctx, symbol: str, days: int = 45) -> pd.DataFrame:
    cal = ctx.calendar
    if cal is None or cal.empty:
        return pd.DataFrame()
    end = (ctx.asof + pd.Timedelta(days=days)).strftime("%Y%m%d")
    return cal[(cal["symbol"] == symbol) & (cal["event_date"] >= ctx.asof.strftime("%Y%m%d")) &
               (cal["event_date"] <= end)]


def sessions_ahead(asof: pd.Timestamp, n: int) -> str:
    """Approximate calendar date n trading sessions ahead (weekdays)."""
    return (asof + pd.offsets.BDay(n)).strftime("%Y%m%d")
