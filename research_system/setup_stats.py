"""Build the setup registry from a backtest run (and, later, live outcomes).

    python -m research_system.setup_stats --setup momentum_lowvol_rank \
        --trades backtests/registry/trades_fixed.csv

The registry records, per setup: status, overall per-trade statistics, the
split by market regime at entry, an early/late split as a stability check,
and the out-of-sample (live) record once the audit log has outcomes.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research_system import paths


def summarize(t: pd.DataFrame) -> dict:
    r = t["ret"].astype(float)
    n = len(r)
    if n == 0:
        return {"n": 0}
    sd = r.std(ddof=1) if n > 1 else float("nan")
    return {"n": int(n),
            "period": f"{t['entry_date'].min()}..{t['exit_date'].max()}",
            "win_rate": round(float((r > 0).mean()), 4),
            "mean_ret": round(float(r.mean()), 5), "median_ret": round(float(r.median()), 5),
            "t_stat_ret": round(float(r.mean() / (sd / math.sqrt(n))), 2) if n > 1 and sd > 0 else None,
            "mean_r": round(float(t["r_mult"].mean()), 3) if "r_mult" in t else None,
            "median_days": float(t["days"].median()), "p90_days": float(t["days"].quantile(0.9))}


def build(setup: str, trades_csv: str, description: str, caveats: list) -> dict:
    t = pd.read_csv(trades_csv)
    # returns are price-based; charges at 35 bps per side were applied to P&L in the backtest
    t["ret"] = t["ret"] - 0.007
    mid = t["entry_date"].sort_values().iloc[len(t) // 2]
    rec = {
        "status": "backtest_only",
        "description": description,
        "source": trades_csv,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": summarize(t),
        "by_regime": {k: summarize(g) for k, g in t.groupby("regime")},
        "first_half": summarize(t[t["entry_date"] < mid]),
        "second_half": summarize(t[t["entry_date"] >= mid]),
        "oos": {"n": 0, "note": "filled from the audit log once live outcomes accumulate"},
        "caveats": caveats,
    }
    reg = json.loads(paths.SETUP_REGISTRY.read_text()) if paths.SETUP_REGISTRY.exists() else {}
    reg[setup] = rec
    paths.SETUP_REGISTRY.write_text(json.dumps(reg, indent=2))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", default="momentum_lowvol_rank")
    ap.add_argument("--trades", required=True)
    a = ap.parse_args()
    rec = build(a.setup, a.trades,
                "62.5% skip-month momentum (3-1, 6-1) + 37.5% low 90d volatility; top 15 liquid NSE names; "
                "weekly entry at next open; stop 2 ATR; chandelier 4 ATR; regime-scaled gross (V5)",
                ["Backtest on split-adjusted NSE EOD data, 35 bps per side charges; not out-of-sample.",
                 "The breadth sleeve was removed after testing on overlapping 2017-2026 data.",
                 "No quality gate in the backtest; the gate is reported as evidence, not applied.",
                 "Stops filled at the stop price; gaps through the stop are understated."])
    print(json.dumps({k: rec[k] for k in ("stats", "first_half", "second_half")}, indent=2))
    print({k: (v["n"], v.get("mean_ret"), v.get("t_stat_ret")) for k, v in rec["by_regime"].items()})


if __name__ == "__main__":
    main()
