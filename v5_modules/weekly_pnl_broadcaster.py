"""
weekly_pnl_broadcaster.py
=========================
Saturday morning summary built from live ledger files (no manual entry).

Reads:
  equity_log.csv       - daily NAV from paper_trade_manager
  positions.csv        - open + closed positions
  realized_trades.csv  - completed trade log
  daily_regime.json    - current regime state

Sends a Telegram message with:
  Weekly P&L %, YTD P&L %, drawdown from peak
  Open positions count + total MTM
  Realized trades this week (n wins / n losses)
  Win rate (rolling all-time + last 12 weeks)
  Current regime
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

import pandas as pd

from telegram_broadcaster import send


def fmt_inr(v: float) -> str:
    if abs(v) >= 10_000_000:
        return f"₹{v / 10_000_000:.2f} Cr"
    if abs(v) >= 100_000:
        return f"₹{v / 100_000:.2f} L"
    return f"₹{v:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--regime-json", default="./data/live_signals/daily_regime.json")
    ap.add_argument("--benchmark-json", default="./data/live_signals/benchmark_summary.json")
    ap.add_argument("--portfolio-inr", type=float, default=200000.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = pd.Timestamp.now().strftime("%a %d %b %Y")

    # Equity log
    if not Path(args.equity_log).exists():
        msg = (
            f"*V5 WEEKLY SUMMARY — {today}*\n\n"
            "No paper trade activity yet — the ledger is empty.\n"
            "First positions open the next Monday after a signal is generated."
        )
    else:
        eq = pd.read_csv(args.equity_log)
        eq["DATE"] = pd.to_datetime(eq["DATE"])
        eq = eq.sort_values("DATE")

        # Weekly P&L: last 5 trading days
        week_eq = eq.tail(5)
        if len(week_eq) >= 2:
            week_ret = week_eq["EQUITY"].iloc[-1] / week_eq["EQUITY"].iloc[0] - 1
        else:
            week_ret = 0.0

        # YTD
        ytd_eq = eq[eq["DATE"].dt.year == pd.Timestamp.now().year]
        if len(ytd_eq) >= 2:
            ytd_ret = ytd_eq["EQUITY"].iloc[-1] / ytd_eq["EQUITY"].iloc[0] - 1
        else:
            ytd_ret = 0.0

        # Drawdown from peak
        peak = eq["EQUITY"].cummax().iloc[-1]
        dd = eq["EQUITY"].iloc[-1] / peak - 1
        latest_equity = float(eq["EQUITY"].iloc[-1])

        # Position counts
        if Path(args.positions).exists():
            pos = pd.read_csv(args.positions)
            open_pos = pos[pos["status"] == "open"]
            n_open = len(open_pos)
            top_symbols = ", ".join(open_pos["symbol"].head(8).tolist()) if n_open else "—"
        else:
            n_open = 0
            top_symbols = "—"

        # Realized trades this week
        wins = losses = n_trades_week = 0
        recent_pnl = 0.0
        if Path(args.realized).exists():
            real = pd.read_csv(args.realized)
            if len(real):
                real["exit_date"] = pd.to_datetime(real["exit_date"], errors="coerce")
                this_week = real[real["exit_date"] >= eq["DATE"].iloc[-1] - pd.Timedelta(days=7)]
                n_trades_week = len(this_week)
                wins = int((this_week["pnl_inr"] > 0).sum())
                losses = int((this_week["pnl_inr"] < 0).sum())
                recent_pnl = float(this_week["pnl_inr"].sum())

                # All-time win rate (closed trades)
                n_total = len(real)
                if n_total:
                    win_rate_alltime = (real["pnl_inr"] > 0).sum() / n_total
                else:
                    win_rate_alltime = 0
            else:
                win_rate_alltime = 0
        else:
            win_rate_alltime = 0

        # Regime
        regime_state = "UNKNOWN"
        if Path(args.regime_json).exists():
            with open(args.regime_json) as f:
                rj = json.load(f)
            regime_state = rj.get("state", "UNKNOWN")

        # Benchmark: vs Nifty 500 (the go-live test)
        bench_line = None
        if Path(args.benchmark_json).exists():
            try:
                with open(args.benchmark_json) as f:
                    b = json.load(f)
                if b.get("status") == "ok":
                    verdict = "ahead" if b.get("beating_market") else "behind"
                    ir = b.get("information_ratio")
                    ir_str = f"{ir:+.2f}" if isinstance(ir, (int, float)) else "n/a"
                    bench_line = (
                        f"_vs Nifty 500:_ you {b['portfolio_return_pct']:+.2f}% vs "
                        f"market {b['benchmark_return_pct']:+.2f}% → *{verdict} {abs(b['excess_return_pct']):.2f}%*"
                        f"  (IR {ir_str})"
                    )
            except Exception:
                bench_line = None

        lines = []
        lines.append(f"*V5 WEEKLY SUMMARY — {today}*")
        lines.append("")
        lines.append(f"_Equity:_ {fmt_inr(latest_equity)}  ({week_ret * 100:+.2f}% this wk)")
        lines.append(f"_YTD:_ {ytd_ret * 100:+.2f}%   _DD from peak:_ {dd * 100:+.2f}%")
        lines.append(f"_Regime:_ `{regime_state}`")
        if bench_line:
            lines.append(bench_line)
        lines.append("")
        lines.append(f"*Open positions:* {n_open}")
        if top_symbols != "—":
            lines.append(f"_{top_symbols}_")
        lines.append("")
        lines.append(f"*Closed this week:* {n_trades_week} ({wins}W / {losses}L)   P&L {fmt_inr(recent_pnl)}")
        if Path(args.realized).exists() and len(pd.read_csv(args.realized)):
            lines.append(f"_All-time win rate:_ {win_rate_alltime * 100:.1f}%")
        lines.append("")
        lines.append("_Auto-tracked. Update positions.csv if you skipped any picks._")
        msg = "\n".join(lines)

    if args.dry_run:
        print(msg)
        return

    resp = send(msg)
    if resp.get("ok"):
        print("Sent OK.")
    else:
        print("FAILED:", resp)


if __name__ == "__main__":
    main()
