"""Central risk engine: deterministic, final veto over every candidate.

No engine can approve its own idea. evaluate_all() walks candidates in order
of confidence (ties by engine, symbol, id), sizes each one with the shared
allocator (v5_modules/allocator.py: loss budget over the full loss per unit,
liquidity / capital / concentration / exposure caps, the regime applied once to
the gross cap), and books approved ideas into a hypothetical book so later
candidates in the same run see the exposure the earlier ones would add.

Every check is recorded on the candidate as {"check", "result", "detail"} where
result is PASS, FAIL, ADJUST (size reduced) or INFO.
"""
from __future__ import annotations

import copy
import sys
from dataclasses import replace
from datetime import datetime

import numpy as np
import pandas as pd

from research_system import config as cfgmod
from research_system import paths
from research_system.candidate import Candidate, Sizing
from research_system.portfolio import Portfolio, Position

sys.path.insert(0, str(paths.ROOT / "v5_modules"))
import allocator  # noqa: E402

DATASETS_BY_ENGINE = {
    "investment": ["equity_eod", "fundamentals"],
    "swing": ["equity_eod", "indices"],
    "fno": ["equity_eod", "fno_summary"],
    "intraday_short": ["equity_eod"],
}
REGIME_GROSS = allocator.REGIME_GROSS   # V5 backtested schedule


