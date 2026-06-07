"""
daily_exit_broadcaster.py
=========================
Sends a Telegram alert ONLY if positions were closed today via stop-loss
or time-stop. Reads from realized_trades.csv and filters to today's exits.

Used as a step inside daily_refresh.yml right after paper_trade_manager
has updated the ledger.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from telegram_broadcaster import send


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not Path(args.realized).exists():
        print("No realized_trades.csv yet — skipping exit broadcast.")
        return
    real = pd.read_csv(args.realized)
    if real.empty:
        print("No realized trades yet — skipping.")
        return

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    real["exit_date"] = real["exit_date"].astype(str)
    todays_exits = real[real["exit_date"] == today]
    if todays_exits.empty:
        print(f"No exits on {today} — skipping broadcast.")
        return

    lines = [f"*⚠️ EXIT SIGNAL — {pd.Timestamp.now().strftime('%a %d %b %Y')}*", ""]
    total_pnl = 0.0
    for r in todays_exits.itertuples():
        sym = r.symbol
        reason = r.exit_reason
        exit_px = r.exit_price
        pnl = r.pnl_inr
        pnl_pct = r.pnl_pct * 100
        days = r.days_held
        emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
        lines.append(
            f"{emoji} `{sym}` — {reason} at ₹{exit_px:.2f}  "
            f"({pnl_pct:+.2f}% / ₹{pnl:+,.0f} / held {days}d)"
        )
        total_pnl += pnl
    lines.append("")
    lines.append(f"*Today's realized:* ₹{total_pnl:+,.0f}  ({len(todays_exits)} positions)")
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
