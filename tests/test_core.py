import math
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))

from corporate_actions import adjust_long, adjust_wide, parse_subject  # noqa: E402
from research_system import company_graph, confidence, paths  # noqa: E402
from research_system.data.calendar import IST  # noqa: E402
from research_system.engines import intraday_short  # noqa: E402
from research_system.engines.fno import option_charges  # noqa: E402
from research_system.market_state import relationship, session_returns  # noqa: E402
from research_system.pricing import black76, implied_vol  # noqa: E402


class CorporateActions(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_subject("Bonus 1:1"), ("bonus", 0.5))
        self.assertEqual(parse_subject("Bonus 4:1"), ("bonus", 0.2))
        k, f = parse_subject("Face Value Split (Sub-Division) - From Rs10/- Per Share To Re 1/- Per Share")
        self.assertEqual(k, "split"); self.assertAlmostEqual(f, 0.1)
        self.assertEqual(parse_subject("Bonus - 1 Debenture For 1 Equity Share Held"), (None, None))
        self.assertEqual(parse_subject("Interim Dividend - Rs 5 Per Share"), (None, None))

    def _frame(self):
        d = pd.date_range("2024-01-01", periods=6, freq="B")
        df = pd.DataFrame({"SYMBOL": "X", "DATE_DT": d, "OPEN": [100, 100, 50, 50, 5, 5.0],
                           "HIGH": [101, 101, 51, 51, 5.1, 5.1], "LOW": [99, 99, 49, 49, 4.9, 4.9],
                           "CLOSE": [100, 100, 50, 50, 5, 5.0], "VOLUME": [10, 10, 20, 20, 200, 200.0]})
        ev = pd.DataFrame({"SYMBOL": ["X", "X"], "EX_DATE": [d[2].strftime("%Y%m%d"), d[4].strftime("%Y%m%d")],
                           "FACTOR": [0.5, 0.1]})
        return d, df, ev

    def test_long_and_wide_agree(self):
        d, df, ev = self._frame()
        a = adjust_long(df, ev)
        self.assertTrue(np.allclose(a["CLOSE"], 5) and np.allclose(a["VOLUME"], 200))
        W = {c: df.pivot(index="DATE_DT", columns="SYMBOL", values=c) for c in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]}
        self.assertTrue(np.allclose(adjust_wide(W, ev)["CLOSE"]["X"], 5))

    def test_future_ex_date_is_ignored(self):
        d, df, ev = self._frame()
        ev = pd.DataFrame({"SYMBOL": ["X"], "EX_DATE": ["20300101"], "FACTOR": [0.5]})
        self.assertTrue(np.allclose(adjust_long(df, ev)["CLOSE"], df["CLOSE"]))


class Pricing(unittest.TestCase):
    def test_parity_and_iv(self):
        F, K, T, r = 1000.0, 1050.0, 0.1, 0.065
        c, p = black76(F, K, T, r, 0.3, True), black76(F, K, T, r, 0.3, False)
        self.assertAlmostEqual(c.price - p.price, math.exp(-r * T) * (F - K), places=8)
        self.assertAlmostEqual(implied_vol(c.price, F, K, T, r, True), 0.3, places=4)
        self.assertIsNone(implied_vol(0.0, F, K, T, r, True))
        self.assertTrue(0 < c.delta < 1 and -1 < p.delta < 0)

    def test_option_charges_positive(self):
        self.assertGreater(option_charges(40, 15, 500), 80)


class Confidence(unittest.TestCase):
    def test_caps_and_missing(self):
        g = confidence.grade({k: (0.95, "x") for k in confidence.WEIGHTS}, caps=[("C", "test cap")])
        self.assertEqual(g.grade, "C")
        self.assertIn("max C: test cap", g.caps)
        g2 = confidence.grade({})
        self.assertEqual(g2.grade, "E")                     # missing evidence is not neutral
        self.assertIsNone(g2.probability)


class MarketState(unittest.TestCase):
    def test_gap_returns_are_nan(self):
        sess = pd.date_range("2024-01-01", periods=10, freq="B")
        lvl = pd.Series([100, 101, 102, np.nan, np.nan, np.nan, 200, 201, 202, 203.0], index=sess)
        r = session_returns(lvl, sess)
        self.assertTrue(np.isnan(r.loc[sess[6]]))           # no 2-session jump
        self.assertAlmostEqual(r.loc[sess[7]], 201 / 200 - 1)

    def test_relationship_labels(self):
        rng = np.random.default_rng(0)
        idx = pd.date_range("2018-01-01", periods=1500, freq="B")
        x = pd.Series(rng.normal(0, 1, 1500), index=idx)
        y_strong = 0.6 * x + pd.Series(rng.normal(0, 1, 1500), index=idx)
        y_none = pd.Series(rng.normal(0, 1, 1500), index=idx)
        self.assertEqual(relationship(y_strong, x)["label"], "stable")
        self.assertIn(relationship(y_none, x)["label"], ("weak", "unstable"))
        self.assertEqual(relationship(y_none.head(50), x.head(50))["label"], "insufficient")