class RiskEngine:
    def __init__(self, cfg: dict, portfolio: Portfolio, market_state: dict | None = None,
                 quality: dict | None = None, returns: pd.DataFrame | None = None,
                 events: pd.DataFrame | None = None, eligible_fno: set | None = None,
                 today: str | None = None):
        self.cfg = cfg
        self.book = copy.deepcopy(portfolio)
        self.ms = market_state or {}
        self.quality = quality or {}
        self.returns = returns            # dates x symbols daily returns (adjusted)
        self.events = events              # calendar: symbol, event_date (YYYYMMDD), purpose
        self.eligible_fno = eligible_fno or set()
        self.today = today or datetime.now().strftime("%Y%m%d")
        self.configured, self.config_problems = cfgmod.budget_status(cfg)
        self.policy = self._policy()

    # ------------------------------------------------------------ helpers
    def _policy(self) -> allocator.Policy:
        """The configured limits as the shared allocator's policy (unset unless the budget is configured)."""
        p = allocator.policy_from_risk_config(self.cfg)
        return p if self.configured else replace(p, configured=False)

    def _book_state(self) -> allocator.BookState:
        b = self.book
        fm = float(self._ecfg("fno").get("futures_margin_pct", 0.2))
        return allocator.BookState(
            exposures=tuple(allocator.Exposure(p.symbol, p.engine, p.notional, p.sector, p.segment,
                                               p.underlying or p.symbol) for p in b.positions),
            claims=tuple(allocator.Claim(o.symbol, o.engine, o.owed, o.slot, o.held) for o in b.others),
            available_cash=b.available_cash, problems=tuple(b.problems),
            margin_used=sum(abs(p.notional) * fm for p in b.positions if p.segment == "fno"))

    @staticmethod
    def _priority(c: Candidate) -> float:
        return c.confidence.score if c.confidence else 0.0

    def _request(self, c: Candidate, size_mult: float, ecfg: dict) -> allocator.Request:
        parts = c.unit_loss_parts or {}
        cap = ecfg.get("max_loss_per_trade_inr", 0) if c.engine == "fno" else 0
        return allocator.Request(
            key=c.id, symbol=c.symbol, engine=c.engine, priority=self._priority(c), price=c.price,
            stop_loss=parts.get("stop"), gap_allowance=parts.get("gap"), costs=parts.get("costs"),
            unit_capital=c.unit_capital_inr, unit_notional=c.unit_notional_inr, unit_margin=c.unit_margin_inr,
            sector=c.sector, adv_inr=c.adv_inr, direction=c.direction, segment=c.segment, underlying=c.symbol,
            size_multiplier=size_mult, max_loss_inr=cap if cap and cap > 0 else None,
            stated_unit_loss=c.unit_risk_inr)

    @staticmethod
    def _rec(c: Candidate, check: str, result: str, detail: str) -> None:
        c.risk_checks.append({"check": check, "result": result, "detail": detail})

    def _reject(self, c: Candidate, check: str, detail: str) -> Candidate:
        self._rec(c, check, "FAIL", detail)
        c.status = "rejected"
        return c

    def _ecfg(self, engine: str) -> dict:
        return self.cfg.get("engines", {}).get(engine, {})

    def _corr(self, sym: str) -> tuple[float | None, float | None, str | None]:
        """(max pairwise, average, most-correlated symbol) vs the open book."""
        if self.returns is None or sym not in self.returns.columns or not self.book.positions:
            return None, None, None
        lb = int(self.cfg.get("correlation", {}).get("lookback_days", 126))
        r = self.returns.tail(lb)
        others = [p.underlying or p.symbol for p in self.book.positions]
        others = [o for o in dict.fromkeys(others) if o in r.columns and o != sym]
        if not others:
            return None, None, None
        sub = r[[sym] + others].dropna(thresh=int(lb * 0.6), axis=1)
        if sym not in sub.columns or sub.shape[1] < 2:
            return None, None, None
        cs = sub.corr()[sym].drop(sym)
        return float(cs.max()), float(cs.mean()), str(cs.idxmax())

    def _events(self, c: Candidate) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(events near entry, events later inside the expected hold)."""
        empty = pd.DataFrame()
        if self.events is None or self.events.empty:
            return empty, empty
        w = int(self.cfg.get("event_risk", {}).get("results_window_days", 3))
        near_end = (pd.Timestamp(self.today) + pd.Timedelta(days=w)).strftime("%Y%m%d")
        hold_end = max(c.hold_until or near_end, near_end)
        e = self.events
        e = e[(e["symbol"] == c.symbol.upper()) &
              e["purpose"].str.contains("result|ex_date", case=False, regex=True)]
        near = e[(e["event_date"] >= self.today) & (e["event_date"] <= near_end)]
        later = e[(e["event_date"] > near_end) & (e["event_date"] <= hold_end)]
        return near, later

    # ------------------------------------------------------------ main
    def evaluate(self, c: Candidate) -> Candidate:
        c.risk_checks = []
        cfg, lim, acct = self.cfg, self.cfg.get("limits", {}), self.cfg.get("account", {})
        ecfg = self._ecfg(c.engine)

        # 1. Budget must be configured explicitly
        if not self.configured:
            return self._reject(c, "risk_budget", f"risk budget not configured ({len(self.config_problems)} "
                                                  "items; see the Risk budget section)")
        self._rec(c, "risk_budget", "PASS", f"configured ({cfg.get('_source')})")

        # 1b. The book must be fully valued: every active ledger state, and cash that reconciles
        if self.book.problems:
            return self._reject(c, "book_view", "; ".join(self.book.problems))
        self._rec(c, "book_view", "PASS", self._book_note())

        # 2. Engine enabled
        if not ecfg.get("enabled", False):
            return self._reject(c, "engine_enabled", f"{c.engine} engine disabled in config")

        # 3. Data freshness
        dq = self.quality.get("datasets", {})
        lag_cfg = cfg.get("data", {})
        stale = []
        for ds in DATASETS_BY_ENGINE.get(c.engine, []):
            st = dq.get(ds)
            if st is None or st["status"] == "MISSING":
                stale.append(f"{ds} missing")
            elif (st.get("lag_sessions") or 0) > int(lag_cfg.get(ds, 0)):
                stale.append(f"{ds} {st['lag_sessions']} sessions old (max {lag_cfg.get(ds, 0)})")
        if stale:
            return self._reject(c, "data_freshness", "; ".join(stale))
        self._rec(c, "data_freshness", "PASS", "required inputs current")

        # 4. Kill switch: drawdown and daily loss
        dd, day = self.book.drawdown(), self.book.today_pnl_pct()
        if dd is not None and lim.get("max_drawdown_pct") and dd <= -abs(lim["max_drawdown_pct"]):
            return self._reject(c, "kill_switch", f"drawdown {dd:.1%} breaches {lim['max_drawdown_pct']:.0%}")
        if day is not None and lim.get("daily_loss_limit_inr"):
            day_inr = day * self.book.capital
            if day_inr <= -abs(lim["daily_loss_limit_inr"]):
                return self._reject(c, "kill_switch", f"today's P&L Rs{day_inr:,.0f} breaches daily limit")
        self._rec(c, "kill_switch", "PASS",
                  f"drawdown {dd:.1%}" if dd is not None else "no equity history (kill switch inactive)")

        # 5. Confidence floor
        if c.confidence is None:
            return self._reject(c, "confidence", "no confidence assessment")
        min_g = ecfg.get("min_grade", "C")
        if not cfgmod.grade_at_least(c.confidence.grade, min_g):
            return self._reject(c, "confidence", f"grade {c.confidence.grade} below engine minimum {min_g}")
        self._rec(c, "confidence", "PASS", f"grade {c.confidence.grade} (min {min_g})")

        # 6. Instrument eligibility and liquidity floor
        liq = cfg.get("liquidity", {})
        if c.segment == "fno" or (c.engine == "intraday_short" and ecfg.get("require_fno_listed", True)):
            if c.symbol.upper() not in self.eligible_fno:
                return self._reject(c, "instrument", f"{c.symbol} not in the current NSE F&O contract list")
        if c.segment == "cash":
            if c.price < liq.get("min_price_inr", 0):
                return self._reject(c, "instrument", f"price Rs{c.price:.2f} below floor")
            if c.adv_inr < liq.get("min_adv_inr", 0):
                return self._reject(c, "liquidity", f"20d ADV Rs{c.adv_inr/1e7:.1f} Cr below floor")
        self._rec(c, "instrument", "PASS", f"{c.segment} eligible")

        # 7. Strategy-specific rules
        if c.engine == "fno":
            naked = any(l.side == "SELL" and l.instrument in ("CE", "PE") for l in c.legs) and \
                not self._is_defined_risk(c)
            if naked and not ecfg.get("allow_naked_short_options", False):
                return self._reject(c, "strategy_rules", "naked short option not allowed")
            if acct.get("fno_margin_inr", 0) <= 0:
                return self._reject(c, "strategy_rules", "derivatives disabled (account.fno_margin_inr = 0)")
        if c.engine == "intraday_short":
            if c.overnight:
                return self._reject(c, "strategy_rules", "intraday short must be flat by session end")
        if c.direction == "short" and c.segment == "cash" and c.overnight:
            return self._reject(c, "strategy_rules", "overnight cash short needs SLB borrow; not supported")
        self._rec(c, "strategy_rules", "PASS", "engine rules satisfied")

        # 8. Event risk: near entry, and later inside the expected hold
        size_mult = 1.0
        if c.overnight:
            near, later = self._events(c)
            er = cfg.get("event_risk", {})
            for frame, action, label in ((near, er.get("results_action", "reject"), "near entry"),
                                         (later, er.get("in_hold_action", "halve"), "inside hold")):
                if frame.empty:
                    continue
                desc = ", ".join(f"{r.purpose} {r.event_date}" for r in frame.itertuples())
                if action == "reject":
                    return self._reject(c, "event_risk", f"{label}: {desc}")
                if action == "halve":
                    size_mult *= 0.5
                    self._rec(c, "event_risk", "ADJUST", f"{label}: {desc} -> size halved")
                else:
                    self._rec(c, "event_risk", "INFO", f"{label}: {desc} (allowed by config)")
            if near.empty and later.empty:
                self._rec(c, "event_risk", "PASS", "no scheduled results/ex-dates near entry or inside hold")
        else:
            self._rec(c, "event_risk", "PASS", "intraday: flat before session end")

        # 9. Macro overlay (risk control only: can reduce, never increase)
        mo = cfg.get("macro_overlay", {})
        ra = (self.ms.get("broad") or {}).get("risk_appetite_z")
        if mo.get("enabled") and ra is not None and ra < mo.get("risk_off_z", -1.5):
            size_mult *= float(mo.get("scale", 0.5))
            self._rec(c, "macro_overlay", "ADJUST", f"risk appetite z {ra:+.2f} < {mo['risk_off_z']}: size x{mo['scale']}")
        else:
            self._rec(c, "macro_overlay", "PASS", f"risk appetite z {ra:+.2f}" if ra is not None else "not available")

        # 10-12. Size and exposure: the shared allocator (v5_modules/allocator.py). Loss budget over the full loss
        # per unit (stop + gap allowance + costs); position, liquidity, capital, verified-cash and margin caps; the
        # regime-scaled gross cap (the only regime adjustment); net cap, slots, duplicates, sector cap. Missing
        # inputs (regime, sector, traded value, any part of the loss) reject the candidate.
        regime = (self.ms.get("broad") or {}).get("regime")
        dec = allocator.size_one(self._request(c, size_mult, ecfg), self._book_state(), self.policy, regime)
        for check, result, detail in dec.checks:
            self._rec(c, check, result, detail)
        if dec.status != "approved":
            c.status = "rejected"
            return c
        q, add, notes = dec.quantity, dec.notional_inr, dec.notes

        # 13. Correlation with open positions
        mx, avg, who = self._corr(c.symbol)
        cc = cfg.get("correlation", {})
        if mx is not None:
            if mx > cc.get("max_pairwise", 0.8):
                return self._reject(c, "correlation", f"corr {mx:.2f} with {who} > {cc['max_pairwise']}")
            if avg > cc.get("max_avg_with_book", 0.6):
                return self._reject(c, "correlation", f"avg corr with book {avg:.2f} > {cc['max_avg_with_book']}")
            self._rec(c, "correlation", "PASS", f"max {mx:.2f} ({who}), avg {avg:.2f}")
        else:
            self._rec(c, "correlation", "INFO", "no overlapping history with the book")

        # 14. Factor exposure (beta to market, macro factors via sector partial betas)
        fx = self._factor_exposure(c, add)
        fc = cfg.get("factors", {})
        if fc.get("max_beta_weighted_gross", 0) > 0 and fx.get("beta_gross") is not None \
                and fx["beta_gross"] > fc["max_beta_weighted_gross"]:
            return self._reject(c, "factor_exposure", f"beta-weighted gross {fx['beta_gross']:.2f} > cap")
        if fc.get("max_macro_exposure", 0) > 0:
            for f, v in fx.get("macro", {}).items():
                if abs(v) > fc["max_macro_exposure"]:
                    return self._reject(c, "factor_exposure", f"{f} exposure {v:+.3f} > cap")
        if fx.get("macro"):
            detail = "book macro exposure " + ", ".join(f"{k} {v:+.3f}" for k, v in fx["macro"].items())
        elif self.ms.get("sector_macro_betas"):
            detail = "no statistically significant macro betas for the book's sectors"
        else:
            detail = "sector macro betas not computed"
        if fx.get("beta_gross") is not None:
            detail += f"; beta-weighted gross {fx['beta_gross']:.2f}"
        self._rec(c, "factor_exposure", "INFO", detail)

        # Approved: attach sizing and book it
        c.sizing = Sizing(quantity=q, capital_inr=dec.capital_inr, margin_inr=dec.margin_inr,
                          max_planned_loss_inr=dec.planned_loss_inr, exposure_after={**dec.exposure_after, **fx},
                          notes=notes + ["stops are planned controls, not guaranteed fills: gaps, halts, "
                                         "thin books or rejected orders can make the realised loss larger"])
        c.status = "approved"
        if self.book.available_cash is not None and c.segment == "cash":
            self.book.available_cash -= q * c.unit_capital_inr
        self.book.add(Position(engine=c.engine, symbol=c.symbol, segment=c.segment, direction=c.direction,
                               quantity=q * (c.unit_notional_inr / c.price if c.price else 1),
                               entry_price=c.price, stop=c.plan.stop or 0.0, last_price=c.price,
                               sector=c.sector, underlying=c.symbol, overnight=c.overnight))
        return c

    def _book_note(self) -> str:
        b = self.book
        states: dict = {}
        for p in b.positions:
            states[p.state] = states.get(p.state, 0) + 1
        for o in b.others:
            states[o.state] = states.get(o.state, 0) + 1
        parts = [f"{b.source or 'book'}: " + (", ".join(f"{n} {s}" for s, n in states.items()) or "empty"),
                 f"share exposure Rs{b.gross():,.0f}", f"owed Rs{b.owed():,.0f}", f"slots {b.count()}"]
        parts.append(f"available cash Rs{b.available_cash:,.0f} (verified against ledger records)"
                     if b.available_cash is not None else "available cash n/a")
        return "; ".join(parts)

    def _is_defined_risk(self, c: Candidate) -> bool:
        """True when every short option is covered by a long option of the same type/expiry."""
        for typ in ("CE", "PE"):
            shorts = sum(l.ratio for l in c.legs if l.instrument == typ and l.side == "SELL")
            longs = sum(l.ratio for l in c.legs if l.instrument == typ and l.side == "BUY")
            if shorts > longs:
                return False
        return True

    def _factor_exposure(self, c: Candidate, add_notional: float) -> dict:
        cap = self.cfg["account"]["capital_inr"]
        betas = self.ms.get("sector_macro_betas") or {}
        mkt_beta = self.ms.get("sector_market_beta") or {}
        out: dict = {}
        positions = [(p.sector, p.notional) for p in self.book.positions] + \
                    [(c.sector, (1 if c.direction == "long" else -1) * add_notional)]
        if mkt_beta:
            out["beta_gross"] = round(sum(abs(n) / cap * mkt_beta.get(s, 1.0) for s, n in positions), 4)
        macro: dict = {}
        for s, n in positions:
            for f, b in (betas.get(s) or {}).items():
                macro[f] = macro.get(f, 0.0) + n / cap * b
        out["macro"] = {k: round(v, 4) for k, v in macro.items()}
        return out

    def evaluate_all(self, candidates: list) -> list:
        order = sorted(candidates, key=lambda c: allocator.rank_key(self._priority(c), c.engine, c.symbol, c.id))
        return [self.evaluate(c) for c in order]
