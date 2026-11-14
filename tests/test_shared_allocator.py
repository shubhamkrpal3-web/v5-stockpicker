"""Shared allocator (v5_modules/allocator.py): sizing, ordering, exposure caps, duplicate holdings and fail-closed
behaviour, and its two connections (research risk engine, backtest --entry-allocation shared).

TEST VALUES ONLY: every policy number below exists to exercise the rules and is not a suggestion of suitable
amounts. The shipped configuration stays unset."""
import random
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))

import allocator as al  # noqa: E402
import backtest_regime as br  # noqa: E402
from portfolio_accounting import Book, Holding  # noqa: E402
from research_system import config as cfgmod  # noqa: E402
from research_system.risk_engine import RiskEngine  # noqa: E402
from tests.helpers import FRESH, broad, cand, empty_book, test_config  # noqa: E402

POLICY = al.Policy(configured=True, capital_inr=1_000_000, per_trade_max_loss_inr=5_000, max_gross_exposure_pct=1.0,
                   max_net_exposure_pct=1.0, max_position_pct=0.10, max_sector_pct=0.30, max_open_positions=20,
                   max_positions_by_engine={"swing": 15, "investment": 10, "fno": 4}, max_participation_adv=0.02,
                   margin_inr=200_000, source="tests")
EMPTY = al.BookState()


def req(symbol="AAA", priority=0.5, price=100.0, stop=4.0, gap=2.0, costs=1.0, sector="IT", adv=1e9, engine="swing",
        **kw) -> al.Request:
    return al.Request(key=kw.pop("key", f"{engine}:{symbol}"), symbol=symbol, engine=engine, priority=priority,
                      price=price, stop_loss=stop, gap_allowance=gap, costs=costs, sector=sector, adv_inr=adv, **kw)


def held(symbol, notional, sector="IT", engine="swing", **kw) -> al.Exposure:
    return al.Exposure(symbol, engine, notional, sector, kw.pop("segment", "cash"), kw.pop("underlying", symbol), **kw)


def failed(d: al.Decision) -> str:
    return d.failed[0] if d.failed else ""


