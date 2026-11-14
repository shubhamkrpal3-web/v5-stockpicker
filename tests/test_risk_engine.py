import unittest

import numpy as np
import pandas as pd

from research_system import config as cfgmod
from research_system.candidate import Leg
from research_system.portfolio import Portfolio, Position
from research_system.risk_engine import RiskEngine
from tests.helpers import FRESH, broad, cand, empty_book, test_config


def first_fail(c):
    return next((r for r in c.risk_checks if r["result"] == "FAIL"), None)


class BudgetAndDefaults(unittest.TestCase):
    def test_example_config_is_not_configured(self):
        cfg = cfgmod.load(path="__does_not_exist__.toml")
        ok, problems = cfgmod.budget_status(cfg)
        self.assertFalse(ok)
        self.assertIn("account.configured is false", problems)

    def test_unconfigured_budget_rejects_everything(self):
        cfg = cfgmod.load(path="__does_not_exist__.toml")
        c = RiskEngine(cfg, empty_book(), broad(), FRESH).evaluate(cand())
        self.assertEqual(c.status, "rejected")
        self.assertEqual(first_fail(c)["check"], "risk_budget")

    def test_loss_budget_above_capital_is_a_problem(self):
        cfg = test_config(limits__per_trade_max_loss_inr=2_000_000)
        self.assertFalse(cfgmod.budget_status(cfg)[0])