class Intraday(unittest.TestCase):
    def _bars(self, path):
        ts = pd.date_range("2026-09-21 09:15", periods=len(path), freq="5min")
        c = pd.Series(path, dtype=float)
        return pd.DataFrame({"ts": ts, "open": c.shift(1).fillna(c.iloc[0]), "high": c + 0.3, "low": c - 0.3,
                             "close": c, "volume": 1000.0})

    def test_breakdown_detected(self):
        s = self._bars([100, 100.5, 100.2, 99.8, 99.3, 98.9, 98.4])
        i = self._bars([100, 100.1, 100.2, 100.2, 100.3, 100.3, 100.4])
        sig = intraday_short.detect(s, i)
        self.assertIsNotNone(sig)
        self.assertGreater(sig["stop"], sig["entry"])

    def test_no_feed_no_trade(self):
        class Ctx:
            cfg = {"engines": {"intraday_short": {"enabled": True}}}
        c, nt = intraday_short.run(Ctx(), feed=intraday_short.NullFeed(), now=datetime(2026, 9, 21, 10, 0, tzinfo=IST))
        self.assertEqual(c, [])
        self.assertIn("no live intraday data feed", nt[0].reasons[0])


class AuditReplay(unittest.TestCase):
    def setUp(self):
        from research_system import audit
        self.audit = audit
        self._dir, self._out = paths.AUDIT_DIR, audit.OUTCOMES
        self.tmp = tempfile.TemporaryDirectory()
        paths.AUDIT_DIR = Path(self.tmp.name)
        audit.OUTCOMES = Path(self.tmp.name) / "outcomes.parquet"

    def tearDown(self):
        paths.AUDIT_DIR, self.audit.OUTCOMES = self._dir, self._out
        self.tmp.cleanup()

    def test_stop_gap_fills_at_open_and_missing_hold(self):
        import json
        d = pd.date_range("2026-09-14", periods=10, freq="B")
        C = pd.DataFrame({"AAA": [100, 101, 102, 103, 104, 90, 91, 92, 93, 94.0]}, index=d)
        O = C.shift(1).fillna(100).copy(); O.iloc[5] = 88.0            # gap below the stop
        H, L = C + 1, C - 1
        rec = {"run_at": "2026-09-14T18:00:00", "type": "candidate", "id": "x1", "engine": "swing",
               "symbol": "AAA", "setup": "s", "direction": "long", "status": "approved",
               "data_timestamp": "EOD 2026-09-14 (built x)", "hold_until": None,   # -> NaN in the frame
               "plan": {"entry_low": 95, "entry_high": 105, "stop": 95.0, "targets": [120.0]},
               "confidence": {"grade": "C", "score": 0.6}}
        (paths.AUDIT_DIR / "20260914.jsonl").write_text(json.dumps(rec) + "\n")
        o = self.audit.reconcile(O, H, L, C)
        r = o.iloc[0]
        self.assertTrue(r["filled"])
        self.assertEqual(r["exit_reason"], "stop")
        self.assertAlmostEqual(r["exit"], 88.0)            # filled at the gap open, not the stop


class Graph(unittest.TestCase):
    def setUp(self):
        self._orig = paths.GRAPH_DB
        self.tmp = tempfile.TemporaryDirectory()
        paths.GRAPH_DB = Path(self.tmp.name) / "g.sqlite"

    def tearDown(self):
        paths.GRAPH_DB = self._orig
        self.tmp.cleanup()

    def test_disclosed_edge_needs_source(self):
        with self.assertRaises(ValueError):
            company_graph.add_edge("AAA", "company", "supplier_of", "BBB", "company", disclosed=True)

    def test_transmission_paths(self):
        company_graph.add_edge("crude oil", "commodity", "input_cost", "AAA", "company", channel="costs",
                               evidence_url="https://example.org/ar", disclosed=True, confidence=0.9)
        company_graph.add_edge("AAA", "company", "supplier_of", "BBB", "company", channel="revenue",
                               confidence=0.5)
        p = company_graph.transmission_paths("commodity:crude oil", depth=2)
        self.assertEqual(list(p["company"]), ["AAA", "BBB"])
        self.assertTrue(p.iloc[0]["all_disclosed"])
        self.assertFalse(p.iloc[1]["all_disclosed"])
        self.assertAlmostEqual(p.iloc[1]["path_confidence"], 0.45)


if __name__ == "__main__":
    unittest.main()
