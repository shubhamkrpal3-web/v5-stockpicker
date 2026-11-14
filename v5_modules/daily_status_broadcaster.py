"""
daily_status_broadcaster.py
===========================
Sends a Telegram message after every daily refresh — even if nothing exciting
happened. Replaces the silent daily_exit_broadcaster behavior with a heartbeat
so you always know the system is alive.

Three message variants:
  - Exits, cash settlements or newly stale/locked holdings in the ledger sessions not yet
    reported (catch-up included): exit_report.py, the one exit-reporting function
  - Nothing new: short heartbeat with open count + NAV + P&L
  - Empty ledger (haven't paper-traded yet): heartbeat with "no positions" note
After a message is sent, the ledger run is recorded in last_exit_report.txt (committed with the
other data by the refresh workflow), so the Exit Check job does not repeat it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from telegram_broadcaster import send
import exit_report
import ledger_view


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
    ap.add_argument("--exit-marker", default=None, help="last_exit_report.txt (default: next to --positions)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = pd.Timestamp.now()
    today_str = today.strftime("%a %d %b %Y")

    # Exits, cash settlements and newly stale/locked holdings for every ledger session not yet
    # reported (catch-up included) - the one exit-reporting function, exit_report.py
    marker = args.exit_marker or exit_report.marker_path(args.positions)
    rep = exit_report.collect_paths(args.realized, args.positions, args.equity_log, marker)
    has_events = bool(rep and rep.get("has_events"))

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
    other_lines = []                       # stale, locked, pending, cash owed: active but not open
    if Path(args.positions).exists():
        pos = pd.read_csv(args.positions)
        if not pos.empty:
            rows = ledger_view.classify(pos)
            opens = rows[rows.state == ledger_view.OPEN]
            open_symbols = opens["symbol"].astype(str).head(6).tolist()
            n_open = len(opens)
            other_lines = ledger_view.holdings_line(rows[rows.state != ledger_view.OPEN])

    # Load regime
    regime_state = "—"
    if Path(args.regime_json).exists():
        with open(args.regime_json) as f:
            rj = json.load(f)
        regime_state = rj.get("state", "—")

    # Build message
    lines = []

    if has_events:
        # Exit alert: trade exits, cash settlements, holdings needing attention
        lines.append(f"*⚠️ V5 PAPER EXITS — {today_str}*")
        lines.append("")
        lines += exit_report.lines(rep)
        lines.append(f"_Open:_ {n_open}  |  _NAV:_ {_fmt_inr(nav)}  |  _Regime:_ `{regime_state}`")
        if other_lines:
            lines.append("_Also active (not open):_")
            lines += [f"• {line}" for line in other_lines]
    elif n_open == 0 and nav == 0 and not other_lines:
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
        if other_lines:
            lines.append("_Also active (not open):_")
            lines += [f"• {line}" for line in other_lines]
        lines.append("")
        if rep and not rep["window"]["already_reported"]:
            lines.append(f"_No exits, cash settlements or newly stale/locked holdings ({exit_report.coverage(rep)})._")
        else:
            lines.append("_No new ledger session since the last report._")

    msg = "\n".join(lines)

    if args.dry_run:
        print(msg)
        return
    resp = send(msg)
    if resp.get("ok"):
        print("Sent OK.")
    else:
        print("FAILED:", resp)
    # sent: the run is reported; not sent: the marker keeps (or records) where reporting stopped, so the
    # next nightly report covers these sessions. The refresh workflow commits data/, persisting it.
    exit_report.record(marker, rep, bool(resp.get("ok")))


if __name__ == "__main__":
    main()
