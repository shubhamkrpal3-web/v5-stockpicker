"""
daily_status_broadcaster.py
===========================
Sends a Telegram message after every daily refresh — even if nothing exciting
happened. Replaces the silent daily_exit_broadcaster behavior with a heartbeat
so you always know the system is alive.

Three message variants:
  - With exits today: detailed exit alerts + P&L summary
  - No exits today: short heartbeat with open count + NAV + P&L
  - Empty ledger (haven't paper-traded yet): heartbeat with "no positions" note
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from telegram_broadcaster import send


def _fmt_inr(v: float) -> str:
    if abs(v) >= 10_000_000:
        return f"₹{v / 10_000_000:.2f} Cr"
    if abs(v) >= 100_000:
        return f"₹{v / 100_000:.2f} L"
    return f"₹{v:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--regime-json", default="./data/live_signals/daily_regime.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = pd.Timestamp.now()
    today_str = today.strftime("%a %d %b %Y")
    today_date_iso = today.strftime("%Y-%m-%d")

    # Detect today's exits from realized_trades.csv
    todays_exits = pd.DataFrame()
    if Path(args.realized).exists():
        real = pd.read_csv(args.realized)
        if not real.empty:
            real["exit_date"] = real["exit_date"].astype(str)
            todays_exits = real[real["exit_date"] == today_date_iso]

    # Load equity & positions
    nav = day_pnl = realized_total = invested_capital = 0.0
    n_open = 0
    open_symbols = []
    if Path(args.equity_log).exists():
        eq = pd.read_csv(args.equity_log)
        if not eq.empty:
            eq["DATE"] = pd.to_datetime(eq["DATE"])
            eq = eq.sort_values("DATE")
            nav = float(eq["EQUITY"].iloc[-1])
            invested_capital = float(eq["INVESTED"].iloc[-1])
            realized_total = float(eq["REALIZED_PNL"].iloc[-1])
            if len(eq) >= 2:
                day_pnl = float(eq["EQUITY"].iloc[-1] - eq["EQUITY"].iloc[-2])
            n_open = int(eq["N_OPEN"].iloc[-1])
    if Path(args.positions).exists():
        pos = pd.read_csv(args.positions)
        if not pos.empty:
            opens = pos[pos["status"] == "open"]
            open_symbols = opens["symbol"].astype(str).head(6).tolist()
            n_open = len(opens)

    # Load regime
    regime_state = "—"
    if Path(args.regime_json).exists():
        with open(args.regime_json) as f:
            rj = json.load(f)
        regime_state = rj.get("state", "—")

    # Build message
    lines = []

    if not todays_exits.empty:
        # Detailed exit alert
        lines.append(f"*⚠️ V5 EXITS TODAY — {today_str}*")
        lines.append("")
        total_pnl_today = 0.0
        for r in todays_exits.itertuples():
            sym = r.symbol
            reason = r.exit_reason
            exit_px = float(r.exit_price)
            pnl = float(r.pnl_inr)
            pnl_pct = float(r.pnl_pct) * 100
            days = int(r.days_held) if r.days_held else 0
            emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
            lines.append(
                f"{emoji} `{sym}` — {reason} at ₹{exit_px:.2f}  "
                f"({pnl_pct:+.2f}% / ₹{pnl:+,.0f} / {days}d)"
            )
            total_pnl_today += pnl
        lines.append("")
        lines.append(f"_Today's realized:_ ₹{total_pnl_today:+,.0f}  ({len(todays_exits)} closed)")
        lines.append("")
        lines.append(f"_Open:_ {n_open}  |  _NAV:_ {_fmt_inr(nav)}  |  _Regime:_ `{regime_state}`")
    elif n_open == 0 and nav == 0:
        # Pre-paper-trading heartbeat (no positions yet)
        lines.append(f"✓ *V5 daily refresh OK* — {today_str}")
        lines.append("")
        lines.append(f"_Status:_ Paper trading hasn't started — no positions opened yet.")
        lines.append(f"_Regime:_ `{regime_state}`")
        lines.append("")
        lines.append(f"_First positions open the Monday after a signal is generated._")
    else:
        # Normal heartbeat: no exits, but positions exist
        symbols_preview = ", ".join(open_symbols) + (f" + {n_open - 6} more" if n_open > 6 else "")
        lines.append(f"✓ *V5 daily refresh OK* — {today_str}")
        lines.append("")
        lines.append(f"_Open:_ {n_open}   _NAV:_ {_fmt_inr(nav)}   _Day P&L:_ ₹{day_pnl:+,.0f}")
        lines.append(f"_Total realized:_ ₹{realized_total:+,.0f}   _Regime:_ `{regime_state}`")
        if symbols_preview:
            lines.append("")
            lines.append(f"_Holdings:_ {symbols_preview}")
        lines.append("")
        lines.append(f"_No stops fired today._")

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
