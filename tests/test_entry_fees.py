"""Historical entry fees vs today's estimates, fractions that are not cash, back-dated (provisional) replay exits, and
the cash history rebuilt from dated movements. TEST VALUES ONLY (the IPCALAB row and its equity-log rows
mirror the shape of the real records; nothing here reads or writes the live ledger)."""
import argparse
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))
sys.path.insert(0, str(ROOT))

import data_integrity  # noqa: E402
import ledger_view as lv  # noqa: E402
import paper_trade_manager as ptm  # noqa: E402
import security_master  # noqa: E402
from portfolio_accounting import CLOSED, OPEN, PENDING, Book  # noqa: E402
from tests.test_accounting import run_day, sec_events  # noqa: E402
from tests.test_cash_integrity import ledger_rows, reload  # noqa: E402

SC = pd.DataFrame(columns=["OLD", "NEW", "EFFECTIVE_DATE"])
SUPPLEMENT_HEADER = "DATE,SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,VOLUME,TRADED_VALUE,SOURCE\n"
ROW = {"symbol": "IPCALAB", "signal_date": "20260814", "entry_date": "2026-08-17", "entry_price": 1805.9, "qty": 5.0,
       "size_inr": 9029.5, "initial_stop": 1605.14, "current_stop": 1819.34, "highest_close": 2009.6,
       "atr_at_entry": 73.94, "status": "open", "entry_cost_inr": np.nan}
EVIDENCE = pd.DataFrame([{
    "SYMBOL": "IPCALAB", "ENTRY_DATE": "2026-08-17", "QTY": "5", "ENTRY_PRICE": "1805.9", "NOTIONAL_INR": "9029.50",
    "HISTORICAL_ENTRY_FEE_INR": "0", "CODE_IN_FORCE": "ledger code f800a30 (test): no fee step",
    "LEDGER_COMMIT": "253fef6", "RUN_ID": "32046292720", "ENTRY_DAY_CASH_CHANGE_INR": "-62332.40",
    "ENTRY_DAY_NOTIONAL_TOTAL_INR": "62332.40", "STATUS": "verified", "NOTES": "test"}])
EQ = pd.DataFrame([
    {"DATE": "2026-06-08", "EQUITY": 200000.0, "CASH": 200000.0, "INVESTED": 0.0, "REALIZED_PNL": 0.0, "N_OPEN": 0},
    {"DATE": "2026-08-14", "EQUITY": 203308.67, "CASH": 161619.17, "INVESTED": 39755.9, "REALIZED_PNL": 1375.07, "N_OPEN": 8},
    {"DATE": "2026-08-17", "EQUITY": 203856.87, "CASH": 99286.77, "INVESTED": 102088.3, "REALIZED_PNL": 1375.07, "N_OPEN": 15}])


def row_of(d: dict):
    return next(reload(pd.DataFrame([d])).itertuples(index=False))


