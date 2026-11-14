"""
exit_report.py
==============
The one exit-reporting function for the paper ledger. Exit DETAILS are sent by one path only:
  * daily_status_broadcaster.py                 V5 Daily Data Refresh, right after paper_trade_manager.
                                                Sends the report, then writes last_exit_report.txt; the
                                                same workflow commits data/, so the marker persists.
  * telegram_broadcaster.py --type daily_exit   V5 Daily Exit Check. Runs on its own runner and cannot
                                                commit, so it never sends exit details: if the latest
                                                run has no report recorded, it sends missing_report_notice
                                                (counts only) and writes nothing.
  * daily_exit_broadcaster.py                   not scheduled; prints a preview, never sends

For every session the ledger processed since the last report it lists, separately:
  trade exits       realized_trades.csv rows that are sales: stops, gaps through the stop, time
                    stops, entitlement sales. Already booked in the paper ledger at the price shown.
  cash settlements  CASH_DIVIDEND / CASH_CONSIDERATION / FRACTION_CASH rows: dividends, exit/merger
                    consideration or a fraction's sale proceeds credited to cash. Money arriving, not a
                    trade exit, and not in the exit P&L.
  needs attention   holdings that became stale or exit-offer locked in those sessions (the ledger's
                    dated notes "YYYYMMDD: stale: ..." / "YYYYMMDD: locked: ...") and are still so now

Sessions covered: after the last reported ledger run (last_exit_report.txt, written only after a
message is actually sent) - or, with no marker, after the previous equity-log run - up to the
latest run. A run that processed several missed sessions (catch-up) is covered whole, and so are
several unreported runs. Dates come from the ledger, never the wall clock.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

CASH_REASONS = {"CASH_DIVIDEND": "dividend", "CASH_CONSIDERATION": "cash consideration",
                "FRACTION_CASH": "fractional-share sale proceeds"}
MARKER_NAME = "last_exit_report.txt"
NOTE = re.compile(r"(\d{8}): (stale|locked): ([^|]+)")


def _ymd(x) -> str:
    s = "" if x is None else str(x).strip()
    if s.lower() in ("", "nan", "none", "nat"):
        return ""
    s = s.split(".")[0].replace("-", "")[:8]
    return s if len(s) == 8 and s.isdigit() else ""


def _d(s: str) -> str:
    return pd.Timestamp(s).strftime("%d %b %Y") if s else ""


def marker_path(positions_path) -> Path:
    return Path(positions_path).parent / MARKER_NAME


def read_marker(path) -> str:
    p = Path(path)
    return _ymd(p.read_text(encoding="utf-8").strip()) if p.exists() else ""


BEFORE_FIRST = "19000101"     # marker value meaning "no ledger run has been reported yet"


def write_marker(path, run_date: str) -> None:
    """Record that every ledger run up to run_date (YYYYMMDD) has been reported."""
    Path(path).write_text(f"{run_date[:4]}-{run_date[4:6]}-{run_date[6:]}\n", encoding="utf-8")


def record(path, rep: dict | None, sent: bool) -> None:
    """After the nightly status has tried to send. Sent: the latest run is reported. Not sent: keep the
    marker where it is - or, if there is none yet, create it at the last run known to be reported (the
    start of this window), so the next report starts there and the unsent exits are not skipped."""
    if rep is None or rep["window"]["already_reported"]:
        return
    if sent:
        write_marker(path, rep["window"]["end"])
    elif not Path(path).exists():
        write_marker(path, rep["window"]["start"] or BEFORE_FIRST)


def window(equity_log: pd.DataFrame | None, marker: str = "") -> dict | None:
    """Ledger runs not yet reported: {'start' (exclusive, '' = from the beginning), 'end', 'runs',
    'already_reported'}. None when there is no equity log (the ledger has never run)."""
    if equity_log is None or equity_log.empty or "DATE" not in equity_log.columns:
        return None
    runs = sorted({d for d in equity_log["DATE"].map(_ymd) if d})
    if not runs:
        return None
    end = runs[-1]
    if marker and marker >= end:
        return {"start": marker, "end": end, "runs": [], "already_reported": True}
    start = marker if marker else (runs[-2] if len(runs) > 1 else "")
    return {"start": start, "end": end, "runs": [r for r in runs if (not start or r > start) and r <= end],
            "already_reported": False}


def collect(realized: pd.DataFrame | None, positions: pd.DataFrame | None, equity_log: pd.DataFrame | None,
            marker: str = "") -> dict | None:
    """Exit events for the unreported ledger runs, or None when the ledger has never run."""
    w = window(equity_log, marker)
    if w is None:
        return None
    rep = {"window": w, "trades": pd.DataFrame(), "cash": pd.DataFrame(), "attention": []}
    if w["already_reported"]:
        rep["has_events"] = False
        return rep
    inside = lambda d: bool(d) and (not w["start"] or d > w["start"]) and d <= w["end"]
    if realized is not None and len(realized) and "exit_date" in realized.columns:
        reason = realized["exit_reason"].astype(str) if "exit_reason" in realized.columns else ""
        r = realized.assign(xdate=realized["exit_date"].map(_ymd), xreason=reason)
        r = r[r["xdate"].map(inside)].sort_values(["xdate", "symbol"])
        rep["trades"] = r[~r["xreason"].isin(CASH_REASONS)]
        cash = r[r["xreason"].isin(CASH_REASONS)].copy()
        if len(cash):
            cash["amount"] = [_cash_amount(positions, c) for c in cash.itertuples(index=False)]
        rep["cash"] = cash
    if positions is not None and len(positions) and "notes" in positions.columns:
        for p in positions.to_dict("records"):
            status = str(p.get("status", "")).lower()
            locked_now = status == "pending" and str(p.get("kind", "")) == "shares" and ":locked" in str(p.get("ca_applied", ""))
            if status != "stale" and not locked_now:
                continue
            want = "stale" if status == "stale" else "locked"
            hits = [(d, k, t.strip()) for d, k, t in NOTE.findall(str(p.get("notes", ""))) if k == want and inside(d)]
            if hits:
                d, k, t = hits[-1]
                rep["attention"].append({"symbol": str(p.get("symbol", "")).upper(), "what": k, "date": d,
                                         "detail": t[:160]})
    rep["has_events"] = bool(len(rep["trades"]) or len(rep["cash"]) or rep["attention"])
    return rep


def _cash_amount(positions, c) -> float | None:
    """Amount credited, from the settled cash row in positions.csv (realized_trades holds only P&L)."""
    need = ("symbol", "kind", "cash_due", "exit_date", "exit_reason")
    if positions is None or positions.empty or any(k not in positions.columns for k in need):
        return None
    m = positions[(positions["symbol"].astype(str).str.upper() == str(c.symbol).upper())
                  & (positions["kind"].astype(str).isin(["cash", "fraction"]))
                  & (positions["exit_date"].map(_ymd) == c.xdate)
                  & (positions["exit_reason"].astype(str) == c.xreason)]
    v = pd.to_numeric(m["cash_due"], errors="coerce").dropna()
    return float(v.sum()) if len(v) else None


def coverage(rep: dict) -> str:
    w = rep["window"]
    if w["start"] == BEFORE_FIRST:
        return f"every ledger run up to {_d(w['end'])} - none reported before"
    days = len(pd.bdate_range(pd.Timestamp(w["start"]) + pd.Timedelta(days=1), w["end"])) if w["start"] else 1
    span = (f"sessions after {_d(w['start'])} up to {_d(w['end'])}" if w["start"] else f"ledger run of {_d(w['end'])}")
    extra = []
    if days > 1:
        extra.append(f"catch-up: {days} weekdays")
    if len(w["runs"]) > 1:
        extra.append(f"{len(w['runs'])} ledger runs not reported before")
    return span + (f" ({'; '.join(extra)})" if extra else "")


def lines(rep: dict) -> list[str]:
    """Message lines (Telegram Markdown) for the exit part of a report."""
    out = [f"_Covers {coverage(rep)}._", ""]
    t = rep["trades"]
    if len(t):
        out.append(f"*Trade exits ({len(t)})* — already booked in the paper ledger")
        total = 0.0
        for r in t.itertuples(index=False):
            pnl = pd.to_numeric(getattr(r, "pnl_inr", None), errors="coerce")
            pct = pd.to_numeric(getattr(r, "pnl_pct", None), errors="coerce")
            px = pd.to_numeric(getattr(r, "exit_price", None), errors="coerce")
            days = pd.to_numeric(getattr(r, "days_held", None), errors="coerce")
            total += 0.0 if pd.isna(pnl) else float(pnl)
            mark = "✅" if pnl == pnl and pnl > 0 else "❌" if pnl == pnl and pnl < 0 else "➖"
            detail = (([f"{pct * 100:+.2f}%"] if pct == pct else []) + ([f"₹{pnl:+,.0f}"] if pnl == pnl else [])
                      + ([f"{int(days)}d"] if days == days else []))
            out.append(f"{mark} `{r.symbol}` — {r.xreason} at ₹{px:,.2f} on {_d(r.xdate)}"
                       + (f"  ({' / '.join(detail)})" if detail else ""))
        out.append(f"_Trade exits P&L:_ ₹{total:+,.0f}")
        out.append("")
    c = rep["cash"]
    if len(c):
        out.append(f"*Cash settled ({len(c)})* — not trade exits")
        for r in c.itertuples(index=False):
            amt = f"₹{r.amount:,.2f}" if r.amount is not None and r.amount == r.amount else "amount not in ledger rows"
            out.append(f"• `{r.symbol}` — {CASH_REASONS[r.xreason]} {amt} credited {_d(r.xdate)}")
        out.append("")
    a = rep["attention"]
    if a:
        out.append(f"*Needs attention ({len(a)})*")
        for x in a:
            verb = "became stale" if x["what"] == "stale" else "locked for an exit offer"
            out.append(f"• `{x['symbol']}` {verb} on {_d(x['date'])} — {x['detail']}")
        out.append("")
    return out


def build_message(rep: dict | None, title: str = "V5 PAPER EXITS") -> str | None:
    """A complete exit alert, or None when there is nothing new to report."""
    if rep is None or not rep.get("has_events"):
        return None
    return "\n".join([f"*⚠️ {title}*", ""] + lines(rep)).rstrip()


def missing_report_notice(rep: dict | None) -> str | None:
    """For the Exit Check job: a warning - counts only, no exit details - when the latest ledger run has
    no report recorded as sent. None when there is nothing to warn about. Exit details are sent only by
    the nightly status (its workflow commits the marker), so the same exits are never sent twice."""
    if rep is None or rep["window"]["already_reported"]:
        return None
    n_t, n_c, n_a = len(rep["trades"]), len(rep["cash"]), len(rep["attention"])
    return ("*⚠️ V5 exit report not sent*\n\n"
            f"The paper ledger ran ({coverage(rep)}) but no report for it was recorded as sent - "
            "the nightly status message may have failed.\n"
            f"Pending: {n_t} trade exit(s), {n_c} cash settlement(s), {n_a} holding(s) needing attention.\n"
            "_The details will be in the next nightly report; this check never repeats them._")


def collect_paths(realized_path, positions_path, equity_log_path, marker_file=None) -> dict | None:
    read = lambda p: pd.read_csv(p) if p and Path(p).exists() else None
    marker = read_marker(marker_file or marker_path(positions_path))
    return collect(read(realized_path), read(positions_path), read(equity_log_path), marker)
