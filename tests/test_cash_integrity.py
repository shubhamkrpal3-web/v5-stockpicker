"""Cash integrity of the paper ledger: unrecorded costs stay explicitly unknown (never read as zero),
and the cash identity accounts for every cash movement the accounting engine produces. TEST VALUES ONLY."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))

import ledger_view as lv  # noqa: E402
import paper_trade_manager as ptm  # noqa: E402
from portfolio_accounting import CLOSED, OPEN, PENDING, Book, Holding  # noqa: E402
from tests.test_accounting import run_day, sec_events  # noqa: E402

SC = pd.DataFrame(columns=["OLD", "NEW", "EFFECTIVE_DATE"])
# an open row written by the older ledger before the cost model: no entry cost recorded
OLD_ROW = {"symbol": "JBCHEPHARM", "signal_date": "20260710", "entry_date": "2026-07-13", "entry_price": 2415.2,
           "qty": 3.0, "size_inr": 7245.6, "initial_stop": 2300.0, "current_stop": 2350.0, "highest_close": 2500.0,
           "atr_at_entry": 40.0, "status": "open", "entry_cost_inr": np.nan}


def reload(rows: pd.DataFrame) -> pd.DataFrame:
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "positions.csv"
        rows.reindex(columns=ptm.POSITIONS_COLS).to_csv(p, index=False)
        return ptm._load_positions(p)


class UnknownCosts(unittest.TestCase):
    def test_blank_entry_cost_stays_unknown_across_reloads_and_rewrites(self):
        rows = pd.DataFrame([OLD_ROW])
        for cycle in range(3):                                         # load -> rewrite, three times
            back = reload(rows)
            r = next(back.itertuples(index=False))
            h = ptm._holding_from_row(r, 0, SC)
            self.assertEqual(h.cost_basis, 7245.6, cycle)              # the recorded part only
            self.assertIn("entry cost not recorded (entered 2026-07-13)", h.cost_note, cycle)
            row = ptm._row_from_holding(h, r._asdict())
            self.assertEqual((row["cost_basis"], row["cost_known_part"]), ("", 7245.6), cycle)
            self.assertTrue(pd.isna(row["entry_cost_inr"]), cycle)     # never written as 0
            rows = pd.DataFrame([row])
        self.assertNotIn("0.0", str(rows.loc[0, ["entry_cost_inr", "cost_basis"]].tolist()))

    def test_zero_entry_fee_counts_only_with_its_basis(self):
        bare = ptm._holding_from_row(next(reload(pd.DataFrame([OLD_ROW | {"entry_cost_inr": 0.0}])).itertuples(index=False)), 0, SC)
        self.assertEqual(bare.cost_basis, 7245.6)
        self.assertIn("entry fee recorded as 0 with no stated basis", bare.cost_note)
        row = OLD_ROW | {"entry_cost_inr": 0.0, "entry_fee_source": "no fee charged by the paper ledger at entry (test)"}
        h = ptm._holding_from_row(next(reload(pd.DataFrame([row])).itertuples(index=False)), 0, SC)
        self.assertEqual((h.cost_basis, h.cost_note), (7245.6, ""))
        self.assertEqual(ptm._row_from_holding(h)["cost_basis"], 7245.6)

    def test_recorded_cost_is_complete(self):
        h = ptm._holding_from_row(next(reload(pd.DataFrame([OLD_ROW | {"entry_cost_inr": 8.7}])).itertuples(index=False)), 0, SC)
        self.assertEqual((round(h.cost_basis, 2), h.cost_note), (7254.3, ""))

    def test_unknown_cost_carries_to_derived_rows_and_trades(self):
        note = "entry cost not recorded (entered 2026-07-13); the cost basis excludes it"
        bars = {("A", "20201222"): (980, 990, 975, 985.65), ("A", "20201223"): (12.2, 12.5, 12.0, 12.2),
                ("A", "20201224"): (11.0, 11.2, 10.5, 10.8), ("T", "20201222"): (2400, 2420, 2390, 2408.9)}
        f = (985.65 - 974) / 985.65
        sec = sec_events([{"EVENT_ID": "A-div", "SYMBOL": "A", "TYPE": "cash_dividend", "CASH_PER_SHARE": 974,
                           "EX_DATE": "20201223", "CASH_PAY_DATE": "20210115", "PRICE_FACTOR": f, "STATUS": "verified"},
                          {"EVENT_ID": "T-m", "SYMBOL": "T", "TYPE": "merger_stock", "SUCCESSOR": "S", "RATIO_NUM": 51,
                           "RATIO_DEN": 100, "LAST_TRADE_DATE": "20201222", "TRADABLE_DATE": "20210128",
                           "FRACTION_TREATMENT": "pending", "STATUS": "verified"}])
        oe = pd.DataFrame([{"SYMBOL": "A", "EX_DATE": "20201223", "KIND": "cash_distribution", "FACTOR": f}])
        b = Book(100000.0, lambda s, d: dict(zip(("open", "high", "low", "close"), bars[(s, d)])) if (s, d) in bars else None,
                 lambda s, d: None, lambda n, side, s, d: 0.0, None, oe, sec)
        a = b.open_position("A", "20201222", 5, 980.0, None, stop=900.0)
        t = b.open_position("T", "20201222", 3, 2400.0, None, stop=2335.0)
        a.cost_note = t.cost_note = note                               # as loaded from rows with no entry cost
        for d in ("20201222", "20201223", "20201224"):
            run_day(b, d)
        rc = next(x for x in b.holdings if b.is_dividend_receivable(x))
        succ = next(x for x in b.holdings if x.symbol == "S" and x.kind == "shares")
        frac = next(x for x in b.holdings if x.kind == "fraction")
        self.assertEqual((rc.cost_note, succ.cost_note, frac.cost_note), (note, note, note))
        sale = next(x for x in b.trades if x["symbol"] == "A")
        self.assertEqual((sale["reason"], sale["cost_note"]), ("STOP_LOSS", note))
        self.assertEqual(ptm._trade_to_realized(sale)["cost_note"], note)
        rows = ledger_rows(b)
        cash_note = ptm._cash_note(rows)
        self.assertIn("A closed 2020-12-24: realised P&L excludes an unrecorded cost", cash_note)
        self.assertIn("A: entry cost not recorded", cash_note)             # the dividend receivable
        self.assertIn("S: entry cost not recorded", cash_note)

    def test_dividend_without_a_factor_has_a_known_zero_cost(self):
        bars = {("A", "20201222"): (100, 101, 99, 100), ("A", "20201223"): (98, 99, 97, 98)}
        sec = sec_events([{"EVENT_ID": "A-div", "SYMBOL": "A", "TYPE": "cash_dividend", "CASH_PER_SHARE": 2,
                           "EX_DATE": "20201223", "CASH_PAY_DATE": "20210115", "STATUS": "verified"}])
        b = Book(100000.0, lambda s, d: dict(zip(("open", "high", "low", "close"), bars[(s, d)])) if (s, d) in bars else None,
                 lambda s, d: None, lambda n, side, s, d: 0.0, None, None, sec)
        a = b.open_position("A", "20201222", 5, 100.0, None, stop=90.0)
        a.cost_note = "entry cost not recorded"
        run_day(b, "20201222"); run_day(b, "20201223")
        rc = next(x for x in b.holdings if b.is_dividend_receivable(x))
        self.assertEqual((rc.cost_basis, rc.cost_note), (0.0, ""))      # its cost is 0 by rule, not unknown

    def test_live_style_ledger_stays_unverifiable_after_a_rewrite(self):
        closed = {"symbol": "ONGC", "status": "closed", "entry_date": "2026-06-15", "size_inr": 5209.05,
                  "realized_pnl_inr": -110.25, "exit_date": "2026-06-26"}
        rows = pd.DataFrame([OLD_ROW, closed])
        r = next(reload(rows.iloc[[0]]).itertuples(index=False))
        rewritten = pd.DataFrame([ptm._row_from_holding(ptm._holding_from_row(r, 0, SC), r._asdict()), closed])
        rewritten = rewritten.reindex(columns=ptm.POSITIONS_COLS)
        cash = 200000.0 - 7245.6 - 110.25
        eq = pd.DataFrame([{"DATE": "2026-06-08", "EQUITY": 200000.0, "CASH": 200000.0, "INVESTED": 0.0,
                            "REALIZED_PNL": 0.0, "N_OPEN": 0},
                           {"DATE": "2026-10-01", "EQUITY": 199000.0, "CASH": cash, "INVESTED": 7245.6,
                            "REALIZED_PNL": -110.25, "N_OPEN": 1, "CASH_NOTE": ptm._cash_note(rewritten)}])
        for pos in (rows, rewritten):                                    # before and after the rewrite
            v = lv.verify_cash(pos, eq)
            self.assertFalse(v["verified"])
            self.assertIn("JBCHEPHARM entered 2026-07-13", v["check"])
            self.assertIn(f"would give Rs{cash:,.2f}, which equals the logged cash", v["check"])
            self.assertIn("the records do not say whether zero is correct", v["check"])


def ledger_rows(book: Book) -> pd.DataFrame:
    """positions.csv rows as paper_trade_manager.run writes them (closed rows carry their trade's P&L)."""
    rows = []
    for h in book.holdings:
        row = ptm._row_from_holding(h, {"signal_date": "", "size_inr": "" if h.cost_note else round(h.cost_basis, 2)})
        t = next((t for t in book.trades if t["hid"] == h.hid), None)
        if h.state == CLOSED and t:
            row.update({"exit_date": ptm._iso(t["exit_date"]), "exit_reason": t["reason"],
                        "realized_pnl_inr": round(t["pnl"], 2)})
        rows.append(row)
    return pd.DataFrame(rows).reindex(columns=ptm.POSITIONS_COLS)


class CashReconciliation(unittest.TestCase):
    """Every cash movement the engine produces, with fees, against the ledger's cash identity."""

    DAYS = ["20260101", "20260102", "20260105", "20260106", "20260107"]
    CAPITAL = 100_000.0

    def setUp(self):
        d0, d1, d2, d3, d4 = self.DAYS
        bars = {}
        for d in self.DAYS:
            bars[("A", d)] = (100, 101, 99, 100)
            bars[("P", d)] = (679, 679, 658, 660.75) if d == d0 else (400, 421, 376, 395.45)
        for d, b in zip(self.DAYS, [(200, 201, 199, 200), (200, 202, 198, 201), (195, 196, 180, 185)]):
            bars[("B", d)] = b                                          # B: stop 190 hit on d2
        bars[("X", d0)] = (470, 476, 469, 473.75)                        # X: last trade d0, then exit offer
        bars[("T", d0)] = (2400, 2420, 2390, 2408.9)                     # T: merges into S
        for d in (d1, d2, d3, d4):
            bars[("S", d)] = (4760, 4800, 4700, 4763)
        bars[("C", d2)] = (335, 340, 320, 329.45)                        # C: demerged child, first trade d2
        sec = sec_events([
            {"EVENT_ID": "A-div", "SYMBOL": "A", "TYPE": "cash_dividend", "CASH_PER_SHARE": 5, "EX_DATE": d1,
             "CASH_PAY_DATE": d3, "STATUS": "verified"},
            {"EVENT_ID": "X-d", "SYMBOL": "X", "TYPE": "delisting_cash", "CASH_PER_SHARE": 480, "LAST_TRADE_DATE": d0,
             "TENDER_DATE": d3, "CASH_PAY_DATE": d4, "STATUS": "verified"},
            {"EVENT_ID": "T-m", "SYMBOL": "T", "TYPE": "merger_stock", "SUCCESSOR": "S", "RATIO_NUM": 51,
             "RATIO_DEN": 100, "LAST_TRADE_DATE": d0, "TRADABLE_DATE": d2, "FRACTION_TREATMENT": "pending",
             "STATUS": "verified"},
            {"EVENT_ID": "P-d", "SYMBOL": "P", "TYPE": "demerger", "SUCCESSOR": "C", "RATIO_NUM": 1, "RATIO_DEN": 1,
             "EX_DATE": d1, "TRADABLE_DATE": d2, "PRICE_FACTOR": 0.6054, "STATUS": "verified"}])
        bar = lambda s, d: dict(zip(("open", "high", "low", "close"), bars[(s, d)])) if (s, d) in bars else None
        self.book = Book(self.CAPITAL, bar, lambda s, d: None, lambda n, side, s, d: n * 0.001, None, None, sec)
        for sym, qty, px, stop in (("A", 10, 100.0, 90.0), ("B", 5, 200.0, 190.0), ("X", 10, 470.0, 400.0),
                                   ("T", 3, 2400.0, 2335.0), ("P", 10, 679.0, 600.0)):
            self.book.open_position(sym, d0, qty, px, None, stop=stop)

    def snapshot(self, d):
        rows = ledger_rows(self.book)
        comp = self.book.value(d)
        status = rows["status"]
        eq = pd.DataFrame([
            {"DATE": "2025-12-31", "EQUITY": self.CAPITAL, "CASH": self.CAPITAL, "INVESTED": 0.0, "REALIZED_PNL": 0.0,
             "N_OPEN": 0},
            {"DATE": ptm._iso(d), "EQUITY": round(comp["equity"], 2), "CASH": round(comp["cash"], 2),
             "INVESTED": round(sum(h.cost_basis for h in self.book.active() if h.state == OPEN), 2),
             "REALIZED_PNL": round(pd.to_numeric(rows.loc[status == "closed", "realized_pnl_inr"]).sum(), 2),
             "N_OPEN": comp["n_open"], "CASH_NOTE": ptm._cash_note(rows)}])
        realized = pd.DataFrame([ptm._trade_to_realized(t) for t in self.book.trades]) if self.book.trades else None
        return rows, eq, realized, comp

    def test_every_movement_reconciles_every_day(self):
        cash = {}
        for d in self.DAYS:
            run_day(self.book, d)
            rows, eq, realized, comp = self.snapshot(d)
            v = lv.verify_cash(rows, eq, realized)
            self.assertTrue(v["verified"], f"{d}: {v['check']}")
            self.assertAlmostEqual(v["cash"], self.book.cash, places=2)
            cash[d] = self.book.cash
        d0, d1, d2, d3, d4 = self.DAYS
        reasons = {(t["symbol"], t["reason"]) for t in self.book.trades}
        self.assertEqual(reasons, {("B", "STOP_LOSS"), ("C", "ENTITLEMENT_SALE_FIRST_TRADABLE_OPEN"),
                                   ("A", "CASH_DIVIDEND"), ("X", "CASH_CONSIDERATION")})
        statuses = dict(zip(ledger_rows(self.book).hid, ledger_rows(self.book).status))
        self.assertIn("converted", statuses.values())                     # T: merged, not a sale
        self.assertTrue(any(h.kind == "fraction" and h.state == PENDING for h in self.book.holdings))
        fee = lambda n: n * 0.001
        # sale: + proceeds net of the exit fee (B), + the demerged child's sale (C)
        self.assertAlmostEqual(cash[d2] - cash[d1], 190 * 5 - fee(190 * 5) + 335 * 10 - fee(335 * 10), places=6)
        # dividend: nothing on the ex-date, cash only on the pay date
        self.assertAlmostEqual(cash[d1], cash[d0], places=6)
        self.assertAlmostEqual(cash[d3] - cash[d2], 50.0, places=6)
        # exit offer: owed from the tender date (d3), spendable on the pay date (d4)
        self.assertAlmostEqual(cash[d4] - cash[d3], 4800.0, places=6)

    def test_amounts_owed_are_not_cash(self):
        d0, d1, d2, d3, d4 = self.DAYS
        for d in (d0, d1):
            run_day(self.book, d)
        rows, eq, realized, comp = self.snapshot(d1)
        s = lv.summary(rows, eq, None, realized)
        self.assertEqual(s["owed_by_state"], {lv.DIV_OWED: 50.0})          # dividend owed on the ex-date ...
        self.assertAlmostEqual(s["available_cash"], round(self.book.cash, 2))
        self.assertAlmostEqual(self.book.cash, self.CAPITAL - sum(h.cost_basis for h in self.book.holdings
                                                                    if h.origin == "entry"), places=6)  # ... not in cash
        for d in (d2, d3):
            run_day(self.book, d)
        rows, eq, realized, comp = self.snapshot(d3)
        s = lv.summary(rows, eq, None, realized)
        self.assertEqual(s["owed_by_state"], {lv.CONSIDERATION_OWED: 4800.0})   # tendered, not yet paid
        self.assertTrue(lv.verify_cash(rows, eq, realized)["verified"])

    def test_missing_or_disagreeing_records_fail_closed(self):
        for d in self.DAYS:
            run_day(self.book, d)
        rows, eq, realized, comp = self.snapshot(self.DAYS[-1])
        r2 = realized.copy(); r2.loc[0, "pnl_inr"] += 10                # realized_trades disagrees
        self.assertIn("realized_trades.csv P&L", lv.verify_cash(rows, eq, r2)["check"])
        p2 = rows.copy(); p2.loc[p2.status == "closed", "realized_pnl_inr"] = p2.loc[p2.status == "closed", "realized_pnl_inr"].where(
            p2.loc[p2.status == "closed", "exit_reason"] != "STOP_LOSS")         # one closed row lost its P&L
        self.assertIn("(no realised P&L recorded)", lv.verify_cash(p2, eq, realized)["check"])
        e2 = eq.copy(); e2.loc[1, "CASH_NOTE"] = "X: entry cost not recorded"  # the ledger itself flags a gap
        self.assertIn("the equity log says the cash leaves out unrecorded amounts", lv.verify_cash(rows, e2, realized)["check"])


if __name__ == "__main__":
    unittest.main()