class HistoricalFee(unittest.TestCase):
    def test_evidence_and_the_run_record_establish_a_zero_fee(self):
        r = row_of(ROW)
        fee, src = ptm._fee_from_evidence(r, EVIDENCE, EQ)
        self.assertEqual(fee, 0.0)
        for part in ("no fee charged by the paper ledger at entry", "f800a30", "253fef6", "32046292720",
                     "CASH Rs99,286.77 = capital - INVESTED + REALIZED_PNL", "day's cash change Rs-62,332.40"):
            self.assertIn(part, src)
        h = ptm._holding_from_row(r, 0, SC, (fee, src))
        self.assertEqual((h.cost_basis, h.cost_note), (9029.5, ""))
        row = ptm._row_from_holding(h, r._asdict()) | {"entry_cost_inr": fee, "entry_fee_source": src}
        back = row_of(row)                                         # the recorded zero survives a reload ...
        self.assertEqual((back.entry_cost_inr, back.entry_fee_source), (0.0, src))
        h2 = ptm._holding_from_row(back, 0, SC)                    # ... and needs no evidence any more
        self.assertEqual((h2.cost_basis, h2.cost_note), (9029.5, ""))

    def test_without_evidence_the_fee_stays_unrecorded(self):
        r = row_of(ROW)
        self.assertEqual(ptm._fee_from_evidence(r, pd.DataFrame(), EQ), (None, ""))
        h = ptm._holding_from_row(r, 0, SC, (None, ""))
        self.assertEqual(h.cost_basis, 9029.5)
        self.assertEqual(h.cost_note, "entry cost not recorded (entered 2026-08-17); the cost basis excludes it")

    def test_evidence_must_match_the_row_and_the_run_record(self):
        cases = {
            "does not match the row": (ROW | {"qty": 6.0, "size_inr": 10835.4}, EVIDENCE, EQ),
            "that run's cash may include fees": (ROW, EVIDENCE, EQ.assign(CASH=EQ["CASH"] - [0, 0, 19.33])),
            "no row for the entry day": (ROW, EVIDENCE, EQ.iloc[:2]),
            "differs from the equity log": (ROW, EVIDENCE.assign(ENTRY_DAY_CASH_CHANGE_INR="-62000"), EQ),
            "only a historical fee of zero": (ROW, EVIDENCE.assign(HISTORICAL_ENTRY_FEE_INR="12"), EQ),
            "no equity log": (ROW, EVIDENCE, None),
        }
        for why, (row, ev, eq) in cases.items():
            r = row_of(row)
            fee, reason = ptm._fee_from_evidence(r, ev, eq)
            self.assertIsNone(fee, why)
            self.assertIn(why, reason)
            h = ptm._holding_from_row(r, 0, SC, (fee, reason))
            self.assertIn("entry cost not recorded", h.cost_note, why)
            self.assertIn("evidence not applied: " + reason, h.cost_note, why)

    def test_only_verified_evidence_is_loaded(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "ev.csv"
            pd.concat([EVIDENCE, EVIDENCE.assign(SYMBOL="DIVISLAB", STATUS="draft")]).to_csv(p, index=False)
            ev = ptm._load_fee_evidence(p)
            self.assertEqual(ev["SYMBOL"].tolist(), ["IPCALAB"])
            self.assertTrue(ptm._load_fee_evidence(Path(t) / "missing.csv").empty)

    def test_a_recorded_fee_is_never_replaced(self):
        r = row_of(ROW | {"entry_cost_inr": 19.2})
        self.assertEqual(ptm._fee_from_evidence(r, EVIDENCE, EQ), (None, ""))
        h = ptm._holding_from_row(r, 0, SC)
        self.assertEqual((round(h.cost_basis, 2), h.cost_note), (9048.7, ""))

    def test_cash_verification_accepts_the_established_zero_and_rejects_a_bare_one(self):
        src = ptm._fee_from_evidence(row_of(ROW), EVIDENCE, EQ)[1]
        eq = pd.DataFrame([EQ.iloc[0].to_dict(),
                           {"DATE": "2026-08-17", "EQUITY": 200000.0, "CASH": 200000.0 - 9029.5, "INVESTED": 9029.5,
                            "REALIZED_PNL": 0.0, "N_OPEN": 1}])
        good = pd.DataFrame([ROW | {"entry_cost_inr": 0.0, "entry_fee_source": src}])
        v = lv.verify_cash(good, eq)
        self.assertTrue(v["verified"], v["check"])
        self.assertIn("matches the recorded movements on all 2 dates", v["check"])
        bare = pd.DataFrame([ROW | {"entry_cost_inr": 0.0}])
        v = lv.verify_cash(bare, eq)
        self.assertFalse(v["verified"])
        self.assertIn("IPCALAB entered 2026-08-17 (entry fee recorded as 0 with no stated basis)", v["check"])


class Estimates(unittest.TestCase):
    EST = pd.DataFrame([{"SYMBOL": "IPCALAB", "ENTRY_DATE": "2026-08-17", "ESTIMATED_ENTRY_FEE_INR": "19.33",
                         "LABEL": "ESTIMATE (test)"}])

    def test_estimates_are_shown_apart_and_never_reach_cash_cost_or_pnl(self):
        src = ptm._fee_from_evidence(row_of(ROW), EVIDENCE, EQ)[1]
        pos = pd.DataFrame([ROW | {"entry_cost_inr": 0.0, "entry_fee_source": src, "origin": "entry"}])
        eq = pd.DataFrame([EQ.iloc[0].to_dict(), {"DATE": "2026-08-17", "EQUITY": 200000.0, "CASH": 190970.5,
                                                  "INVESTED": 9029.5, "REALIZED_PNL": 0.0, "N_OPEN": 1}])
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "est.csv"
            self.EST.to_csv(p, index=False)
            shown = lv.entry_fee_estimates(pos, p)
            self.assertEqual(shown[["symbol", "fee_charged", "estimate"]].values.tolist(), [["IPCALAB", 0.0, 19.33]])
            self.assertEqual(shown["label"].iloc[0], lv.ESTIMATE_LABEL)
            self.assertIn("no fee charged", shown["fee_charged_basis"].iloc[0])
            before = lv.verify_cash(pos, eq)
            self.EST.assign(ESTIMATED_ENTRY_FEE_INR="999").to_csv(p, index=False)   # estimates change ...
            self.assertEqual(lv.verify_cash(pos, eq), before)                       # ... cash does not
        self.assertTrue(before["verified"])
        self.assertEqual(ptm._holding_from_row(row_of(pos.iloc[0].to_dict()), 0, SC).cost_basis, 9029.5)
        # the ledger has no column an estimate could live in
        self.assertFalse([c for c in ptm.POSITIONS_COLS + ptm.REALIZED_COLS if "estimate" in c])
        mv = lv.cash_movements(pos)
        self.assertEqual(mv[["movement", "amount", "status"]].values.tolist(),
                         [["purchase (notional)", -9029.5, "recorded"], ["entry fee", -0.0, "recorded"]])


DAYS = ["20260701", "20260702", "20260703", "20260706", "20260707", "20260708"]


def merger_book(**fraction_terms):
    d0, d1, d2, d3, d4, d5 = DAYS
    bars = {("T", d0): (2400, 2420, 2390, 2408.9)}
    for d in DAYS[1:]:
        bars[("S", d)] = (4760, 4800, 4700, 4763)
    sec = sec_events([{"EVENT_ID": "T-m", "SYMBOL": "T", "TYPE": "merger_stock", "SUCCESSOR": "S", "RATIO_NUM": 51,
                       "RATIO_DEN": 100, "LAST_TRADE_DATE": d0, "TRADABLE_DATE": d2,
                       "FRACTION_TREATMENT": "trustee sale (test)", "STATUS": "verified", **fraction_terms}])
    bar = lambda s, d: dict(zip(("open", "high", "low", "close"), bars[(s, d)])) if (s, d) in bars else None
    b = Book(100000.0, bar, lambda s, d: None, lambda n, side, s, d: 0.0, None, None, sec)
    b.open_position("T", d0, 3, 2400.0, None, stop=2335.0)
    return b


class Fraction(unittest.TestCase):
    def test_fraction_and_its_trustee_sale_are_not_cash_without_documented_payment(self):
        b = merger_book()
        cash0 = b.cash
        for d in DAYS:
            comp = run_day(b, d)
            self.assertEqual(b.cash, cash0, d)                               # conversion and fraction: no cash
        frac = next(h for h in b.holdings if h.kind == "fraction")
        self.assertEqual((frac.state, round(frac.qty, 2), frac.cash_due), (PENDING, 0.53, 0.0))
        self.assertAlmostEqual(frac.cost_basis, 7200.0 * 0.53 / 1.53)
        self.assertAlmostEqual(comp["fraction_ref_unconfirmed"], 0.53 * 4763)     # reported ...
        self.assertAlmostEqual(comp["equity"], comp["cash"] + comp["open_mtm"])      # ... outside equity
        rows = ledger_rows(b)
        cls = lv.classify(rows)
        f = cls[cls.symbol == "S"].set_index("state")
        self.assertEqual(f.loc[lv.FRACTION, "owed"], 0.0)                     # not cash, not owed
        self.assertFalse(bool(f.loc[lv.FRACTION, "slot"]))

    def test_documented_proceeds_are_owed_then_cash_on_the_pay_date(self):
        d0, d1, d2, d3, d4, d5 = DAYS
        b = merger_book(FRACTION_CASH_PER_SHARE="4701.30", FRACTION_ROUNDING="ceil_rupee", FRACTION_PAY_DATE=d5)
        cash0 = b.cash
        for d in DAYS[:-1]:
            comp = run_day(b, d)
        frac = next(h for h in b.holdings if h.kind == "fraction")
        self.assertEqual((frac.state, frac.cash_due, frac.cash_date), (PENDING, 2492.0, d5))   # ceil(0.53 x 4701.30)
        self.assertEqual(b.cash, cash0)                                      # owed, still not cash
        self.assertAlmostEqual(comp["pending_ref"], 2492.0)
        self.assertEqual(comp["fraction_ref_unconfirmed"], 0.0)
        rows = ledger_rows(b)
        back = reload(rows)                                                  # owed amount survives a reload
        fr = next(r for r in back.itertuples(index=False) if r.kind == "fraction")
        h = ptm._holding_from_row(fr, 0, SC)
        self.assertEqual((h.kind, h.state, h.cash_due, h.cash_date), ("fraction", PENDING, 2492.0, d5))
        cls = lv.classify(rows)
        owed = cls[cls.state == lv.FRACTION_OWED]
        self.assertEqual((owed.owed.tolist(), bool(owed.slot.iloc[0])), ([2492.0], False))
        run_day(b, d5)
        self.assertAlmostEqual(b.cash - cash0, 2492.0)
        t = next(t for t in b.trades if t["reason"] == "FRACTION_CASH")
        self.assertAlmostEqual(t["pnl"], 2492.0 - 7200.0 * 0.53 / 1.53)
        self.assertEqual(frac.state, CLOSED)

    def test_cash_verification_carries_the_fraction_cost_and_never_counts_it(self):
        b = merger_book()
        for d in DAYS:
            run_day(b, d)
        rows = ledger_rows(b)
        eq = pd.DataFrame([{"DATE": "2026-06-30", "EQUITY": 100000.0, "CASH": 100000.0, "INVESTED": 0.0,
                            "REALIZED_PNL": 0.0, "N_OPEN": 0},
                           {"DATE": "2026-07-08", "EQUITY": 0.0, "CASH": round(b.cash, 2),
                            "INVESTED": round(sum(h.cost_basis for h in b.active() if h.state == OPEN), 2),
                            "REALIZED_PNL": 0.0, "N_OPEN": 1, "CASH_NOTE": ptm._cash_note(rows)}])
        s = lv.summary(rows, eq.assign(EQUITY=lambda e: e["CASH"] + [0, 4763.0]))
        self.assertTrue(s["cash_verified"], s["cash_check"])
        self.assertEqual(s["available_cash"], 92800.0)                       # 100000 - 7200: no fraction cash
        self.assertEqual(s["owed"], 0.0)


class Provisional(unittest.TestCase):
    def test_back_dated_replay_exits_are_provisional_and_block_cash_verification(self):
        d0, d1, d2, d3, d4, d5 = DAYS
        bars = {("T", d0): (2400, 2420, 2390, 2408.9), ("A", d0): (100, 101, 99, 100), ("A", d1): (95, 96, 80, 85)}
        for d in DAYS[1:]:
            bars[("S", d)] = (4760, 4800, 4700, 4763)
        sec = sec_events([{"EVENT_ID": "T-m", "SYMBOL": "T", "TYPE": "merger_stock", "SUCCESSOR": "S", "RATIO_NUM": 51,
                           "RATIO_DEN": 100, "LAST_TRADE_DATE": d0, "TRADABLE_DATE": d2,
                           "FRACTION_TREATMENT": "trustee sale (test)", "STATUS": "verified"}])
        bar = lambda s, d: dict(zip(("open", "high", "low", "close"), bars[(s, d)])) if (s, d) in bars else None
        capital = 1000.0 + 7200.0
        rb = Book(capital, bar, lambda s, d: None, lambda n, side, s, d: 0.0, None, None, sec)
        rb.open_position("A", d0, 10, 100.0, None, stop=90.0)
        rb.open_position("T", d0, 3, 2400.0, None, stop=2335.0)
        for d in DAYS[:3]:
            run_day(rb, d)
        why = ptm._replay_backdated(rb, "20260708", "20260707")      # history already recorded to 7 Jul
        self.assertIn("A STOP_LOSS on 2026-07-02 falls before sessions already recorded (to 2026-07-07)", why)
        ptm._mark_replay(rb, why)
        sale = next(t for t in rb.trades if t["symbol"] == "A")
        self.assertEqual((sale["reason"], sale["provisional"]), ("STOP_LOSS", why))
        self.assertEqual(ptm._trade_to_realized(sale)["provisional"], why)
        active = [h for h in rb.holdings if h.state != CLOSED]
        self.assertEqual({(h.symbol, h.kind) for h in active}, {("S", "shares"), ("S", "fraction")})
        self.assertTrue(all(h.provisional == why for h in rb.holdings))
        rows = ledger_rows(rb)
        self.assertIn("A closed 2026-07-02: provisional exit (back-dated by a replay", ptm._cash_note(rows))
        eq = pd.DataFrame([{"DATE": "2026-06-30", "EQUITY": capital, "CASH": capital, "INVESTED": 0.0,
                            "REALIZED_PNL": 0.0, "N_OPEN": 0},
                           {"DATE": "2026-07-03", "EQUITY": capital, "CASH": round(rb.cash, 2),
                            "INVESTED": 4763.0, "REALIZED_PNL": -100.0, "N_OPEN": 1}])
        self.assertAlmostEqual(rb.cash, 900.0)
        v = lv.verify_cash(rows, eq)
        self.assertFalse(v["verified"])
        self.assertIn("A closed 2026-07-02 (cash Rs900.00 from a provisional exit: back-dated by a replay", v["check"])
        self.assertNotIn("zero would give", v["check"])

    def test_successor_of_a_provisional_holding_stays_provisional(self):
        b = merger_book()
        b.holdings[0].provisional = "test reason"
        for d in DAYS:
            run_day(b, d)
        self.assertEqual({h.provisional for h in b.holdings}, {"test reason"})


class CashHistory(unittest.TestCase):
    def setUp(self):
        from tests.test_cash_integrity import CashReconciliation
        self.case = CashReconciliation("test_every_movement_reconciles_every_day")
        self.case.setUp()
        self.logs = [{"DATE": "2025-12-31", "EQUITY": self.case.CAPITAL, "CASH": self.case.CAPITAL, "INVESTED": 0.0,
                      "REALIZED_PNL": 0.0, "N_OPEN": 0}]
        for d in self.case.DAYS:
            run_day(self.case.book, d)
            rows, eq, realized, comp = self.case.snapshot(d)
            self.logs.append(eq.iloc[-1].to_dict())
        self.rows, self.realized = rows, realized

    def test_every_logged_date_is_rebuilt_from_dated_movements(self):
        eq = pd.DataFrame(self.logs)
        h = lv.cash_history(self.rows, eq)
        self.assertEqual((h["dates"], h["compared"], h["mismatches"], h["inconclusive_from"]), (6, 6, [], ""))
        v = lv.verify_cash(self.rows, eq, self.realized)
        self.assertTrue(v["verified"], v["check"])
        self.assertIn("cash history: the logged cash matches the recorded movements on all 6 dates", v["check"])
        mv = lv.cash_movements(self.rows)
        self.assertEqual(set(mv.status), {"recorded"})
        self.assertIn("settlement (CASH_DIVIDEND)", set(mv.movement))

    def test_a_past_date_that_disagrees_is_named(self):
        eq = pd.DataFrame(self.logs)
        eq.loc[3, "CASH"] += 250.0                                           # one historical day is off
        v = lv.verify_cash(self.rows, eq, self.realized)
        self.assertFalse(v["verified"])
        self.assertIn(f"differs from the recorded movements on 1 of 6 dates (first {self.case.DAYS[2]}", v["check"])

    def test_an_unrecorded_fee_makes_later_dates_inconclusive(self):
        pos = pd.DataFrame([ROW | {"origin": "entry"}])
        h = lv.cash_history(pos, EQ.iloc[[0, 2]])
        self.assertEqual((h["inconclusive_from"], h["compared"], h["mismatches"]), ("20260817", 1, []))
        self.assertIn("from then on a movement has no recorded amount", h["text"])


class EndToEnd(unittest.TestCase):
    """paper_trade_manager.run on temporary files: evidence applied before the merger replay, the replay's exit
    recorded in its own session (not back-dated), the fraction outside cash, and a new entry's fee recorded as
    charged. Dates after 3 Sep 2026: today's exit rules and fees are in force throughout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        self.saved = (data_integrity.QUARANTINE, security_master.SYMBOL_CHANGES, security_master.SYMBOL_CHANGES_CURATED,
                      security_master.SUPPLEMENT, security_master.SECURITY_EVENTS)
        data_integrity.QUARANTINE = t / "none.csv"
        security_master.SYMBOL_CHANGES = security_master.SYMBOL_CHANGES_CURATED = t / "none2.csv"
        security_master.SUPPLEMENT = t / "none3.csv"
        (t / "none3.csv").write_text(SUPPLEMENT_HEADER)       # header only: deliberately no supplementary prices
        security_master.SECURITY_EVENTS = t / "sec.csv"
        days = list(pd.bdate_range("2026-09-07", periods=40).strftime("%Y%m%d"))
        self.days = days
        E, last_jb, tradable = days[15], days[20], days[23]
        rows = []
        for i, d in enumerate(days):
            rows.append({"DATE": d, "SYMBOL": "IPC", "SERIES": "EQ", "OPEN": 50, "HIGH": 51, "LOW": 49, "CLOSE": 50,
                         "TRADED_VALUE": 1e8})
            rows.append({"DATE": d, "SYMBOL": "NEW", "SERIES": "EQ", "OPEN": 20, "HIGH": 21, "LOW": 19, "CLOSE": 20,
                         "TRADED_VALUE": 1e8})
            if d <= last_jb:
                rows.append({"DATE": d, "SYMBOL": "JB", "SERIES": "EQ", "OPEN": 100, "HIGH": 101, "LOW": 99,
                             "CLOSE": 100, "TRADED_VALUE": 1e8})
            tp = (185, 186, 170, 172) if d == days[-1] else (196, 198, 194, 196)
            rows.append({"DATE": d, "SYMBOL": "TP", "SERIES": "EQ", **dict(zip(("OPEN", "HIGH", "LOW", "CLOSE"), tp)),
                         "TRADED_VALUE": 1e8})
        pd.DataFrame(rows).to_csv(t / "master.csv", index=False)
        sec_events([{"EVENT_ID": "JB-m", "SYMBOL": "JB", "TYPE": "merger_stock", "SUCCESSOR": "TP", "RATIO_NUM": 51,
                     "RATIO_DEN": 100, "LAST_TRADE_DATE": last_jb, "TRADABLE_DATE": tradable,
                     "FRACTION_TREATMENT": "trustee sale (test)", "STATUS": "verified"}]).to_csv(t / "sec.csv", index=False)
        iso = ptm._iso
        old = lambda sym, px, qty, stop: {"symbol": sym, "signal_date": days[14], "entry_date": iso(E),
                                          "entry_price": px, "qty": qty, "size_inr": px * qty, "initial_stop": stop,
                                          "current_stop": stop, "highest_close": px, "atr_at_entry": 1.0,
                                          "status": "open", "exit_date": "", "exit_price": "", "exit_reason": "",
                                          "realized_pnl_inr": "", "realized_pct": "", "entry_cost_inr": "",
                                          "exit_cost_inr": ""}
        pd.DataFrame([old("IPC", 50.0, 10, 45.0), old("JB", 100.0, 3, 90.0)]).to_csv(t / "positions.csv", index=False)
        cap, inv = 100000.0, 800.0
        pd.DataFrame([{"DATE": iso(days[0]), "EQUITY": cap, "CASH": cap, "MTM_OPEN": 0.0, "INVESTED": 0.0,
                       "REALIZED_PNL": 0.0, "N_OPEN": 0},
                      {"DATE": iso(E), "EQUITY": cap, "CASH": cap - inv, "MTM_OPEN": inv, "INVESTED": inv,
                       "REALIZED_PNL": 0.0, "N_OPEN": 2},
                      {"DATE": iso(days[-2]), "EQUITY": cap, "CASH": cap - inv, "MTM_OPEN": inv, "INVESTED": inv,
                       "REALIZED_PNL": 0.0, "N_OPEN": 2}]).to_csv(t / "equity_log.csv", index=False)
        ev = lambda sym, px, qty: {"SYMBOL": sym, "ENTRY_DATE": iso(E), "QTY": str(qty), "ENTRY_PRICE": str(px),
                                   "NOTIONAL_INR": f"{px * qty:.2f}", "HISTORICAL_ENTRY_FEE_INR": "0",
                                   "CODE_IN_FORCE": "test code: no fee step", "LEDGER_COMMIT": "abc1234",
                                   "RUN_ID": "1", "ENTRY_DAY_CASH_CHANGE_INR": f"{-inv:.2f}",
                                   "ENTRY_DAY_NOTIONAL_TOTAL_INR": f"{inv:.2f}", "STATUS": "verified", "NOTES": ""}
        pd.DataFrame([ev("IPC", 50.0, 10), ev("JB", 100.0, 3)]).to_csv(t / "evidence.csv", index=False)
        pd.DataFrame([{"DATE": days[-2], "SYMBOL": "NEW", "CLOSE": 20.0, "SIZE_INR": 400.0, "STOP_PRICE": 18.0,
                       "CONFIDENCE": 7.0}]).to_csv(t / "signal.csv", index=False)

    def tearDown(self):
        (data_integrity.QUARANTINE, security_master.SYMBOL_CHANGES, security_master.SYMBOL_CHANGES_CURATED,
         security_master.SUPPLEMENT, security_master.SECURITY_EVENTS) = self.saved
        self.tmp.cleanup()

    def run_ledger(self, evidence="evidence.csv"):
        t = self.t
        args = argparse.Namespace(master=str(t / "master.csv"), signal=str(t / "signal.csv"),
                                  positions=str(t / "positions.csv"), equity_log=str(t / "equity_log.csv"),
                                  realized=str(t / "realized.csv"), corporate_actions=str(t / "none_ca.csv"),
                                  portfolio_inr=100000.0, entry_day="any", max_holding_days=0, max_open_positions=15,
                                  dry_run=True, fee_evidence=str(t / evidence))
        return ptm.run(args)

    def test_run_records_historical_zero_fees_and_keeps_estimates_and_fractions_out(self):
        out = self.run_ledger()
        pos = out["positions"].set_index("hid")
        ipc = pos[(pos.symbol == "IPC")].iloc[0]
        jb = pos[(pos.symbol == "JB")].iloc[0]
        for r in (ipc, jb):
            self.assertEqual(r.entry_cost_inr, 0.0)
            self.assertIn("no fee charged by the paper ledger at entry: test code: no fee step", r.entry_fee_source)
            self.assertIn("abc1234", r.entry_fee_source)
            self.assertTrue(pd.isna(r.cost_note) or r.cost_note == "")
        self.assertEqual((ipc.status, ipc.cost_basis), ("open", 500.0))
        self.assertEqual((jb.status, jb.cost_basis), ("converted", 300.0))
        tp = pos[(pos.symbol == "TP") & (pos.kind == "shares")].iloc[0]
        frac = pos[(pos.symbol == "TP") & (pos.kind == "fraction")].iloc[0]
        self.assertEqual((tp.status, tp.exit_reason, tp.exit_date), ("closed", "STOP_LOSS", ptm._iso(self.days[-1])))
        self.assertAlmostEqual(tp.exit_price, round(92.0 / 0.51, 4))             # max(90, 100 - 4 x 2) / 0.51
        self.assertTrue(pd.isna(tp.provisional) or tp.provisional == "")        # exit in its own session
        self.assertAlmostEqual(tp.cost_basis, round(300.0 / 1.53, 2))
        self.assertEqual(frac.status, "pending")
        self.assertTrue(pd.isna(pd.to_numeric(frac.cash_due, errors="coerce")))       # no proceeds documented
        self.assertAlmostEqual(frac.cost_basis, round(300.0 * 0.53 / 1.53, 2))
        new = pos[pos.symbol == "NEW"].iloc[0]
        self.assertEqual((new.size_inr, new.entry_fee_source), (20.0 * new.qty, ptm.FEE_CHARGED))
        self.assertGreater(new.entry_cost_inr, 0)
        self.assertAlmostEqual(new.cost_basis, new.size_inr + new.entry_cost_inr, places=2)
        e = out["eq_row"]
        self.assertEqual(e["CASH_NOTE"], "")
        active = out["positions"][out["positions"].status.isin(ptm.ACTIVE)]
        realised = pd.to_numeric(out["positions"].loc[out["positions"].status == "closed", "realized_pnl_inr"]).sum()
        self.assertAlmostEqual(e["CASH"], 100000.0 - pd.to_numeric(active.cost_basis).sum() + realised, places=2)
        eq = pd.concat([pd.read_csv(self.t / "equity_log.csv"), pd.DataFrame([e])], ignore_index=True)
        v = lv.verify_cash(out["positions"], eq, out["realized"])
        self.assertTrue(v["verified"], v["check"])
        self.assertEqual(v["history"]["mismatches"], [])                       # every logged date reconciles
        self.assertEqual(v["history"]["compared"], 4)
        self.assertFalse([c for c in out["positions"].columns if "estimate" in c])

    def test_without_evidence_the_fees_stay_unrecorded(self):
        out = self.run_ledger(evidence="missing.csv")
        note = out["eq_row"]["CASH_NOTE"]
        self.assertIn("IPC: entry cost not recorded", note)
        self.assertIn("TP closed", note)
        ipc = out["positions"][out["positions"].symbol == "IPC"].iloc[0]
        self.assertTrue(pd.isna(pd.to_numeric(ipc.entry_cost_inr, errors="coerce")))

    def test_evidence_contradicted_by_the_equity_log_is_not_applied(self):
        eq = pd.read_csv(self.t / "equity_log.csv")
        eq.loc[1, "CASH"] -= 1.0                                               # entry-day cash shows a fee
        eq.to_csv(self.t / "equity_log.csv", index=False)
        out = self.run_ledger()
        ipc = out["positions"][out["positions"].symbol == "IPC"].iloc[0]
        self.assertIn("evidence not applied: the equity log on", ipc.cost_note)
        self.assertTrue(pd.isna(pd.to_numeric(ipc.entry_cost_inr, errors="coerce")))


if __name__ == "__main__":
    unittest.main()
