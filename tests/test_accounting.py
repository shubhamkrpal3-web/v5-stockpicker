"""Share-based accounting, derived-data layer and symbol continuity."""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))

import data_integrity  # noqa: E402
import security_master  # noqa: E402
from portfolio_accounting import CLOSED, OPEN, PENDING, STALE, Book  # noqa: E402

SEC_COLS = security_master.EVENT_COLS


def sec_events(rows):
    df = pd.DataFrame(rows).reindex(columns=SEC_COLS)
    for c in ("RATIO_NUM", "RATIO_DEN", "CASH_PER_SHARE", "PRICE_FACTOR"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.fillna({c: "" for c in SEC_COLS if c not in ("RATIO_NUM", "RATIO_DEN", "CASH_PER_SHARE", "PRICE_FACTOR")})


def make_book(bars, atr=None, share_ev=None, other_ev=None, sec=None, cash=100000.0, stale_after=3):
    def bar(sym, d):
        b = bars.get((sym, d))
        return dict(zip(("open", "high", "low", "close"), b)) if b else None
    return Book(cash, bar, lambda s, d: (atr or {}).get((s, d)), lambda n, side, s, d: 0.0,
                share_ev, other_ev, sec, chandelier=4.0, stale_after=stale_after)


def run_day(book, d, enter=None):
    book.apply_events(d); book.settle_pending(d); book.check_exits(d)
    if enter:
        enter(book)
    book.end_of_day(d)
    return book.value(d)


class Fills(unittest.TestCase):
    def test_gap_through_stop_fills_at_open(self):
        bars = {("A", "d1"): (100, 101, 99, 100), ("A", "d2"): (90, 92, 88, 91)}
        b = make_book(bars)
        b.open_position("A", "d1", 10, 100.0, None, stop=95.0)
        run_day(b, "d1"); run_day(b, "d2")
        t = b.trades[0]
        self.assertEqual(t["reason"], "STOP_GAP_AT_OPEN")
        self.assertEqual(t["exit"], 90.0)                       # not the 95 stop

    def test_touch_fills_at_stop(self):
        bars = {("A", "d1"): (100, 101, 99, 100), ("A", "d2"): (99, 99, 94, 96)}
        b = make_book(bars)
        b.open_position("A", "d1", 10, 100.0, None, stop=95.0)
        run_day(b, "d1"); run_day(b, "d2")
        self.assertEqual((b.trades[0]["reason"], b.trades[0]["exit"]), ("STOP_LOSS", 95.0))

    def test_trailing_stop_uses_previous_close_only(self):
        # d2 closes at a new high that would lift the stop to 116; its own low (110) must
        # be judged against the stop in force at the open (95), so no exit on d2.
        bars = {("A", "d1"): (100, 101, 99, 100), ("A", "d2"): (101, 125, 110, 124)}
        b = make_book(bars, atr={("A", "d2"): 2.0})
        b.open_position("A", "d1", 10, 100.0, None, stop=95.0)
        run_day(b, "d1"); run_day(b, "d2")
        self.assertEqual(b.trades, [])
        self.assertAlmostEqual(b.holdings[0].stop, 124 - 8.0)   # applies from d3

    def test_initial_stop_live_on_entry_day(self):
        bars = {("A", "d1"): (100, 101, 93, 99)}
        b = make_book(bars)
        b.open_position("A", "d1", 10, 100.0, None, stop=95.0)
        run_day(b, "d1")
        self.assertEqual((b.trades[0]["reason"], b.trades[0]["exit"]), ("STOP_LOSS_ENTRY_DAY", 95.0))


class Events(unittest.TestCase):
    def test_official_split_changes_share_count_not_value(self):
        bars = {("A", "d1"): (100, 101, 99, 100), ("A", "d2"): (50, 51, 49, 50)}
        se = pd.DataFrame([{"SYMBOL": "A", "EX_DATE": "d2", "KIND": "split", "FACTOR": 0.5}])
        b = make_book(bars, share_ev=se)
        b.open_position("A", "d1", 10, 100.0, None, stop=90.0)
        run_day(b, "d1"); v = run_day(b, "d2")
        h = b.holdings[0]
        self.assertEqual((h.qty, h.stop, h.state), (20.0, 45.0, OPEN))
        self.assertAlmostEqual(v["open_mtm"], 1000.0)
        self.assertEqual(b.trades, [])

    def test_price_derived_event_makes_holding_stale_not_sold(self):
        bars = {("A", "d1"): (100, 101, 99, 100), ("A", "d2"): (40, 41, 39, 40)}
        oe = pd.DataFrame([{"SYMBOL": "A", "EX_DATE": "d2", "KIND": "inferred", "FACTOR": 0.4}])
        b = make_book(bars, other_ev=oe)
        b.open_position("A", "d1", 10, 100.0, None, stop=90.0)
        run_day(b, "d1"); v = run_day(b, "d2")
        h = b.holdings[0]
        self.assertEqual(h.state, STALE)
        self.assertEqual(b.trades, [])                          # no sale invented
        self.assertEqual(b.slots_used(), 1)                     # capacity still taken
        self.assertAlmostEqual(v["stale_mark"], 1000.0)         # last traded price x shares
        self.assertAlmostEqual(v["equity_stale_at_zero"], v["equity"] - 1000.0)

    def test_unexplained_ending_stays_stale(self):
        bars = {("A", "d1"): (100, 101, 99, 100)}
        b = make_book(bars, stale_after=2)
        b.open_position("A", "d1", 10, 100.0, None, stop=90.0)
        for d in ("d1", "d2", "d3", "d4"):
            v = run_day(b, d)
        self.assertEqual(b.holdings[0].state, STALE)
        self.assertEqual((b.trades, v["n_stale"], b.slots_used()), ([], 1, 1))

    def test_partial_merger_is_stale_with_reason(self):
        bars = {("A", "20260101"): (100, 101, 99, 100)}
        sec = sec_events([{"EVENT_ID": "A-m", "SYMBOL": "A", "TYPE": "merger", "LAST_TRADE_DATE": "20260101",
                           "STATUS": "partial"}])
        b = make_book(bars, sec=sec)
        b.open_position("A", "20260101", 10, 100.0, None, stop=90.0)
        run_day(b, "20260101"); run_day(b, "20260102")
        self.assertEqual(b.holdings[0].state, STALE)
        self.assertIn("terms not verified", b.holdings[0].stale_reason)

    def test_verified_stock_merger_pending_then_tradable_with_fraction(self):
        # 3 shares of T -> 51:100 of S = 1.53: 1 whole share pending + 0.53 fraction pending
        bars = {("T", "20260716"): (2400, 2420, 2390, 2408.9),
                ("S", "20260717"): (4760, 4800, 3000, 4763),     # a low far below the converted stop
                ("S", "20260728"): (4990, 5000, 4900, 4950)}
        sec = sec_events([{"EVENT_ID": "T-m", "SYMBOL": "T", "TYPE": "merger_stock", "SUCCESSOR": "S",
                           "RATIO_NUM": 51, "RATIO_DEN": 100, "LAST_TRADE_DATE": "20260716",
                           "RECORD_DATE": "20260717", "TRADABLE_DATE": "20260728",
                           "FRACTION_TREATMENT": "pending", "STATUS": "verified"}])
        b = make_book(bars, sec=sec)
        h = b.open_position("T", "20260716", 3, 2400.0, None, stop=2335.46)
        run_day(b, "20260716")
        v17 = run_day(b, "20260717")
        succ = [x for x in b.holdings if x.symbol == "S" and x.kind == "shares"][0]
        frac = [x for x in b.holdings if x.kind == "fraction"][0]
        self.assertEqual((h.state, succ.state, succ.qty), (CLOSED, PENDING, 1.0))
        self.assertAlmostEqual(frac.qty, 0.53)
        self.assertEqual(b.trades, [])                          # pending: no stop fired at 3000
        self.assertAlmostEqual(succ.stop, 2335.46 / 0.51, places=4)
        self.assertAlmostEqual(v17["pending_ref"], 4763.0)      # market reference, not tradable
        run_day(b, "20260728")
        self.assertEqual(succ.state, OPEN)
        self.assertEqual(frac.state, PENDING)                   # treatment not documented
        self.assertAlmostEqual(succ.cost_basis + frac.cost_basis, h.cost_basis)

    def test_converted_holding_ignores_later_events(self):
        bars = {("T", "20260716"): (2400, 2420, 2390, 2408.9), ("S", "20260717"): (4760, 4800, 4700, 4763)}
        sec = sec_events([
            {"EVENT_ID": "T-v", "SYMBOL": "T", "TYPE": "merger_stock", "SUCCESSOR": "S", "RATIO_NUM": 51,
             "RATIO_DEN": 100, "LAST_TRADE_DATE": "20260716", "TRADABLE_DATE": "20260728", "STATUS": "verified"},
            {"EVENT_ID": "T-p", "SYMBOL": "T", "TYPE": "merger", "LAST_TRADE_DATE": "20260716", "STATUS": "partial"}])
        b = make_book(bars, sec=sec)
        h = b.open_position("T", "20260716", 3, 2400.0, None, stop=2335.46)
        run_day(b, "20260716"); v = run_day(b, "20260717")
        self.assertEqual(h.state, CLOSED)
        self.assertEqual((v["n_stale"], v["stale_mark"]), (0, 0.0))

    def test_demerger_parent_kept_child_sold_at_first_tradable_open(self):
        bars = {("P", "20251013"): (679, 679, 658, 660.75), ("P", "20251014"): (400, 421, 376, 395.45),
                ("C", "20251112"): (335, 340, 320, 329.45)}
        sec = sec_events([{"EVENT_ID": "P-d", "SYMBOL": "P", "TYPE": "demerger", "SUCCESSOR": "C",
                           "RATIO_NUM": 1, "RATIO_DEN": 1, "EX_DATE": "20251014", "TRADABLE_DATE": "20251112",
                           "PRICE_FACTOR": 0.6054, "FACTOR_TYPE": "price_derived_estimate", "STATUS": "verified"}])
        oe = pd.DataFrame([{"SYMBOL": "P", "EX_DATE": "20251014", "KIND": "demerger", "FACTOR": 0.6054}])
        b = make_book(bars, sec=sec, other_ev=oe)
        # stop 600 -> 363.2 after continuity rescaling, below the ex-date low of 376.
        # Without rescaling, the 400 open would have gapped through 600 and sold the parent.
        p = b.open_position("P", "20251013", 10, 679.0, None, stop=600.0)
        run_day(b, "20251013")
        v = run_day(b, "20251014")
        child = [x for x in b.holdings if x.symbol == "C"][0]
        self.assertEqual((p.state, p.qty, child.state, child.qty), (OPEN, 10.0, PENDING, 10.0))
        self.assertEqual(b.trades, [])                          # parent not stopped by the ex-date drop
        self.assertAlmostEqual(v["open_mtm"], 3954.5)           # parent at its real price
        self.assertAlmostEqual(v["pending_ref"], 0.0)           # child unpriced until it trades
        self.assertGreater(v["pending_unpriced_indicative"], 0)  # estimate reported separately only
        run_day(b, "20251112")
        t = b.trades[0]
        self.assertEqual((t["symbol"], t["reason"], t["exit"]), ("C", "ENTITLEMENT_SALE_FIRST_TRADABLE_OPEN", 335.0))

    def test_exit_offer_locked_then_receivable_then_spendable(self):
        # last trade 13-Dec -> shares locked -> tendered 21-Dec (receivable) -> cash on the pay date
        bars = {("X", "20181213"): (470, 476, 469, 473.75)}
        sec = sec_events([{"EVENT_ID": "X-d", "SYMBOL": "X", "TYPE": "delisting_cash", "CASH_PER_SHARE": 480,
                           "LAST_TRADE_DATE": "20181213", "TENDER_DATE": "20181221", "TENDER_DATE_BASIS": "assumed tender",
                           "CASH_PAY_DATE": "20190114", "PAY_DATE_BASIS": "assumed payment", "STATUS": "verified"}])
        b = make_book(bars, sec=sec, cash=10000.0)
        h = b.open_position("X", "20181213", 10, 470.0, None, stop=400.0)
        run_day(b, "20181213")
        cash0 = b.cash
        for d in ("20181214", "20181217", "20181220"):            # suspended, not yet tenderable
            v = run_day(b, d)
            self.assertEqual((h.kind, h.state, h.cash_due), ("shares", PENDING, 0.0))
            self.assertAlmostEqual(v["pending_ref"], 4737.5)        # locked shares at their last traded price
            self.assertEqual((v["cash"], v["n_stale"], b.slots_used()), (cash0, 0, 1))
        self.assertIn("awaiting tender on 20181221 [assumed tender]", h.notes[-1])
        v = run_day(b, "20181221")
        self.assertEqual((h.kind, h.state, h.cash_due, h.cash_date), ("cash", PENDING, 4800.0, "20190114"))
        self.assertEqual((v["cash"], v["pending_ref"], v["equity_tradable"]), (cash0, 4800.0, cash0))
        self.assertIn("tendered 20181221 [assumed tender]; receivable", h.notes[-1])
        self.assertIn("spendable from 20190114 [assumed payment]", h.notes[-1])
        run_day(b, "20190111")
        self.assertEqual(b.cash, cash0)                         # not spendable before the pay date
        v = run_day(b, "20190114")
        self.assertEqual((h.state, v["cash"], v["pending_ref"]), (CLOSED, cash0 + 4800.0, 0.0))
        self.assertEqual(b.trades[-1]["reason"], "CASH_CONSIDERATION")

    def _dividend_book(self, pay="", extra_bars=None):
        bars = {("A", "20201222"): (980, 990, 975, 985.65), ("A", "20201223"): (12.2, 12.5, 12.0, 12.2),
                ("A", "20210115"): (13.0, 13.2, 12.8, 13.1)} | (extra_bars or {})
        f = (985.65 - 974) / 985.65
        sec = sec_events([{"EVENT_ID": "A-div", "SYMBOL": "A", "TYPE": "cash_dividend", "CASH_PER_SHARE": 974,
                           "EX_DATE": "20201223", "RECORD_DATE": "20201225", "CASH_PAY_DATE": pay,
                           "PRICE_FACTOR": f, "FACTOR_TYPE": "official_cash_distribution", "STATUS": "verified"}])
        oe = pd.DataFrame([{"SYMBOL": "A", "EX_DATE": "20201223", "KIND": "cash_distribution", "FACTOR": f}])
        b = make_book(bars, sec=sec, other_ev=oe, cash=10000.0)
        h = b.open_position("A", "20201222", 5, 980.0, None, stop=900.0)
        return b, h, f

    def test_cash_dividend_shares_continue_receivable_not_spendable(self):
        b, h, f = self._dividend_book(pay="")
        cost0 = h.cost_basis
        run_day(b, "20201222")
        cash0 = b.cash
        v = run_day(b, "20201223")
        rc = [x for x in b.holdings if x.origin.startswith("dividend:")][0]
        self.assertEqual((h.state, h.qty), (OPEN, 5.0))        # shares continue, not stale
        self.assertEqual(b.trades, [])                          # 12.20 open did not trip a 900 stop
        self.assertAlmostEqual(h.stop, 900.0 * f)
        self.assertEqual((rc.kind, rc.state, rc.cash_due), ("cash", PENDING, 4870.0))
        self.assertEqual((v["cash"], v["pending_ref"]), (cash0, 4870.0))
        self.assertAlmostEqual(v["open_mtm"], 61.0)
        self.assertEqual(b.slots_used(), 1)                     # the receivable takes no slot
        self.assertAlmostEqual(h.cost_basis + rc.cost_basis, cost0)
        self.assertIn("pay date not established", rc.notes[-1])
        v = run_day(b, "20210115")
        self.assertEqual((rc.state, v["cash"]), (PENDING, cash0))   # still not spendable

    def test_cash_dividend_credited_on_documented_pay_date(self):
        b, h, f = self._dividend_book(pay="20210115")
        run_day(b, "20201222"); run_day(b, "20201223")
        cash0 = b.cash
        v = run_day(b, "20210115")
        self.assertEqual((v["cash"], v["pending_ref"]), (cash0 + 4870.0, 0.0))
        self.assertEqual(b.trades[-1]["reason"], "CASH_DIVIDEND")

    def test_rebuy_while_earlier_dividend_receivable_pending(self):
        # shares stopped out after the ex-date while their dividend is still owed: the receivable is money,
        # not a holding, so the stock can be bought again; the new lot alone takes the slot; each settles once
        b, h, f = self._dividend_book(pay="20210115", extra_bars={("A", "20201224"): (11.0, 11.2, 10.5, 10.8),
                                                                  ("A", "20201228"): (11.0, 11.5, 10.9, 11.4)})
        run_day(b, "20201222"); run_day(b, "20201223")
        rc = [x for x in b.holdings if b.is_dividend_receivable(x)][0]
        self.assertTrue(b.holds("A"))                             # shares still open alongside the receivable
        run_day(b, "20201224")
        self.assertEqual((h.state, b.trades[-1]["reason"], rc.state), (CLOSED, "STOP_LOSS", PENDING))
        self.assertFalse(b.holds("A"))                            # only the receivable remains
        self.assertEqual(b.slots_used(), 0)
        new = []
        v = run_day(b, "20201228", enter=lambda bk: new.append(bk.open_position("A", "20201228", 10, 11.0, None, stop=10.0)))
        n = new[0]
        self.assertIsNotNone(n)
        self.assertEqual((n.state, n.qty, b.holds("A"), b.slots_used()), (OPEN, 10.0, True, 1))
        self.assertEqual((rc.state, v["pending_ref"]), (PENDING, 4870.0))   # the old receivable is untouched
        cash0 = b.cash
        v = run_day(b, "20210115")
        self.assertEqual((rc.state, v["cash"], v["pending_ref"]), (CLOSED, cash0 + 4870.0, 0.0))
        self.assertEqual(sum(1 for x in b.holdings if b.is_dividend_receivable(x)), 1)   # no second receivable
        self.assertEqual((n.state, n.qty, n.stop0), (OPEN, 10.0, 10.0))     # the new lot is not rescaled

    def test_locked_exit_offer_shares_still_held(self):
        sec = sec_events([{"EVENT_ID": "X-d", "SYMBOL": "X", "TYPE": "delisting_cash", "CASH_PER_SHARE": 480,
                           "LAST_TRADE_DATE": "20181213", "TENDER_DATE": "20181221", "CASH_PAY_DATE": "20190114",
                           "STATUS": "verified"}])
        b = make_book({("X", "20181213"): (470, 476, 469, 473.75)}, sec=sec, cash=10000.0)
        h = b.open_position("X", "20181213", 10, 470.0, None, stop=400.0)
        run_day(b, "20181213"); run_day(b, "20181214")
        self.assertEqual((h.kind, h.state, b.holds("X"), b.slots_used()), ("shares", PENDING, True, 1))


class DerivedLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._d, self._q = data_integrity.DERIVED, data_integrity.QUARANTINE
        data_integrity.DERIVED = Path(self.tmp.name)
        data_integrity.QUARANTINE = Path(self.tmp.name) / "quarantine.csv"

    def tearDown(self):
        data_integrity.DERIVED, data_integrity.QUARANTINE = self._d, self._q
        self.tmp.cleanup()

    def test_drop_and_replace_sessions(self):
        (Path(self.tmp.name) / "corr.csv").write_text(
            "DATE,SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,VOLUME,TRADED_VALUE\n20190930,X,EQ,28,29,27,28.7,1,1\n20190930,X,BE,1,1,1,1,1,1\n")
        pd.DataFrame([{"DATE": "20190930", "ACTION": "replace_session", "REPLACEMENT": "corr.csv"},
                      {"DATE": "20260528", "ACTION": "drop_session", "REPLACEMENT": ""}]).to_csv(
            data_integrity.QUARANTINE, index=False)
        df = pd.DataFrame({"DATE": ["20190930", "20260527", "20260528"], "SYMBOL": "X",
                           "OPEN": [49, 1, 1], "HIGH": [50, 1, 1], "LOW": [48, 1, 1], "CLOSE": [49.55, 1, 1]})
        out = data_integrity.apply(df)
        self.assertEqual(sorted(out["DATE"]), ["20190930", "20260527"])
        self.assertEqual(float(out.loc[out["DATE"] == "20190930", "CLOSE"].iloc[0]), 28.7)   # EQ row only

    def test_relabel_session_moves_rows_to_the_files_own_date(self):
        pd.DataFrame([{"DATE": "20211105", "ACTION": "relabel_session", "REPLACEMENT": "20211104"}]).to_csv(
            data_integrity.QUARANTINE, index=False)
        df = pd.DataFrame({"DATE": ["20211103", "20211105", "20211108"], "SYMBOL": "X", "CLOSE": [1.0, 2.0, 3.0]})
        out = data_integrity.apply(df)
        self.assertEqual(list(out["DATE"]), ["20211103", "20211104", "20211108"])
        self.assertEqual(float(out.loc[out["DATE"] == "20211104", "CLOSE"].iloc[0]), 2.0)   # rows kept, not dropped
        clash = pd.DataFrame({"DATE": ["20211104", "20211105"], "SYMBOL": "X", "CLOSE": [1.0, 2.0]})
        with self.assertRaises(ValueError):
            data_integrity.apply(clash)

    def test_supplement_rows_follow_renames(self):
        saved = (security_master.SUPPLEMENT, security_master.SYMBOL_CHANGES, security_master.SYMBOL_CHANGES_CURATED)
        try:
            t = Path(self.tmp.name)
            security_master.SUPPLEMENT = t / "sup.csv"
            security_master.SYMBOL_CHANGES = t / "sc.csv"
            security_master.SYMBOL_CHANGES_CURATED = t / "none.csv"
            pd.DataFrame([{"OLD": "MAJESCO", "NEW": "AURUM", "EFFECTIVE_DATE": "20211022"}]).to_csv(
                security_master.SYMBOL_CHANGES, index=False)
            pd.DataFrame([{"DATE": "20210205", "SYMBOL": "MAJESCO", "SERIES": "BE", "OPEN": 51.2, "HIGH": 51.2,
                           "LOW": 51.2, "CLOSE": 51.2, "VOLUME": 1, "TRADED_VALUE": 1, "SOURCE": "x"}]).to_csv(
                security_master.SUPPLEMENT, index=False)
            self.assertEqual(list(security_master.load_supplement()["SYMBOL"]), ["AURUM"])
            self.assertEqual(list(security_master.load_supplement(stitched=False)["SYMBOL"]), ["MAJESCO"])
        finally:
            security_master.SUPPLEMENT, security_master.SYMBOL_CHANGES, security_master.SYMBOL_CHANGES_CURATED = saved

    def test_stitch_renames_and_refuses_overlaps(self):
        sc = pd.DataFrame([{"OLD": "KPIT", "NEW": "BSOFT", "EFFECTIVE_DATE": "20190226"},
                           {"OLD": "OLDX", "NEW": "NEWX", "EFFECTIVE_DATE": "20200101"}])
        df = pd.DataFrame({"DATE": ["20190123", "20190226", "20191201", "20191201"],
                           "SYMBOL": ["KPIT", "BSOFT", "OLDX", "NEWX"], "CLOSE": [216.65, 100, 1, 2]})
        out = security_master.stitch(df, sc)
        self.assertEqual(list(out["SYMBOL"]), ["BSOFT", "BSOFT", "OLDX", "NEWX"])   # overlap left alone
        self.assertEqual(security_master.canonical("KPIT", sc), "BSOFT")


class LedgerRows(unittest.TestCase):
    """Paper-ledger rows must carry everything a pending entitlement needs across reloads."""

    def test_cash_receivable_and_locked_shares_survive_reload(self):
        import paper_trade_manager as ptm
        from portfolio_accounting import Holding
        sc = pd.DataFrame(columns=["OLD", "NEW", "EFFECTIVE_DATE"])
        rc = Holding(hid="V1", symbol="AURUM", qty=4, state=PENDING, entry_date="20201223", kind="cash",
                     cash_due=3896.0, cash_date="", origin="dividend:AURUM-20201223-dividend", cost_basis=3100.0)
        ex = Holding(hid="H2", symbol="X", qty=10, state=PENDING, entry_date="20181203", kind="cash",
                     cash_due=4800.0, cash_date="20190114", origin="entry", cost_basis=4700.0)
        lk = Holding(hid="H3", symbol="Y", qty=3, state=PENDING, entry_date="20230123", kind="shares",
                     last_mark=461.7, last_mark_date="20230327", origin="entry", cost_basis=1400.0)
        lk.applied = ["sec:Y-d:locked"]
        rows = pd.DataFrame([ptm._row_from_holding(h) for h in (rc, ex, lk)]).reindex(columns=ptm.POSITIONS_COLS)
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "positions.csv"
            rows.to_csv(p, index=False)
            back = ptm._load_positions(p)
        got = [ptm._holding_from_row(r, i, sc) for i, r in enumerate(back.itertuples(index=False))]
        self.assertEqual([(g.kind, g.state, g.cash_due, g.cash_date) for g in got[:2]],
                         [("cash", PENDING, 3896.0, ""), ("cash", PENDING, 4800.0, "20190114")])
        self.assertEqual(got[0].origin, "dividend:AURUM-20201223-dividend")
        self.assertEqual((got[2].state, got[2].applied), (PENDING, ["sec:Y-d:locked"]))

    def test_reload_fills_last_price_safely(self):
        import paper_trade_manager as ptm
        from portfolio_accounting import Holding
        raw = pd.DataFrame({"SYMBOL": ["A", "A"], "DATE": ["20261001", "20261002"], "CLOSE": [10.0, 11.0]}
                           ).set_index(["SYMBOL", "DATE"])
        nan = float("nan")
        a = Holding(hid="1", symbol="A", qty=1, entry_price=9.0, entry_date="20260901", last_mark=nan)
        gone = Holding(hid="2", symbol="DELISTED", qty=1, entry_price=7.0, entry_date="20200101", last_mark=nan)
        rc = Holding(hid="3", symbol="DELISTED", qty=1, kind="cash", cash_due=480.0, last_mark=nan)
        for h in (a, gone, rc):
            ptm._fill_last_mark(h, raw, "20261001")
        self.assertEqual((a.last_mark, a.last_mark_date), (10.0, "20261001"))     # not the later 11.0
        self.assertEqual((gone.last_mark, gone.last_mark_date), (7.0, "20200101"))
        self.assertTrue(rc.last_mark != rc.last_mark)                              # cash: valued at cash_due

    def test_replay_triggers_for_unapplied_dividend_not_for_locked_exit(self):
        import paper_trade_manager as ptm
        from portfolio_accounting import Holding
        sec = sec_events([{"EVENT_ID": "A-div", "SYMBOL": "A", "TYPE": "cash_dividend", "EX_DATE": "20201223",
                           "STATUS": "verified"},
                          {"EVENT_ID": "Y-d", "SYMBOL": "Y", "TYPE": "delisting_cash", "LAST_TRADE_DATE": "20230327",
                           "STATUS": "verified"}])
        a = Holding(hid="H1", symbol="A", qty=5, entry_date="20201201")
        y = Holding(hid="H2", symbol="Y", qty=3, entry_date="20230123")
        y.applied = ["sec:Y-d:locked"]
        self.assertTrue(ptm._needs_replay(a, sec))
        self.assertFalse(ptm._needs_replay(y, sec))


class DividendFactor(unittest.TestCase):
    def test_verified_dividend_supersedes_inferred_gap(self):
        import corporate_actions as ca
        px = pd.DataFrame({"SYMBOL": "A", "DATE": ["20201222", "20201223"], "OPEN": [980.0, 12.2],
                           "CLOSE": [985.65, 12.2]})
        sec = sec_events([{"EVENT_ID": "A-div", "SYMBOL": "A", "TYPE": "cash_dividend", "CASH_PER_SHARE": 974,
                           "EX_DATE": "20201223", "STATUS": "verified"}])
        new = ca.cash_distribution_events(px, sec)
        self.assertAlmostEqual(float(new["FACTOR"].iloc[0]), (985.65 - 974) / 985.65)
        self.assertEqual(new["FACTOR_TYPE"].iloc[0], "official_cash_distribution")
        old = pd.DataFrame([{"SYMBOL": "A", "EX_DATE": "20201223", "KIND": "inferred", "FACTOR": 0.0124,
                             "FACTOR_TYPE": "price_derived_estimate", "SOURCE": "inferred", "STATUS": "raw_gap"}])
        merged = ca.merge_events(old, new)
        self.assertEqual(list(merged["KIND"]), ["cash_distribution"])


class ExecutionBars(unittest.TestCase):
    """A holding moved to the BE series: its BE bars feed execution and the stop's ATR,
    never the signal matrices."""

    def test_supplement_rows_feed_exec_not_signals(self):
        import backtest_regime as br
        tmp = tempfile.TemporaryDirectory()
        saved = (data_integrity.QUARANTINE, security_master.SYMBOL_CHANGES,
                 security_master.SYMBOL_CHANGES_CURATED, security_master.SUPPLEMENT)
        try:
            t = Path(tmp.name)
            data_integrity.QUARANTINE = t / "none.csv"
            security_master.SYMBOL_CHANGES = security_master.SYMBOL_CHANGES_CURATED = t / "none2.csv"
            security_master.SUPPLEMENT = t / "sup.csv"
            days = pd.bdate_range("2021-07-01", periods=30).strftime("%Y%m%d")
            rows = []
            for i, d in enumerate(days):
                rows.append({"DATE": d, "SYMBOL": "OTHER", "SERIES": "EQ", "OPEN": 100, "HIGH": 102, "LOW": 98,
                             "CLOSE": 100 + i % 3, "TRADED_VALUE": 1e8})
                if not 10 <= i < 20:                       # EQ gap: sessions 10..19 traded in BE
                    rows.append({"DATE": d, "SYMBOL": "HELD", "SERIES": "EQ", "OPEN": 50, "HIGH": 51, "LOW": 49,
                                 "CLOSE": 50, "TRADED_VALUE": 1e7})
            pd.DataFrame(rows).to_csv(t / "master.csv", index=False)
            pd.DataFrame([{"DATE": d, "SYMBOL": "HELD", "SERIES": "BE", "OPEN": 50, "HIGH": 55, "LOW": 45,
                           "CLOSE": 52, "VOLUME": 1, "TRADED_VALUE": 1, "SOURCE": "test"} for d in days[10:20]]
                         ).to_csv(security_master.SUPPLEMENT, index=False)
            W = br.load_wide(t / "master.csv", "2021-07-01")
            d = pd.Timestamp(days[19])
            self.assertTrue(pd.isna(W["CLOSE"].at[d, "HELD"]))             # signals: EQ only
            self.assertEqual(W["RAW"]["CLOSE"].at[d, "HELD"], 52)          # execution: BE print
            self.assertEqual(W["EXEC"]["HIGH"].at[d, "HELD"], 55)          # stop ATR basis: same BE bar
            atr_exec = br.atr14(W["EXEC"]["HIGH"], W["EXEC"]["LOW"], W["EXEC"]["CLOSE"])
            atr_sig = br.atr14(W["HIGH"], W["LOW"], W["CLOSE"])
            # window = sessions 6..19: four EQ bars (true range 2) and ten BE bars (true range 10)
            self.assertAlmostEqual(atr_exec.at[d, "HELD"], (4 * 2 + 10 * 10) / 14)
            self.assertTrue(pd.isna(atr_sig.at[d, "HELD"]))                 # only 4 EQ bars in the window
            pd.testing.assert_series_equal(atr_exec["OTHER"], atr_sig["OTHER"])   # no supplement: identical
        finally:
            (data_integrity.QUARANTINE, security_master.SYMBOL_CHANGES,
             security_master.SYMBOL_CHANGES_CURATED, security_master.SUPPLEMENT) = saved
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