class Sizing(unittest.TestCase):
    def test_size_from_risk_budget_and_invalidation(self):
        c = RiskEngine(test_config(), empty_book(), broad(), FRESH).evaluate(cand(unit_risk=7.0))
        self.assertEqual(c.status, "approved")
        self.assertEqual(c.sizing.quantity, 5_000 // 7)                 # 714
        self.assertLessEqual(c.sizing.max_planned_loss_inr, 5_000)

    def test_capped_by_max_position(self):
        # budget alone allows 5000 shares of a Rs100 stock; 10% of 10L caps at 1000
        c = RiskEngine(test_config(), empty_book(), broad(), FRESH).evaluate(cand(unit_risk=1.0))
        self.assertEqual(c.sizing.quantity, 1000)

    def test_capped_by_liquidity(self):
        # 2% of Rs 50 lakh ADV = Rs 1 lakh -> 1000 shares; budget would allow 5000
        c = RiskEngine(test_config(liquidity__min_adv_inr=1e6), empty_book(), broad(), FRESH).evaluate(
            cand(unit_risk=1.0, adv=5e6, price=100))
        self.assertEqual(c.sizing.quantity, 1000)

    def test_below_one_unit_rejected(self):
        c = RiskEngine(test_config(), empty_book(), broad(), FRESH).evaluate(cand(unit_risk=9_000))
        self.assertEqual(first_fail(c)["check"], "sizing")

    def test_macro_overlay_only_reduces(self):
        lo = RiskEngine(test_config(), empty_book(), broad(ra=-2.0), FRESH).evaluate(cand(unit_risk=10))
        hi = RiskEngine(test_config(), empty_book(), broad(ra=+3.0), FRESH).evaluate(cand(unit_risk=10))
        self.assertEqual(lo.sizing.quantity, 250)
        self.assertEqual(hi.sizing.quantity, 500)


class PortfolioLimits(unittest.TestCase):
    def test_regime_scales_gross_cap(self):
        book = empty_book()
        book.add(Position("swing", "OLD", "cash", "long", 3_500, 100, 90, 100, sector="IT"))  # 35% gross
        cfg = test_config(limits__max_gross_exposure_pct=1.0)
        c = RiskEngine(cfg, book, broad("RISK_OFF"), FRESH).evaluate(cand(unit_risk=1))  # cap 40%
        self.assertEqual(c.status, "approved")
        self.assertEqual(c.sizing.quantity, 500)                         # room = 5% of 10L
        self.assertTrue(any(r["check"] == "gross_exposure" and r["result"] == "ADJUST" for r in c.risk_checks))

    def test_sector_cap(self):
        book = empty_book()
        book.add(Position("swing", "OLD", "cash", "long", 2_950, 100, 90, 100, sector="Healthcare"))
        c = RiskEngine(test_config(), book, broad(), FRESH).evaluate(cand(unit_risk=5))
        self.assertEqual(first_fail(c)["check"], "sector")

    def test_correlated_with_book_rejected(self):
        rng = np.random.default_rng(1)
        base = rng.normal(0, 0.01, 200)
        rets = pd.DataFrame({"AAA": base + rng.normal(0, 0.001, 200), "OLD": base})
        book = empty_book()
        book.add(Position("swing", "OLD", "cash", "long", 10, 100, 90, 100, sector="IT", underlying="OLD"))
        c = RiskEngine(test_config(), book, broad(), FRESH, returns=rets).evaluate(cand())
        self.assertEqual(first_fail(c)["check"], "correlation")

    def test_later_candidates_see_earlier_approvals(self):
        eng = RiskEngine(test_config(limits__max_open_positions=1), empty_book(), broad(), FRESH)
        out = eng.evaluate_all([cand("AAA"), cand("BBB", sector="IT")])
        self.assertEqual([c.status for c in out], ["approved", "rejected"])

    def test_kill_switch_drawdown(self):
        book = empty_book()
        book.equity = pd.Series([100.0, 120.0, 100.0])                 # -16.7% from peak
        c = RiskEngine(test_config(), book, broad(), FRESH).evaluate(cand())
        self.assertEqual(first_fail(c)["check"], "kill_switch")


class GatesAndRules(unittest.TestCase):
    def test_stale_data_rejected(self):
        q = {"datasets": {**FRESH["datasets"], "equity_eod": {"status": "STALE", "lag_sessions": 3, "last": "x"}}}
        c = RiskEngine(test_config(), empty_book(), broad(), q).evaluate(cand())
        self.assertEqual(first_fail(c)["check"], "data_freshness")

    def test_grade_floor(self):
        c = RiskEngine(test_config(), empty_book(), broad(), FRESH).evaluate(cand(grade_score=0.2))
        self.assertEqual(first_fail(c)["check"], "confidence")

    def test_results_near_entry_rejected_and_in_hold_halved(self):
        ev = pd.DataFrame({"symbol": ["AAA", "BBB"], "event_date": ["20260922", "20261010"],
                           "purpose": ["results", "results"]})
        eng = RiskEngine(test_config(), empty_book(), broad(), FRESH, events=ev, today="20260921")
        near = eng.evaluate(cand("AAA", unit_risk=10, hold_until="20261020"))
        later = eng.evaluate(cand("BBB", unit_risk=10, hold_until="20261020", sector="IT"))
        self.assertEqual(first_fail(near)["check"], "event_risk")
        self.assertEqual(later.sizing.quantity, 250)

    def test_naked_short_option_rejected(self):
        legs = [Leg("CE", "AAA", "SELL", 10, "20261027", 110, 1, 500)]
        c = cand(engine="fno", segment="fno", legs=legs)
        eng = RiskEngine(test_config(), empty_book(), broad(), FRESH, eligible_fno={"AAA"})
        self.assertEqual(first_fail(eng.evaluate(c))["check"], "strategy_rules")

    def test_spread_is_defined_risk(self):
        legs = [Leg("CE", "AAA", "BUY", 10, "20261027", 100, 1, 500), Leg("CE", "AAA", "SELL", 4, "20261027", 110, 1, 500)]
        c = cand(engine="fno", segment="fno", legs=legs, unit_risk=3_000)
        out = RiskEngine(test_config(), empty_book(), broad(), FRESH, eligible_fno={"AAA"}).evaluate(c)
        self.assertEqual(out.status, "approved")

    def test_fno_requires_contract_list(self):
        c = cand(engine="fno", segment="fno")
        out = RiskEngine(test_config(), empty_book(), broad(), FRESH, eligible_fno=set()).evaluate(c)
        self.assertEqual(first_fail(out)["check"], "instrument")

    def test_intraday_short_cannot_be_overnight(self):
        cfg = test_config()
        cfg["engines"]["intraday_short"]["enabled"] = True
        c = cand(engine="intraday_short", direction="short", overnight=True)
        out = RiskEngine(cfg, empty_book(), broad(), FRESH, eligible_fno={"AAA"}).evaluate(c)
        self.assertEqual(first_fail(out)["check"], "strategy_rules")

    def test_approval_never_comes_from_engine(self):
        c = cand()
        c.status = "approved"                    # an engine trying to self-approve
        out = RiskEngine(cfgmod.load(path="__x__.toml"), empty_book(), broad(), FRESH).evaluate(c)
        self.assertEqual(out.status, "rejected")


if __name__ == "__main__":
    unittest.main()
