"""
telegram_broadcaster.py
=======================
Telegram broadcaster for V5 signals.

Reads daily_live_portfolio.csv, positions.csv, and regime state, then sends
formatted messages to a Telegram channel. Designed to be called from
GitHub Actions on the cron schedule defined in .github/workflows/.

Four message types:
  1. WEEKLY_SIGNAL  : Mondays pre-market — new entries with conviction + stops
  2. DAILY_EXIT     : trading day evenings — only if exits fired
  3. WEEKLY_SUMMARY : Saturdays — PnL, drawdown, regime
  4. TEST           : sanity check that secrets + bot are wired up

Usage:
    python telegram_broadcaster.py --type weekly_signal
    python telegram_broadcaster.py --type daily_exit
    python telegram_broadcaster.py --type weekly_summary
    python telegram_broadcaster.py --type test

Reads credentials from env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID,
or from a creds file specified by --creds /path/to/telegram_creds.txt
(format: BOT_TOKEN=xxx CHAT_ID=yyy on separate lines).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests


def _load_creds(creds_path: Optional[str] = None) -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if creds_path:
        with open(creds_path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    if k == "BOT_TOKEN":
                        token = v
                    elif k == "CHAT_ID":
                        chat = v
    if not token or not chat:
        raise RuntimeError(
            "Telegram credentials missing. Set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID env vars or use --creds."
        )
    return token, chat


def send(text: str, creds_path: Optional[str] = None, parse_mode: str = "Markdown",
         retries: int = 3, timeout: float = 20) -> dict:
    """Send a message to Telegram. Returns the API response.

    A transport failure (timeout, connection error) or a gateway/rate-limit status (5xx, 429) is retried with a
    short back-off. After the last attempt the failure is RETURNED as {"ok": False, ...} rather than raised, so an
    api.telegram.org outage never fails the run that produced the message: callers log the response, and the
    exit-report marker keeps the unsent report pending for the next run (2026-11-13: a Telegram read timeout
    failed the daily refresh before its data commit). Missing credentials still raise: that is a setup error."""
    token, chat = _load_creds(creds_path)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    last: dict = {"ok": False, "error": "not attempted"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            try:
                resp = r.json()
            except ValueError:
                resp = {"ok": False, "error_code": r.status_code, "description": r.text[:200]}
            if resp.get("ok") or not (r.status_code == 429 or 500 <= r.status_code < 600):
                return resp                      # delivered, or a definite client-side error (no point retrying)
            last = resp
        except requests.RequestException as e:
            last = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if attempt < retries:
            time.sleep(5 * attempt)
    print(f"[WARN] Telegram send failed after {retries} attempts: {last}", file=sys.stderr)
    return last


def _fmt_inr(v: float) -> str:
    if abs(v) >= 10_000_000:
        return f"₹{v / 10_000_000:.2f} Cr"
    if abs(v) >= 100_000:
        return f"₹{v / 100_000:.2f} L"
    return f"₹{v:,.0f}"


def _conviction_band(score: float) -> str:
    if score >= 9.0:
        return "high (1.5R)"
    if score >= 7.0:
        return "baseline (1.0R)"
    if score >= 5.0:
        return "toehold (0.5R)"
    return "skip"


def build_weekly_signal(
    portfolio_path: str,
    positions_path: Optional[str] = None,
    regime_state: str = "RISK_NEU",
    regime_triggers: dict = None,
    regime_json_path: Optional[str] = None,
    ytd_pct: float = 0.0,
    dd_from_peak_pct: float = 0.0,
    portfolio_target: float = 1.0,
) -> str:
    """Build the Monday-morning new-entries/holds/exits broadcast."""
    df = pd.read_csv(portfolio_path)
    df.columns = [c.upper() for c in df.columns]
    today = pd.Timestamp.now().strftime("%a %d %b %Y")

    if regime_json_path and Path(regime_json_path).exists():
        with open(regime_json_path) as f:
            rj = json.load(f)
        regime_state = rj.get("state", regime_state)
        regime_triggers = rj.get("triggers", regime_triggers)
        portfolio_target = {"RISK_ON": 1.0, "RISK_NEU": 0.7, "RISK_OFF": 0.4}.get(
            regime_state, 1.0
        )

    rt = regime_triggers or {"Trend": "?", "Breadth": "?", "VIX": "?"}
    trig = "  ".join(f"{k}{v}" for k, v in rt.items())

    lines = []
    lines.append(f"*V5 SIGNAL — {today}*")
    lines.append(f"_Regime:_ `{regime_state}`  ({trig})")
    lines.append(f"_Gross target:_ {portfolio_target * 100:.0f}%   _Kill switch:_ clear")
    lines.append(f"_Portfolio YTD:_ {ytd_pct * 100:+.2f}%   _DD from peak:_ {dd_from_peak_pct * 100:+.2f}%")
    lines.append("")

    new_entries = df[df.get("ACTION", "ENTER") == "ENTER"] if "ACTION" in df.columns else df
    if len(new_entries):
        lines.append(f"*NEW ENTRIES ({len(new_entries)}):*")
        # Show ALL picks. Telegram allows 4096 chars/msg; 15 picks ~ 1200 chars — well under.
        for i, r in enumerate(new_entries.itertuples(), 1):
            sym = getattr(r, "SYMBOL", "?")
            close = getattr(r, "CLOSE", 0)
            conv = getattr(r, "CONFIDENCE", 7.0)
            stop = getattr(r, "STOP_PRICE", close * 0.97)
            size_inr = getattr(r, "SIZE_INR", 2000)
            band = _conviction_band(conv)
            lines.append(
                f"{i}. `{sym}`  Conv {conv:.1f}  Entry@open ₹{close:.1f}  "
                f"Stop ₹{stop:.1f}  Size {_fmt_inr(size_inr)} ({band})"
            )
        lines.append("")

    if positions_path and Path(positions_path).exists():
        pos = pd.read_csv(positions_path)
        if len(pos):
            import ledger_view                      # every active ledger state, labelled
            held = ledger_view.classify(pos)
            if len(held):
                lines.append(f"*HOLD ({len(held)} active):*")
                lines += [f"• {line}" for line in ledger_view.holdings_line(held)]
                lines.append("")

    lines.append("_Disclaimer: Educational signal. Not investment advice. Execute at your discretion._")
    return "\n".join(lines)


def build_daily_exit(positions_path: str, realized_path: Optional[str] = None,
                     equity_log_path: Optional[str] = None, marker_file: Optional[str] = None) -> Optional[str]:
    """What the V5 Daily Exit Check sends: a 'report not sent' notice (counts only) when the latest
    ledger run has no exit report recorded, else None. Exit details are sent only by the nightly status
    (daily_status_broadcaster.py), whose workflow commits the marker; this job runs on a separate runner
    that cannot persist one, so it never sends exit details and the same exits are never sent twice.
    (The original version looked for statuses the ledger never writes, so it never fired.)"""
    import exit_report
    base = Path(positions_path).parent
    marker_file = marker_file or exit_report.marker_path(positions_path)
    if not Path(marker_file).exists():
        # the nightly status creates the marker on its first run (sent or not); until then there is
        # no record to check against, so stay quiet rather than raise a false alarm
        return None
    rep = exit_report.collect_paths(realized_path or base / "realized_trades.csv", positions_path,
                                    equity_log_path or base / "equity_log.csv", marker_file)
    return exit_report.missing_report_notice(rep)


def build_weekly_summary(
    equity_log_path: str,
    bench_log_path: Optional[str] = None,
    regime_state: str = "RISK_NEU",
    regime_json_path: Optional[str] = None,
) -> str:
    """Saturday morning summary."""
    if regime_json_path and Path(regime_json_path).exists():
        with open(regime_json_path) as f:
            rj = json.load(f)
        regime_state = rj.get("state", regime_state)

    eq = pd.read_csv(equity_log_path)
    eq["DATE"] = pd.to_datetime(eq["DATE"])
    eq = eq.sort_values("DATE")
    today = pd.Timestamp.now().strftime("%a %d %b %Y")
    week_eq = eq.tail(5)
    week_ret = (week_eq["EQUITY"].iloc[-1] / week_eq["EQUITY"].iloc[0] - 1) if len(week_eq) >= 2 else 0
    ytd_eq = eq[eq["DATE"].dt.year == pd.Timestamp.now().year]
    ytd_ret = (ytd_eq["EQUITY"].iloc[-1] / ytd_eq["EQUITY"].iloc[0] - 1) if len(ytd_eq) >= 2 else 0
    peak = eq["EQUITY"].cummax().iloc[-1]
    dd = eq["EQUITY"].iloc[-1] / peak - 1
    lines = [
        f"*V5 WEEKLY SUMMARY — {today}*",
        "",
        f"Portfolio: {week_ret * 100:+.2f}% this wk   YTD {ytd_ret * 100:+.2f}%   DD {dd * 100:+.2f}%",
        f"Regime: {regime_state}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--type",
        required=True,
        choices=["weekly_signal", "daily_exit", "weekly_summary", "test"],
    )
    ap.add_argument("--portfolio", default="./data/live_signals/daily_live_portfolio.csv")
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--realized", default=None,
                    help="realized_trades.csv (default: next to --positions)")
    ap.add_argument("--exit-marker", default=None,
                    help="last_exit_report.txt (default: next to --positions)")
    ap.add_argument(
        "--regime-json",
        default="./data/live_signals/daily_regime.json",
        help="Path to daily_regime.json (preferred). If provided and exists, overrides --regime.",
    )
    ap.add_argument("--regime", default="RISK_NEU")
    ap.add_argument("--ytd", type=float, default=0.0)
    ap.add_argument("--dd", type=float, default=0.0)
    ap.add_argument(
        "--creds",
        default=None,
        help="Path to telegram_creds.txt (BOT_TOKEN=xxx CHAT_ID=yyy)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.type == "test":
        msg = (
            f"*V5 system test* — {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M UTC')}\n"
            "If you see this, the GitHub Actions → Telegram pipeline works.\n"
            "_Sent from your V5 automation._"
        )
    elif args.type == "weekly_signal":
        msg = build_weekly_signal(
            portfolio_path=args.portfolio,
            positions_path=args.positions,
            regime_state=args.regime,
            regime_json_path=args.regime_json,
            ytd_pct=args.ytd,
            dd_from_peak_pct=args.dd,
        )
    elif args.type == "daily_exit":
        # V5 Daily Exit Check: never sends exit details and never writes the marker (see build_daily_exit)
        msg = build_daily_exit(args.positions, args.realized, args.equity_log, args.exit_marker)
        if not msg:
            print("Nothing to send: the latest ledger run is already reported, or the nightly status has not "
                  "created its report marker yet, or there is no ledger.")
            return
    elif args.type == "weekly_summary":
        msg = build_weekly_summary(
            equity_log_path=args.equity_log,
            regime_state=args.regime,
            regime_json_path=args.regime_json,
        )
    else:
        raise ValueError(args.type)

    if args.dry_run:
        print(msg)
        return
    resp = send(msg, creds_path=args.creds)
    if resp.get("ok"):
        print("Sent OK.")
    else:
        print("FAILED:", resp)
        sys.exit(2)


if __name__ == "__main__":
    main()
