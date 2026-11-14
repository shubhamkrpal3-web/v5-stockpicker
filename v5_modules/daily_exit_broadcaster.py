"""
daily_exit_broadcaster.py
=========================
Preview only: prints the exit report the next nightly status would send (exit_report.py, the one
exit-reporting function). It never sends anything and never writes the report marker, so the
nightly status (daily_status_broadcaster.py, V5 Daily Data Refresh) stays the only sender of exit
details and the same exits are never sent twice.

Not scheduled by any workflow. (An earlier version described itself as a step inside
daily_refresh.yml, but no workflow ever ran it; it filtered exits by the wall-clock date.)
--dry-run is accepted for compatibility; it changes nothing.
"""
from __future__ import annotations

import argparse

import exit_report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--exit-marker", default=None, help="last_exit_report.txt (default: next to --positions)")
    ap.add_argument("--dry-run", action="store_true", help="accepted for compatibility; this tool never sends")
    args = ap.parse_args()

    marker = args.exit_marker or exit_report.marker_path(args.positions)
    rep = exit_report.collect_paths(args.realized, args.positions, args.equity_log, marker)
    if rep is None:
        print("No ledger runs yet — nothing to report.")
    elif rep["window"]["already_reported"]:
        print(f"Ledger run {rep['window']['end']} already reported by the nightly status.")
    else:
        print(exit_report.build_message(rep) or
              "No exits, cash settlements or newly stale/locked holdings in the unreported runs.")
    print("(preview only - nothing sent, nothing recorded)")


if __name__ == "__main__":
    main()
