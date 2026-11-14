"""Render a run as Markdown (file) and a short text summary (Telegram/email).

Approved candidates are shown in full. Rejected candidates show the check that
stopped them. An engine with nothing approved shows NO TRADE and the reasons.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from research_system import paths

ENGINE_ORDER = ["investment", "swing", "fno", "intraday_short"]
ENGINE_TITLE = {"investment": "Investment", "swing": "Swing", "fno": "Equity derivatives",
                "intraday_short": "Intraday short"}


def _inr(x) -> str:
    return "-" if x is None else f"Rs{x:,.0f}"


def _candidate_md(c) -> list[str]:
    p, cf, sz = c.plan, c.confidence, c.sizing
    L = [f"#### {c.symbol} — {c.instrument} ({c.direction}, {c.horizon})",
         f"*Engine* {c.engine} · *setup* `{c.setup}` · *data* {c.data_timestamp} · *id* `{c.id}`", "",
         f"**Thesis.** {c.thesis}", "",
         "**Plan**",
         f"- Entry: {p.entry_trigger}" + (f" — zone {p.entry_low}–{p.entry_high}" if p.entry_low or p.entry_high else ""),
         f"- Stop / invalidation: {p.invalidation}",
         f"- Targets: {', '.join(str(t) for t in p.targets) or '-'}",
         f"- Time exit: {p.time_exit} · holding window: {p.holding_window}" + (f" · review by {p.review_by}" if p.review_by else "")]
    if c.legs:
        L += ["", "**Legs**"] + [f"- {l.side} {l.symbol} {l.expiry or ''} {l.strike or ''} {l.instrument} "
                                 f"@ {l.price:.2f} x{l.ratio} lot ({l.lot_size})"
                                 + (f", IV {l.iv:.1%}" if l.iv else "") for l in c.legs]
    if sz:
        ex = sz.exposure_after
        L += ["", "**Size (set by the risk engine)**",
              f"- Quantity: {sz.quantity} {'units' if c.segment == 'fno' else 'shares'} · capital {_inr(sz.capital_inr)}"
              + (f" · margin {_inr(sz.margin_inr)}" if sz.margin_inr else ""),
              f"- Maximum planned loss: **{_inr(sz.max_planned_loss_inr)}** (incl. gap allowance and charges)",
              f"- Portfolio after entry: gross {ex.get('gross_pct', 0):.0%}, net {ex.get('net_pct', 0):.0%}, "
              f"sector {ex.get('sector_pct', 0):.0%}, open positions {ex.get('open_positions')}"]
        if ex.get("macro"):
            L.append("- Book macro exposure (sector partial betas): " +
                     ", ".join(f"{k} {v:+.3f}" for k, v in ex["macro"].items()))
    L += ["", "**Scenarios (per unit, after estimated charges/slippage)**"] + \
         [f"- {s.name}: {('@ ' + str(s.underlying_price) + ' -> ') if s.underlying_price else ''}{s.pnl_inr:+,.2f}"
          for s in c.scenarios]
    L += ["", f"**Confidence: grade {cf.grade}** (score {cf.score:.2f}; {cf.calibration_note})"]
    L += [f"- {k}: {v['score']:.2f} — {v['note']}" for k, v in cf.components.items()]
    if cf.caps:
        L += [f"- cap: {x}" for x in cf.caps]
    if cf.probability:
        pr = cf.probability
        L.append(f"- calibrated probability {pr['p']:.0%} that {pr['event']} (horizon {pr['horizon']}, n={pr['n']}, {pr['period']})")
    L += ["", "**Evidence**"] + [f"- [{e.kind}] {e.text} — {e.source} ({e.as_of})" for e in c.evidence]
    L += ["", "**Risks:** " + "; ".join(c.risks)]
    if c.missing_inputs:
        L.append("**Missing / stale inputs:** " + "; ".join(c.missing_inputs))
    L.append("**Cancel if:** " + "; ".join(c.cancel_conditions))
    L += ["", "<details><summary>Risk-engine checks</summary>", ""] + \
         [f"- {r['result']} {r['check']}: {r['detail']}" for r in c.risk_checks] + ["", "</details>", ""]
    return L


def markdown(decided: list, no_trades: list, ms: dict, quality: dict, cfg: dict, phase: str,
             events: pd.DataFrame | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    b = ms.get("broad") or {}
    L = [f"# Research run — {phase} — {now}", "",
         "> Decision support for personal use. Not investment advice. No orders are placed. "
         "The default output is NO TRADE.", ""]
    approved = [c for c in decided if c.status == "approved"]
    L += [f"**Approved candidates: {len(approved)}** · rejected: {sum(c.status == 'rejected' for c in decided)} · "
          f"risk config: `{cfg.get('_source')}`", ""]

    from research_system import config as cfgmod
    ok, problems = cfgmod.budget_status(cfg)
    if not ok:
        L += ["## Risk budget", "", "**Not configured: every candidate is rejected (NO TRADE).** "
              "Copy `config/risk_config.example.toml` to `config/risk_config.toml` and set:", ""]
        L += [f"- {x}" for x in problems] + [""]
    allowed = cfg.get("data", {})
    L += ["## Data quality", "", "| dataset | status | last | lag (sessions) | allowed lag |", "|---|---|---|---|---|"]
    for k, v in (quality.get("datasets") or {}).items():
        L.append(f"| {k} | {v['status']} | {v['last']} | {v['lag_sessions']} | {allowed.get(k, '-')} |")
    L += ["", f"Expected session: {quality.get('expected_session')}", ""]

    L += ["## Market state", "",
          f"As of {ms.get('as_of')}: **{b.get('regime')}** · index {b.get('index_source')} · "
          f"breadth {b.get('breadth_above_50dma', 0):.0%} above 50-DMA · realised vol "
          f"{(b.get('volatility') or {}).get('realized_20d', 0):.1%} · India VIX {(b.get('volatility') or {}).get('india_vix')} · "
          f"risk appetite z {b.get('risk_appetite_z')}", ""]
    if b.get("risk_appetite_components"):
        L.append("Risk appetite components (z): " + ", ".join(f"{k} {v:+.2f}" for k, v in b["risk_appetite_components"].items() if v is not None))
        L.append("")
    if b.get("flows_latest"):
        f = b["flows_latest"]
        L += [f"Institutional cash flows {f['date']}: FII {f['fii_net_cr']:+,.0f} cr, DII {f['dii_net_cr']:+,.0f} cr "
              f"(history: {f['history_sessions']} sessions)", ""]
    rel = ms.get("macro_relationships") or {}
    if rel:
        L += ["### Measured macro relationships (daily changes vs Nifty 500)", "",
              "| factor | label | corr [95% CI] | 126d corr | 126d beta | sign agreement | best lead (k: corr) | n |",
              "|---|---|---|---|---|---|---|---|"]
        for k, v in rel.items():
            if v.get("label") == "insufficient":
                L.append(f"| {k} | insufficient | | | | | | {v.get('n')} |")
                continue
            ll = v.get("lead_lag") or {}
            best = max(ll.items(), key=lambda kv: abs(kv[1]["corr"]))if ll else None
            bl = f"{best[0]}: {best[1]['corr']:+.3f}{' *' if best[1]['significant'] else ''}" if best else ""
            L.append(f"| {k} | {v['label']} | {v['corr']:+.3f} [{v['ci95'][0]:+.2f}, {v['ci95'][1]:+.2f}] | "
                     f"{v.get('corr_126d')} | {v.get('beta_126d')} | {v.get('sign_agreement')} | {bl} | {v['n']} |")
        L += ["", "_Relationships are measured, not assumed. Same-day values for global series overlap hours after "
              "the Indian close; lead correlations (factor on day t-k vs India on day t) are the decision-relevant ones. "
              "* = significant at ~95%._", ""]

    for eng in ENGINE_ORDER:
        L += [f"## {ENGINE_TITLE[eng]}", ""]
        ap = [c for c in decided if c.engine == eng and c.status == "approved"]
        rj = [c for c in decided if c.engine == eng and c.status == "rejected"]
        nts = [n for n in no_trades if n.engine == eng]
        if not ap:
            L += ["**NO TRADE**", ""]
        for c in ap:
            L += _candidate_md(c)
        if rj:
            L += ["Rejected by the risk engine:", "", "| symbol | instrument | grade | stopped at | reason |",
                  "|---|---|---|---|---|"]
            for c in rj:
                fail = next((r for r in c.risk_checks if r["result"] == "FAIL"), {"check": "-", "detail": "-"})
                L.append(f"| {c.symbol} | {c.instrument} | {c.confidence.grade if c.confidence else '-'} | "
                         f"{fail['check']} | {fail['detail']} |")
            L.append("")
        for n in nts:
            L += [f"- NO TRADE note: {'; '.join(n.reasons)}"]
        L.append("")
    if events is not None and len(events):
        L += ["## Recent material filings for candidate names", "",
              "| symbol | published | category | subject | day-0 abnormal | CAR 0-4 | turnover x | expectation gap |",
              "|---|---|---|---|---|---|---|---|"]
        for e in events.itertuples(index=False):
            L.append(f"| {e.symbol} | {e.published_at} | {e.category} | {str(e.subject)[:60]} | "
                     f"{getattr(e, 'abnormal_return_day0', '')} | {getattr(e, 'car_0_to_4', '')} | "
                     f"{getattr(e, 'turnover_vs_20d', '')} | {e.expectation_gap} |")
        L.append("")
    L += ["---", "Stops are planned controls, not guaranteed execution prices. Gaps, halts, thin order books and "
          "rejected or partial orders can make the realised loss exceed the planned amount."]
    return "\n".join(L)


def summary_text(decided: list, no_trades: list, ms: dict) -> str:
    b = ms.get("broad") or {}
    ap = [c for c in decided if c.status == "approved"]
    lines = [f"Research run {ms.get('as_of')}: regime {b.get('regime')}, risk appetite z {b.get('risk_appetite_z')}"]
    if not ap:
        lines.append("NO TRADE across all engines.")
        reasons = {}
        for c in decided:
            fail = next((r for r in c.risk_checks if r["result"] == "FAIL"), None)
            if fail:
                reasons[fail["check"]] = reasons.get(fail["check"], 0) + 1
        if reasons:
            lines.append("Rejections by check: " + ", ".join(f"{k} {v}" for k, v in reasons.items()))
    for c in ap:
        lines.append(f"{c.engine.upper()} {c.symbol} {c.instrument}: qty {c.sizing.quantity}, max loss "
                     f"{_inr(c.sizing.max_planned_loss_inr)}, stop {c.plan.stop}, grade {c.confidence.grade}")
    return "\n".join(lines)


def write(md: str, phase: str) -> str:
    paths.ensure_dirs()
    f = paths.REPORT_DIR / f"{datetime.now(timezone.utc):%Y%m%d}_{phase}.md"
    f.write_text(md, encoding="utf-8")
    (paths.REPORT_DIR / "latest.md").write_text(md, encoding="utf-8")
    return str(f)
