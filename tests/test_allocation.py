"""Weekly entry allocation in the backtest (backtest_regime.allocate_entries), checked against the sizing formula
of v5_signal_generator.size_positions. This is a formula check, not a match with the paper or research paths."""
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))

from backtest_regime import ENTRY_ALLOCATIONS, allocate_entries  # noqa: E402
import v5_signal_generator as gen  # noqa: E402

EQUITY = 200000.0
# ranked best first; risk-parity sizes (equity x 1% / (2 x ATR) x close), none at the 10% cap
CLOSE = {"A": 100.0, "B": 250.0, "C": 400.0, "D": 1000.0, "E": 50.0}
ATR = {"A": 8.0, "B": 20.0, "C": 40.0, "D": 80.0, "E": 3.0}
INTENDED = {s: min(EQUITY * 0.01 / (2 * ATR[s]) * CLOSE[s], EQUITY * 0.10) for s in CLOSE}


class EntryAllocation(unittest.TestCase):
    def test_small_leftover_goes_to_the_best_pick_not_spread_thin(self):
        s = allocate_entries(INTENDED, ["A", "B", "C"], EQUITY, 1.0, budget=3000.0, method="rank_order")
        self.assertEqual(s, {"A": 3000.0})                       # B and C wait for a later week; their slots stay free

    def test_original_rule_spreads_the_leftover_over_every_free_slot(self):
        s = allocate_entries(INTENDED, ["A", "B", "C"], EQUITY, 1.0, budget=3000.0, method="pro_rata_leftover")
        self.assertEqual(list(s), ["A", "B", "C"])
        self.assertAlmostEqual(sum(s.values()), 3000.0)
        self.assertAlmostEqual(s["C"], 3000.0 * INTENDED["C"] / (INTENDED["A"] + INTENDED["B"] + INTENDED["C"]))

    def test_rank_order_uses_the_size_positions_formula(self):
        # ample budget: each pick gets size_positions' SIZE_INR for the same capital base (top-N list renormalised
        # to equity x gross)
        top = pd.DataFrame({"SYMBOL": list(CLOSE), "CLOSE": list(CLOSE.values()), "ATR14": list(ATR.values())})
        live = gen.size_positions(top, EQUITY, 0.01, 0.10, 0.25).set_index("SYMBOL")["SIZE_INR"]
        s = allocate_entries(INTENDED, list(CLOSE), EQUITY, 0.25, budget=1e9, method="rank_order")
        self.assertEqual(list(s), list(CLOSE))
        for sym, v in s.items():
            self.assertAlmostEqual(v, live[sym], delta=0.5)      # live rounds SIZE_INR to whole rupees

    def test_rank_order_preserves_ranking_and_budget(self):
        for budget in (0.0, 5000.0, 20000.0, 35000.0, 60000.0):
            s = allocate_entries(INTENDED, ["A", "B", "C", "D"], EQUITY, 0.25, budget, "rank_order")
            self.assertEqual(list(s), ["A", "B", "C", "D"][:len(s)])          # a funded prefix of the ranking
            self.assertLessEqual(sum(s.values()), budget + 1e-6)
            full = {k: v * min(1.0, EQUITY * 0.25 / sum(INTENDED.values())) for k, v in INTENDED.items()}
            for k in list(s)[:-1]:
                self.assertAlmostEqual(s[k], full[k])                   # only the last funded pick can be partial

    def test_unknown_method_rejected(self):
        self.assertEqual(ENTRY_ALLOCATIONS, ("rank_order", "pro_rata_leftover", "shared"))
        for bad in ("pro_rata", "shared"):                # "shared" goes through allocator.allocate, not this rule
            with self.assertRaises(ValueError):
                allocate_entries(INTENDED, ["A"], EQUITY, 1.0, 1000.0, bad)


if __name__ == "__main__":
    unittest.main()