class Sizing(unittest.TestCase):
    def test_size_from_loss_budget_over_stop_gap_and_costs(self):
        d = al.size_one(req(stop=4.0, gap=2.0, costs=1.0), EMPTY, POLICY, "RISK_ON")
        self.assertEqual((d.status, d.quantity, d.unit_loss_inr), ("approved", 5_000 // 7, 7.0))   # 714, not 5000/4
        self.assertLessEqual(d.planned_loss_inr, 5_000)
        self.assertIn("stop 4.00 + gap 2.00 + costs 1.00", d.notes[0])

    def test_gap_allowance_and_costs_shrink_the_size(self):
        bare = al.size_one(req(stop=4.0, gap=0.0, costs=0.0), EMPTY, POLICY, "RISK_ON").quantity
        full = al.size_one(req(stop=4.0, gap=2.0, costs=1.0), EMPTY, POLICY, "RISK_ON").quantity
        self.assertEqual((bare, full), (1_000, 714))      # bare is held to the 10% position cap; full by the budget

    def test_float_noise_in_the_parts_does_not_lose_a_share(self):
        d = al.size_one(req(stop=0.75, gap=0.2, costs=0.05, price=10.0), EMPTY, POLICY, "RISK_ON")
        self.assertEqual(d.quantity, 5_000)

    def test_multiplier_and_engine_cap_only_reduce(self):
        self.assertEqual(al.size_one(req(size_multiplier=0.5), EMPTY, POLICY, "RISK_ON").quantity, 357)
        self.assertEqual(al.size_one(req(max_loss_inr=700.0), EMPTY, POLICY, "RISK_ON").quantity, 100)
        self.assertEqual(failed(al.size_one(req(size_multiplier=1.5), EMPTY, POLICY, "RISK_ON")), "allocation_inputs")

    def test_position_liquidity_capital_and_cash_caps(self):
        cheap = dict(stop=0.5, gap=0.0, costs=0.0)                                  # budget alone: 10,000 shares
        self.assertEqual(al.size_one(req(**cheap), EMPTY, POLICY, "RISK_ON").quantity, 1_000)     # 10% position cap
        self.assertEqual(al.size_one(req(adv=2_500_000, **cheap), EMPTY, POLICY, "RISK_ON").quantity, 500)  # 2% ADV
        busy = al.BookState(exposures=(held("X", 960_000, "Energy"),), claims=(al.Claim("Y", "swing", 10_000),))
        d = al.size_one(req(**cheap), busy, POLICY, "RISK_ON")                       # 30,000 of capital left
        self.assertEqual((d.quantity, d.notes[-1]), (300, "capped by available_capital: 1000 -> 300"))
        poor = al.BookState(available_cash=12_345.0)
        self.assertEqual(al.size_one(req(**cheap), poor, POLICY, "RISK_ON").quantity, 123)

    def test_below_one_unit_rejected(self):
        self.assertEqual(failed(al.size_one(req(stop=9_000.0), EMPTY, POLICY, "RISK_ON")), "sizing")

    def test_backtest_capital_base_keeps_the_loss_ratio(self):
        p = POLICY.at_capital(2_000_000)
        self.assertEqual((p.capital_inr, p.per_trade_max_loss_inr), (2_000_000, 10_000))
        self.assertTrue(al.Policy().at_capital(2_000_000).problems())                 # unset stays unset


class Ordering(unittest.TestCase):
    def requests(self):
        rs = [("CCC", 0.9, "swing"), ("AAA", 0.5, "swing"), ("BBB", 0.5, "swing"), ("DDD", 0.5, "investment"),
              ("EEE", float("nan"), "swing"), ("FFF", 0.7, "swing")]
        return [req(s, p, engine=e, sector=f"sector {s}") for s, p, e in rs]

    def test_order_does_not_depend_on_arrival(self):
        expected = ["CCC", "FFF", "DDD", "AAA", "BBB", "EEE"]      # priority, then engine, then symbol; NaN last
        rs = self.requests()
        for seed in range(5):
            random.Random(seed).shuffle(rs)
            out = al.allocate(rs, EMPTY, POLICY, "RISK_ON")
            self.assertEqual([d.symbol for d in out], expected)
            self.assertEqual([d.quantity for d in out], [714] * 6)

    def test_earlier_approvals_use_up_room_for_later_ones(self):
        tight = replace(POLICY, max_gross_exposure_pct=0.10)                        # 100,000 gross in RISK_ON
        out = al.allocate([req("BBB", 0.5), req("AAA", 0.5), req("ZZZ", 0.9)], EMPTY, tight, "RISK_ON")
        self.assertEqual([(d.symbol, d.status, d.quantity) for d in out],
                         [("ZZZ", "approved", 714), ("AAA", "approved", 286), ("BBB", "rejected", 0)])
        self.assertEqual(failed(out[2]), "gross_exposure")

    def test_risk_engine_breaks_ties_by_engine_and_symbol(self):
        for order in (["BBB", "AAA"], ["AAA", "BBB"]):
            eng = RiskEngine(test_config(limits__max_open_positions=1), empty_book(), broad(), FRESH)
            out = eng.evaluate_all([cand(s, sector=s) for s in order])
            self.assertEqual([(c.symbol, c.status) for c in out], [("AAA", "approved"), ("BBB", "rejected")])


class ExposureCaps(unittest.TestCase):
    def test_regime_scales_the_gross_cap_once(self):
        book = al.BookState(exposures=(held("OLD", 350_000, "Energy"),))
        off = al.size_one(req(stop=0.5, gap=0.0, costs=0.0), book, POLICY, "RISK_OFF")   # cap 40% -> 50,000 room
        self.assertEqual((off.quantity, off.checks[-2][:2]), (500, ("gross_exposure", "ADJUST")))
        # with room to spare the regime does not change the size: it is not applied to the per-trade budget too
        on = al.size_one(req(), EMPTY, POLICY, "RISK_ON")
        self.assertEqual(al.size_one(req(), EMPTY, POLICY, "RISK_OFF").quantity, on.quantity)

    def test_legacy_rule_also_scales_each_size_by_the_regime(self):
        intended = {s: 20_000.0 for s in "ABCDEFGHIJ"}                           # ten names, 200,000 intended
        on = al.legacy_allocate(intended, list("AB"), 100_000, 1.0, 1e9)
        off = al.legacy_allocate(intended, list("AB"), 100_000, 0.4, 1e9)
        self.assertEqual((on["A"], off["A"]), (10_000.0, 4_000.0))                # renormalised by the regime

    def test_net_cap_rejects(self):
        p = replace(POLICY, max_net_exposure_pct=0.10)
        book = al.BookState(exposures=(held("OLD", 90_000, "Energy"),))
        self.assertEqual(failed(al.size_one(req(), book, p, "RISK_ON")), "net_exposure")

    def test_sector_cap_counts_the_book_and_earlier_approvals(self):
        book = al.BookState(exposures=(held("OLD", 200_000, "IT"),))
        out = al.allocate([req("AAA", 0.9), req("BBB", 0.8), req("CCC", 0.7, sector="Energy")], book, POLICY, "RISK_ON")
        self.assertEqual([(d.symbol, d.status, failed(d)) for d in out],
                         [("AAA", "approved", ""), ("BBB", "rejected", "sector"), ("CCC", "approved", "")])

    def test_position_counts(self):
        full = al.BookState(exposures=tuple(held(f"H{i}", 1_000, "Energy") for i in range(3)))
        self.assertEqual(failed(al.size_one(req(), full, replace(POLICY, max_open_positions=3), "RISK_ON")),
                         "position_count")
        p = replace(POLICY, max_positions_by_engine={"swing": 3})
        self.assertEqual(failed(al.size_one(req(), full, p, "RISK_ON")), "position_count")
        self.assertEqual(failed(al.size_one(req(engine="investment"), EMPTY, p, "RISK_ON")), "position_count")


class DuplicateHoldings(unittest.TestCase):
    def test_held_symbol_or_underlying_rejected(self):
        book = al.BookState(exposures=(held("OPT1", 10_000, "IT", segment="fno", underlying="AAA"),))
        self.assertEqual(failed(al.size_one(req("AAA"), book, POLICY, "RISK_ON")), "duplicate")

    def test_claims_follow_the_ledger(self):
        book = al.BookState(claims=(al.Claim("AAA", "swing", 5_000.0), al.Claim("BBB", "swing", 50.0, False, False)))
        self.assertEqual(failed(al.size_one(req("AAA"), book, POLICY, "RISK_ON")), "duplicate")   # consideration owed
        self.assertEqual(al.size_one(req("BBB"), book, POLICY, "RISK_ON").status, "approved")     # only a dividend owed

    def test_same_symbol_twice_in_one_batch(self):
        out = al.allocate([req("AAA", 0.5), req("AAA", 0.9, engine="investment")], EMPTY, POLICY, "RISK_ON")
        self.assertEqual([(d.engine, d.status, failed(d)) for d in out],
                         [("investment", "approved", ""), ("swing", "rejected", "duplicate")])

    def test_backtest_book_holdings_are_duplicates(self):
        st = br.accounting_book_state(book_with([("AAA", "open", "shares", 10, 100.0, "20240105")]), {"AAA": "IT"})
        self.assertEqual(failed(al.size_one(req("AAA"), st, POLICY, "RISK_ON")), "duplicate")


class FailClosed(unittest.TestCase):
    def test_default_policy_approves_nothing(self):
        out = al.allocate([req("AAA"), req("BBB")], EMPTY, al.Policy(), "RISK_ON")
        self.assertEqual({(d.status, failed(d)) for d in out}, {("rejected", "risk_budget")})
        problems = al.Policy().problems()
        self.assertIn("policy not marked configured", problems)
        self.assertEqual(len(problems), 1 + len(al.Policy.REQUIRED))

    def test_shipped_example_config_gives_an_unset_policy(self):
        cfg = cfgmod.load(path="__does_not_exist__.toml")
        p = al.policy_from_risk_config(cfg)
        self.assertIn("per_trade_max_loss_inr not set", p.problems())
        self.assertEqual(failed(al.size_one(req(), EMPTY, p, "RISK_ON")), "risk_budget")
        self.assertTrue(RiskEngine(cfg, empty_book(), broad(), FRESH).policy.problems())

    def test_one_missing_limit_is_enough(self):
        for field in al.Policy.REQUIRED:
            for bad in (None, 0, -1, float("nan")):
                p = replace(POLICY, **{field: bad})
                self.assertEqual(failed(al.size_one(req(), EMPTY, p, "RISK_ON")), "risk_budget", (field, bad))

    def test_incomplete_book(self):
        for book in (al.BookState(problems=("ledger cash: does not reconcile",)),
                     al.BookState(exposures=(held("OLD", None),)),
                     al.BookState(exposures=(held("OLD", float("nan")),)),
                     al.BookState(claims=(al.Claim("OLD", "swing", float("nan")),)),
                     al.BookState(available_cash=float("nan"))):
            self.assertEqual(failed(al.size_one(req(), book, POLICY, "RISK_ON")), "book_view")

    def test_unknown_regime(self):
        for regime in (None, "", "SIDEWAYS"):
            self.assertEqual(failed(al.size_one(req(), EMPTY, POLICY, regime)), "regime")

    def test_incomplete_request_data(self):
        cases = {"price": dict(price=None), "price nan": dict(price=float("nan")), "price 0": dict(price=0.0),
                 "stop": dict(stop=None), "stop 0": dict(stop=0.0), "gap": dict(gap=None), "costs": dict(costs=None),
                 "negative costs": dict(costs=-1.0), "sector": dict(sector=None), "sector unknown": dict(sector="Unknown"),
                 "adv": dict(adv=None), "adv 0": dict(adv=0.0), "direction": dict(direction="sideways"),
                 "stated loss": dict(stated_unit_loss=6.0)}
        for name, kw in cases.items():
            d = al.size_one(req(**kw), EMPTY, POLICY, "RISK_ON")
            self.assertEqual((d.status, failed(d)), ("rejected", "allocation_inputs"), name)
        self.assertEqual(al.size_one(req(stated_unit_loss=7.004), EMPTY, POLICY, "RISK_ON").status, "approved")

    def test_derivatives_need_margin(self):
        r = req(segment="fno", engine="fno", unit_margin=3_000.0, unit_capital=3_000.0, adv=None)
        self.assertEqual(al.size_one(r, EMPTY, POLICY, "RISK_ON").status, "approved")
        self.assertEqual(failed(al.size_one(r, EMPTY, replace(POLICY, margin_inr=0), "RISK_ON")), "allocation_inputs")

    def test_risk_engine_rejects_unstated_loss_parts(self):
        eng = RiskEngine(test_config(), empty_book(), broad(), FRESH)
        inv = cand("INV", engine="investment", unit_risk=7.0, loss_parts={"stop": 6.5, "gap": None, "costs": 0.5})
        self.assertIn("gap allowance per unit not stated", eng.evaluate(inv).risk_checks[-1]["detail"])
        none = eng.evaluate(cand("NOP", loss_parts=None))
        self.assertEqual((none.status, none.risk_checks[-1]["check"]), ("rejected", "allocation_inputs"))
        unknown = eng.evaluate(cand("UNK", sector="Unknown"))
        self.assertEqual(unknown.risk_checks[-1]["check"], "allocation_inputs")

    def test_risk_engine_rejects_a_missing_regime(self):
        c = RiskEngine(test_config(), empty_book(), {"broad": {"risk_appetite_z": 0.0}}, FRESH).evaluate(cand())
        self.assertEqual((c.status, c.risk_checks[-1]["check"]), ("rejected", "regime"))

    def test_backtest_unpriced_entitlement_leaves_the_book_incomplete(self):
        bk = book_with([("NEWCO", "pending", "shares", 5, 0.0, "")])
        st = br.accounting_book_state(bk, {})
        self.assertEqual(failed(al.size_one(req(), st, POLICY, "RISK_ON")), "book_view")


# ------------------------------------------------------------------ backtest path
def book_with(rows) -> Book:
    bk = Book(1_000_000, lambda s, d: None, lambda s, d: None, lambda n, side, s, d: 0.0)
    for i, (sym, state, kind, qty, mark, mark_date) in enumerate(rows):
        bk.holdings.append(Holding(hid=f"T{i}", symbol=sym, qty=qty, state=state, kind=kind, last_mark=mark,
                                   last_mark_date=mark_date))
    return bk


def synthetic_market():
    """Three flat stocks, ranked A > B > C; every regime RISK_ON. Close A 100, B 50, C 200; ATR 2% of close.
    Sixteen sessions from Mon 1 Jan 2024; the run starts on Fri 12 Jan so Mon 15 Jan has 20-day traded value."""
    days = pd.bdate_range("2024-01-01", periods=16)
    px = {"A": 100.0, "B": 50.0, "C": 200.0}
    close = pd.DataFrame({s: [p] * len(days) for s, p in px.items()}, index=days)
    raw = {"OPEN": close.copy(), "HIGH": close * 1.01, "LOW": close * 0.99, "CLOSE": close.copy()}
    W = {"RAW": raw, "CLOSE": close.copy(), "TRADED_VALUE": close * 0 + 1e8}
    rank = pd.DataFrame({"A": [3.0] * len(days), "B": [2.0] * len(days), "C": [1.0] * len(days)}, index=days)
    F = {"ATR14": close * 0.02, "MOM_3_1": rank, "MOM_6_1": rank, "VOL90": -rank}
    reg = pd.DataFrame({"state": ["RISK_ON"] * len(days)}, index=days)
    return W, F, reg


class BacktestPath(unittest.TestCase):
    SECTORS = {"A": "IT", "B": "IT", "C": "Pharma"}

    def run_sim(self, **kw):
        W, F, reg = synthetic_market()
        return br.simulate(W, F, reg, "2024-01-12", top_n=3, max_pos=3, capital=1_000_000, cost_bps=35.0, **kw)

    def opened(self, sim) -> dict:
        t = sim["holdings_end"]
        return dict(zip(t["symbol"], t["qty"])) if len(t) else {}

    def test_unset_policy_opens_nothing(self):
        sim = self.run_sim(entry_allocation="shared", sectors=self.SECTORS, gap_allowance_atr=1.0)
        self.assertEqual(self.opened(sim), {})
        self.assertEqual(set(sim["allocations"]["check"]), {"risk_budget"})

    def test_shared_sizes_from_loss_budget_with_gap_and_costs(self):
        sim = self.run_sim(entry_allocation="shared", policy=POLICY, sectors=self.SECTORS, gap_allowance_atr=1.0)
        # unit loss = 2 ATR + 1 ATR + 2 x 35 bps of the close: A 6.70, B 3.35, C 13.40
        self.assertEqual(self.opened(sim), {"A": 746.0, "B": 1492.0, "C": 373.0})
        self.assertEqual(sim["holdings_end"]["entry_date"].unique().tolist(), ["20240115"])

    def test_shared_fails_closed_without_sector_or_gap_allowance(self):
        sim = self.run_sim(entry_allocation="shared", policy=POLICY, sectors={"A": "IT"}, gap_allowance_atr=1.0)
        self.assertEqual(self.opened(sim), {"A": 746.0})
        sim = self.run_sim(entry_allocation="shared", policy=POLICY, sectors=self.SECTORS)
        self.assertEqual(self.opened(sim), {})
        self.assertTrue(sim["allocations"]["detail"].str.contains("gap allowance per unit not stated").all())

    def test_legacy_modes_still_available(self):
        sim = self.run_sim(entry_allocation="rank_order")
        # size_positions formula: 1% x 1,000,000 / (2 x ATR) x close = 250,000 each, held to the 10% cap (100,000)
        self.assertEqual(self.opened(sim), {"A": 1000.0, "B": 2000.0, "C": 500.0})
        self.assertEqual(set(sim["allocations"]["mode"]), {"rank_order"})


if __name__ == "__main__":
    unittest.main()
