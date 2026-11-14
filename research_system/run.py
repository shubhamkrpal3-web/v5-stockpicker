"""Orchestrator: one entry point per phase of the trading day.

    python -m research_system.run --phase post_close     # after the close (main run)
    python -m research_system.run --phase pre_market     # before 09:00 IST
    python -m research_system.run --phase auto           # pick from the NSE calendar and clock

Phases (times from config [session]; holidays from NSE's calendar):
  pre_market  overnight global markets, new public filings, data quality,
              re-check of yesterday's approved ideas for cancel conditions
  pre_open    09:00-09:15 indicative prices -> needs a live feed (NO TRADE without)
  regular     09:15-15:30 continuous updates -> needs a live feed (NO TRADE without)
  post_close  official prices, flows, F&O data, filings -> market state ->
              engines -> risk engine -> report -> audit -> outcome reconciliation

Data refresh steps never abort the run: a failed step leaves its dataset
stale, the quality check records that, and the risk engine rejects any
candidate that depends on it.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from research_system import audit, config, context, market_state, paths, report
from research_system.candidate import NoTrade
from research_system.data import calendar, events, flows, fno, indices, macro, quality
from research_system.data.nse import NSE
from research_system.engines import fno as fno_engine
from research_system.engines import intraday_short, investment, swing
from research_system.portfolio import load as load_portfolio
from research_system.risk_engine import RiskEngine


def _step(log: list, name: str, fn, *a, **k):
    try:
        out = fn(*a, **k)
        log.append({"step": name, "ok": True})
        return out
    except Exception as e:  # keep going; the quality check will catch the gap
        log.append({"step": name, "ok": False, "error": f"{type(e).__name__}: {e}"})
        print(f"[run] step {name} failed: {e}")
        traceback.print_exc(limit=1)
        return None


def refresh_data(log: list, full: bool = True) -> None:
    nse = NSE()
    sys.path.insert(0, str(paths.ROOT / "v5_modules"))
    import corporate_actions as ca
    master = paths.MASTER_CSV if paths.MASTER_CSV.exists() else paths.MASTER

    def _ca():
        sys.argv = ["corporate_actions", "--update", "--master", str(master),
                    "--events", str(paths.CORPORATE_ACTIONS), "--days", "30"]
        ca.main()
    if full:
        _step(log, "corporate_actions", _ca)
        _step(log, "indices", indices.update, date.today() - timedelta(days=10), date.today(), nse)
        _step(log, "fno", lambda: (fno.update_lots(nse), fno.update_bhav(5, nse)))
        _step(log, "participant_oi", flows.update_oi, date.today() - timedelta(days=10), date.today(), nse)
    _step(log, "macro", macro.update, "1mo")
    _step(log, "fii_dii_cash", flows.update_cash, nse)
    _step(log, "events_calendar", events.update_calendar, nse)
    _step(log, "announcements", events.update_announcements, 2, nse)
    if not paths.CALENDAR.exists() or date.today().weekday() == 0:
        _step(log, "holiday_calendar", calendar.update, nse)


def _skip_holiday(args, phase: str) -> bool:
    today = datetime.now(calendar.IST).date()
    if not args.force and not calendar.is_trading_day(today):
        print(f"[run] {today} is not an NSE trading day; {phase} skipped (use --force to run anyway)")
        return True
    return False


def post_close(args) -> int:
    if _skip_holiday(args, "post_close"):
        return 0
    cfg = config.load(args.config)
    log: list = []
    if not args.no_refresh:
        refresh_data(log, full=True)
    q = quality.check()
    ms = _step(log, "market_state", market_state.build) or market_state.load()
    ctx = context.build(cfg)
    ctx.quality = q

    inv_c, inv_nt = investment.run(ctx)
    sw_c, sw_nt = swing.run(ctx)
    fno_c, fno_nt = fno_engine.run(ctx, sw_c)
    in_c, in_nt = intraday_short.run(ctx, feed=None)

    last_px = ctx.C.iloc[-1].dropna().to_dict()
    pf = load_portfolio(cfg["account"].get("capital_inr", 0) or 1, last_px)
    entry_day = calendar.next_trading_day(ctx.asof.date()).strftime("%Y%m%d")
    eng = RiskEngine(cfg, pf, ms, q, returns=ctx.C.pct_change(fill_method=None).tail(260),
                     events=ctx.calendar, eligible_fno=set(ctx.fno_lots["SYMBOL"]), today=entry_day)
    decided = eng.evaluate_all(inv_c + sw_c + fno_c + in_c)
    no_trades = inv_nt + sw_nt + fno_nt + in_nt

    syms = sorted({c.symbol for c in decided})
    from research_system import event_intel
    ev = _step(log, "event_intel", event_intel.recent, ctx, syms, 7)

    md = report.markdown(decided, no_trades, ms, q, cfg, "post_close", ev)
    rp = report.write(md, "post_close")
    ap = audit.record("post_close", decided, no_trades, {"steps": log, "portfolio_source": pf.source})
    outcomes = _step(log, "reconcile", audit.reconcile, ctx.O, ctx.H, ctx.L, ctx.C)
    _update_oos()
    print(report.summary_text(decided, no_trades, ms))
    print(f"[run] report {rp}\n[run] audit  {ap}")
    if args.telegram:
        _telegram(report.summary_text(decided, no_trades, ms))
    return 0


def pre_market(args) -> int:
    if _skip_holiday(args, "pre_market"):
        return 0
    cfg = config.load(args.config)
    log: list = []
    if not args.no_refresh:
        refresh_data(log, full=False)
    q = quality.check()
    ms = market_state.load()
    mac = macro.wide()
    lines = [f"# Pre-market brief — {datetime.now(calendar.IST):%Y-%m-%d %H:%M} IST", "",
             "> Decision support only. No orders are placed.", "", "## Overnight global markets (last two closes)", ""]
    for s in ["SPX", "BRENT", "GOLD_USD", "USDINR", "US10Y", "DXY"]:
        if s in mac.columns and mac[s].dropna().shape[0] >= 2:
            x = mac[s].dropna()
            chg = (x.iloc[-1] - x.iloc[-2]) * 100 if s == "US10Y" else (x.iloc[-1] / x.iloc[-2] - 1) * 100
            unit = " bp" if s == "US10Y" else "%"
            lines.append(f"- {s}: {x.iloc[-1]:,.2f} ({chg:+.2f}{unit}) as of {x.index[-1]:%Y-%m-%d}")
    rel = ms.get("macro_relationships") or {}
    lines += ["", "Measured lead relationships (global move on day t-1 vs Nifty 500 on day t):", ""]
    for k in ["sp500", "brent", "usdinr", "gold_inr", "us10y_bp"]:
        ll = (rel.get(k) or {}).get("lead_lag", {}).get(1) or (rel.get(k) or {}).get("lead_lag", {}).get("1")
        if ll:
            lines.append(f"- {k}: lag-1 corr {ll['corr']:+.3f}{' (significant)' if ll['significant'] else ' (not significant)'}")
    # re-check yesterday's approved ideas against new filings
    cands = audit.load_candidates()
    lines += ["", "## Re-check of the latest approved ideas", ""]
    if cands.empty or not (cands["status"] == "approved").any():
        lines.append("No approved ideas to re-check.")
    else:
        last_run = cands["run_at"].max()
        live = cands[(cands["run_at"] == last_run) & (cands["status"] == "approved")]
        for c in live.itertuples(index=False):
            a = events.announcements_for(c.symbol, since=last_run[:10])
            mat = a[a["category"].isin(["credit_rating", "regulatory", "results", "m_and_a", "pledge",
                                        "management", "clarification"])]
            if len(mat):
                lines.append(f"- **{c.symbol}** ({c.engine}): REVIEW before entry — new filing(s): " +
                             "; ".join(f"{r.category}: {r.subject[:70]} ({r.source_url})" for r in mat.itertuples()))
            else:
                lines.append(f"- {c.symbol} ({c.engine}): no new material filings")
    lines += ["", "## Data quality", ""] + [f"- {k}: {v['status']} (last {v['last']})" for k, v in q["datasets"].items()]
    md = "\n".join(lines)
    rp = report.write(md, "pre_market")
    audit.record("pre_market", [], [], {"steps": log})
    print(md)
    print(f"[run] brief {rp}")
    if args.telegram:
        _telegram("\n".join([l for l in lines if l.startswith(("- ", "#"))][:25]))
    return 0


def live(args, phase: str) -> int:
    cfg = config.load(args.config)
    ctx = context.build(cfg)
    c, nt = intraday_short.run(ctx, feed=None)
    now = datetime.now(calendar.IST).isoformat(timespec="seconds")
    nt_all = nt + [NoTrade(e, now, [f"{phase}: no live feed configured; candidates are produced after the close"])
                   for e in ("investment", "swing", "fno")]
    audit.record(phase, c, nt_all)
    for n in nt_all:
        print(f"[{phase}] {n.engine}: NO TRADE — {'; '.join(n.reasons)}")
    return 0


def _update_oos() -> None:
    if not paths.SETUP_REGISTRY.exists():
        return
    reg = json.loads(paths.SETUP_REGISTRY.read_text())
    for k in reg:
        reg[k]["oos"] = {**audit.oos_summary(k), "note": "live outcomes from the audit log"}
    paths.SETUP_REGISTRY.write_text(json.dumps(reg, indent=2))


def _telegram(text: str) -> None:
    try:
        sys.path.insert(0, str(paths.ROOT / "v5_modules"))
        from telegram_broadcaster import send
        send(text, parse_mode=None)
    except Exception as e:
        print(f"[run] telegram skipped: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="auto",
                    choices=["auto", "pre_market", "pre_open", "regular", "post_close", "closed"])
    ap.add_argument("--config", default=None, help="risk config path (default config/risk_config.toml)")
    ap.add_argument("--no-refresh", action="store_true", help="skip data downloads; use stored data")
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even on an NSE holiday / weekend")
    args = ap.parse_args()
    phase = calendar.phase() if args.phase == "auto" else args.phase
    print(f"[run] phase {phase}")
    if phase == "post_close":
        return post_close(args)
    if phase == "pre_market":
        return pre_market(args)
    if phase in ("pre_open", "regular"):
        return live(args, phase)
    print("[run] market closed today; nothing to do (use --phase post_close to rebuild)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
