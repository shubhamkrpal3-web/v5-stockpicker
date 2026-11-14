"""Exit rules in force by date, one price basis for ATR / rolling low / stops, dates kept as text, back-dated
replays and the corrected replay. TEST VALUES ONLY."""
import argparse
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))
sys.path.insert(0, str(ROOT))

import corrected_replay as cr  # noqa: E402
import data_integrity  # noqa: E402
import ledger_view as lv  # noqa: E402
import paper_trade_manager as ptm  # noqa: E402
import security_master  # noqa: E402
from corporate_actions import load_events, share_count_events  # noqa: E402
from portfolio_accounting import CLOSED, OPEN, STALE, Book  # noqa: E402
from tests.test_accounting import sec_events  # noqa: E402


SUPPLEMENT_HEADER = "DATE,SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,VOLUME,TRADED_VALUE,SOURCE\n"


def bars_book(bars, atr=None, low=None, rules=None, time_stop=None):
    bar = lambda s, d: dict(zip(("open", "high", "low", "close"), bars[(s, d)])) if (s, d) in bars else None
    return Book(1e6, bar, lambda s, d: (atr or {}).get((s, d)), lambda n, side, s, d: 0.0, None, None, None,
                stop_rules=rules, low=(lambda s, d, n: (low or {}).get((s, d, n))), time_stop=time_stop)


class RuleSchedule(unittest.TestCase):
    def test_the_rule_in_force_on_each_session(self):
        b = bars_book({}, rules=ptm.PAPER_RULES)
        self.assertEqual(b.stop_rule("20260713"), (3.0, 22, 25))
        self.assertEqual(b.stop_rule("20260902"), (3.0, 22, 25))
        self.assertEqual(b.stop_rule("20260903"), (4.0, None, None))
        self.assertEqual(Book(0.0, None, None, None).stop_rule("20260902"), (4.0, None, None))   # backtest: unchanged

    def test_trailing_stop_follows_the_rule_and_is_never_lowered_when_it_changes(self):
        days = ["20260901", "20260902", "20260903", "20260904"]
        b = bars_book({("A", d): (100, 101, 99, 100) for d in days}, {("A", d): 2.0 for d in days},
                      {("A", d, 22): 97.0 for d in days}, ptm.PAPER_RULES)
        h = b.open_position("A", "20260831", 10, 100.0, None, stop=90.0)
        b.end_of_day("20260901")
        self.assertEqual(h.stop, 97.0)          # 3 x ATR -> 94; the 22-session low 97 is higher
        b.end_of_day("20260903")
        self.assertEqual(h.stop, 97.0)          # 4 x ATR -> 92 and no rolling low: 97 is kept, not lowered
        h.hi = 110.0
        b.end_of_day("20260904")
        self.assertEqual(h.stop, 102.0)         # 110 - 4 x 2

    def test_calendar_time_stop_applies_only_while_in_force(self):
        days = ["20260901", "20260903", "20260910"]
        b = bars_book({(s, d): (100, 101, 99, 100) for s in ("A", "B") for d in days}, rules=ptm.PAPER_RULES)
        a = b.open_position("A", "20260807", 1, 100.0, None, stop=90.0)
        c = b.open_position("B", "20260815", 1, 100.0, None, stop=90.0)
        b.check_exits("20260901")               # A: 25 calendar days -> time stop at the close
        self.assertEqual((a.state, b.trades[0]["reason"], b.trades[0]["exit"]), (CLOSED, "TIME_STOP", 100.0))
        b.check_exits("20260903"); b.check_exits("20260910")
        self.assertEqual(c.state, OPEN)         # B passes 25 days on 9 Sep, after the time stop was dropped

    def test_without_a_schedule_the_backtest_rules_are_unchanged(self):
        days = ["20260901", "20260902"]
        b = bars_book({("A", d): (100, 101, 99, 100) for d in days}, {("A", d): 2.0 for d in days},
                      {("A", d, 22): 99.0 for d in days}, rules=None, time_stop=2)
        h = b.open_position("A", "20260831", 1, 100.0, None, stop=80.0)
        b.end_of_day("20260901")
        self.assertEqual(h.stop, 92.0)          # 4 x ATR only, rolling low ignored
        b.check_exits("20260901"); b.check_exits("20260902")
        self.assertEqual((h.state, b.trades[0]["reason"]), (CLOSED, "TIME_STOP"))    # session-count time stop


class Patched(unittest.TestCase):
    """Temporary derived-data paths, so nothing reads or writes the repo's data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.saved = (data_integrity.QUARANTINE, security_master.SYMBOL_CHANGES, security_master.SYMBOL_CHANGES_CURATED,
                      security_master.SUPPLEMENT, security_master.SECURITY_EVENTS)
        data_integrity.QUARANTINE = self.t / "none.csv"
        security_master.SYMBOL_CHANGES = security_master.SYMBOL_CHANGES_CURATED = self.t / "none2.csv"
        security_master.SUPPLEMENT = self.t / "none3.csv"
        (self.t / "none3.csv").write_text(SUPPLEMENT_HEADER)  # header only: deliberately no supplementary prices
        security_master.SECURITY_EVENTS = self.t / "sec.csv"

    def tearDown(self):
        (data_integrity.QUARANTINE, security_master.SYMBOL_CHANGES, security_master.SYMBOL_CHANGES_CURATED,
         security_master.SUPPLEMENT, security_master.SECURITY_EVENTS) = self.saved
        self.tmp.cleanup()


class PriceBasis(Patched):
    """ATR, the 22-session low and stops are all in each session's as-traded prices, across a 2:1 split."""

    def test_split_inside_the_window(self):
        days = list(pd.bdate_range("2026-06-01", periods=30).strftime("%Y%m%d"))
        rows = []
        for i, d in enumerate(days):
            px = (200, 204, 180 if i == 2 else 196, 200) if i < 20 else (100, 102, 98, 100)
            rows.append(dict(zip(("DATE", "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"),
                                 (d, "X", "EQ", *px, 1e8))))
        pd.DataFrame(rows).to_csv(self.t / "master.csv", index=False)
        pd.DataFrame([{"DATE": days[0], "SYMBOL": "Z", "SERIES": "BE", "OPEN": 1, "HIGH": 1, "LOW": 1, "CLOSE": 1,
                       "VOLUME": 1, "TRADED_VALUE": 1, "SOURCE": "test"}]).to_csv(security_master.SUPPLEMENT, index=False)
        pd.DataFrame([{"SYMBOL": "X", "EX_DATE": days[20], "KIND": "split", "FACTOR": 0.5, "FACTOR_TYPE": "official_ratio",
                       "SOURCE": "nse", "STATUS": "ok", "OBS_RATIO": "", "SUBJECT": "test split"}]).to_csv(self.t / "ca.csv", index=False)
        raw, atr, lows, tv, sessions = ptm._load_market(self.t / "master.csv", days[0], self.t / "ca.csv", (22,))
        self.assertEqual((atr[("X", days[19])], lows[22][("X", days[19])]), (8.0, 180.0))   # before: pre-split prices
        self.assertEqual((atr[("X", days[21])], lows[22][("X", days[21])]), (4.0, 90.0))    # after: post-split prices
        self.assertEqual(raw.loc[("X", days[21]), "CLOSE"], 100)                           # stops are checked on these
        ev = load_events(self.t / "ca.csv")
        b = Book(1e6, lambda s, d: None, None, lambda n, side, s, d: 0.0, share_count_events(ev), None, None)
        h = b.open_position("X", days[10], 5, 200.0, None, stop=190.0)
        b.apply_events(days[20])                                                            # the split rescales the stop
        self.assertEqual((h.qty, h.stop), (10.0, 95.0))


class DatesStayText(unittest.TestCase):
    ROW = {"symbol": "A", "signal_date": "20260612", "entry_date": "2026-06-15", "entry_price": 10.0, "qty": 3.0,
           "status": "open", "tradable_from": "", "last_mark_date": "20261001", "cash_date": "", "kind": "shares"}

    def cycle(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "positions.csv"
            p.write_text(text, encoding="utf-8")
            ptm._load_positions(p).to_csv(p, index=False)
            return p.read_text(encoding="utf-8")

    def test_blank_dates_stay_blank_and_dates_never_gain_dot_zero(self):
        derived = self.ROW | {"symbol": "S", "signal_date": "", "tradable_from": "20260728"}
        first = pd.DataFrame([self.ROW, derived]).reindex(columns=ptm.POSITIONS_COLS).to_csv(index=False)
        once = self.cycle(first)
        twice = self.cycle(once)
        self.assertEqual(once, twice)
        self.assertNotRegex(twice, r"\d{8}\.0")
        rows = list(csv.DictReader(io.StringIO(twice)))
        self.assertEqual([(r["signal_date"], r["tradable_from"], r["last_mark_date"], r["cash_date"]) for r in rows],
                         [("20260612", "", "20261001", ""), ("", "20260728", "20261001", "")])

    def test_dates_left_with_dot_zero_by_an_earlier_round_trip_are_cleaned(self):
        bad = pd.DataFrame([self.ROW | {"signal_date": "20260612.0", "last_mark_date": "20261001.0"}]
                           ).reindex(columns=ptm.POSITIONS_COLS).to_csv(index=False)
        row = next(csv.DictReader(io.StringIO(self.cycle(bad))))
        self.assertEqual((row["signal_date"], row["last_mark_date"]), ("20260612", "20261001"))


class BackDated(unittest.TestCase):
    def test_a_replay_result_before_the_first_new_session_is_back_dated(self):
        rb = Book(0.0, None, None, None)
        rb.trades.append({"exit_date": "20260821", "symbol": "S", "reason": "TIME_STOP"})
        why = ptm._replay_backdated(rb, "20260825", "20260824")
        self.assertIn("S TIME_STOP on 2026-08-21 falls before sessions already recorded (to 2026-08-24)", why)
        self.assertIn("need a corrected replay from 2026-08-21", why)
        self.assertEqual(ptm._replay_backdated(rb, "20260821", "20260820"), "")         # recorded in its own session

    def test_a_change_in_slot_use_is_back_dated_too(self):
        rb = Book(0.0, None, None, None)
        rb.open_position = None
        from portfolio_accounting import Holding
        rb.holdings += [Holding(hid="a", symbol="P", qty=1), Holding(hid="b", symbol="C", qty=1, state="pending")]
        rb.log.append({"date": "20260810", "hid": "b", "symbol": "C", "event": "pending entitlement"})
        self.assertIn("slot use (2 slots instead of 1) on 2026-08-10", ptm._replay_backdated(rb, "20260825", "20260824"))


class CorrectedReplay(Patched):
    """A merger lot that a time stop closes on 21 Aug frees a slot (capacity 2) for the 24 Aug entries."""
    CAP = 10_000.0

    def setUp(self):
        super().setUp()
        t = self.t
        days = list(pd.bdate_range("2026-07-24", "2026-08-25").strftime("%Y%m%d"))
        self.days = days
        rows = []
        for d in days:
            first = d == days[0]
            px = {"A": (100, 101, 80 if first else 97, 100), "S": (60, 61, 50 if first else 55, 60),
                  "N1": (20, 21, 15 if first else 19, 20), "N2": (30, 31, 25 if first else 29, 30)}
            if d <= "20260729":
                px["M"] = (50, 51, 40 if first else 45, 50)
            for sym, (o, h, lo, c) in px.items():
                rows.append({"DATE": d, "SYMBOL": sym, "SERIES": "EQ", "OPEN": o, "HIGH": h, "LOW": lo, "CLOSE": c,
                             "TRADED_VALUE": 1e8})
        pd.DataFrame(rows).to_csv(t / "master.csv", index=False)
        self.merger = sec_events([{"EVENT_ID": "M-m", "SYMBOL": "M", "TYPE": "merger_stock", "SUCCESSOR": "S",
                                   "RATIO_NUM": 1, "RATIO_DEN": 1, "LAST_TRADE_DATE": "20260729",
                                   "TRADABLE_DATE": "20260731", "FRACTION_TREATMENT": "none", "STATUS": "verified"}])
        self.merger.to_csv(t / "sec.csv", index=False)
        self.merger.iloc[0:0].to_csv(t / "sec_none.csv", index=False)
        base = t / "base"
        base.mkdir()
        old = lambda sym, px, qty, stop: {"symbol": sym, "signal_date": "20260724", "entry_date": "2026-07-27",
                                          "entry_price": px, "qty": qty, "size_inr": px * qty, "initial_stop": stop,
                                          "current_stop": stop, "highest_close": px, "atr_at_entry": 1.0, "status": "open",
                                          "entry_cost_inr": ""}
        pd.DataFrame([old("A", 100.0, 10, 90.0), old("M", 50.0, 2, 42.0)]).to_csv(base / "positions.csv", index=False)
        eq = [{"DATE": "2026-07-24", "EQUITY": self.CAP, "CASH": self.CAP, "MTM_OPEN": 0.0, "INVESTED": 0.0,
               "REALIZED_PNL": 0.0, "N_OPEN": 0}]
        eq += [{"DATE": d, "EQUITY": self.CAP, "CASH": self.CAP - 1100, "MTM_OPEN": 1100.0, "INVESTED": 1100.0,
                "REALIZED_PNL": 0.0, "N_OPEN": 2} for d in ("2026-07-27", "2026-07-28")]
        pd.DataFrame(eq).to_csv(base / "equity_log.csv", index=False)
        ev = lambda sym, px, qty: {"SYMBOL": sym, "ENTRY_DATE": "2026-07-27", "QTY": str(qty), "ENTRY_PRICE": str(px),
                                   "NOTIONAL_INR": f"{px * qty:.2f}", "HISTORICAL_ENTRY_FEE_INR": "0",
                                   "CODE_IN_FORCE": "test", "LEDGER_COMMIT": "t", "RUN_ID": "1",
                                   "ENTRY_DAY_CASH_CHANGE_INR": "-1100.00", "ENTRY_DAY_NOTIONAL_TOTAL_INR": "1100.00",
                                   "STATUS": "verified", "NOTES": ""}
        pd.DataFrame([ev("A", 100.0, 10), ev("M", 50.0, 2)]).to_csv(t / "ev.csv", index=False)
        (t / "signals").mkdir()
        pd.DataFrame([{"DATE": "20260821", "SYMBOL": "N1", "CLOSE": 20.0, "SIZE_INR": 400.0, "STOP_PRICE": 18.0,
                       "CONFIDENCE": 8.0},
                      {"DATE": "20260821", "SYMBOL": "N2", "CLOSE": 30.0, "SIZE_INR": 600.0, "STOP_PRICE": 27.0,
                       "CONFIDENCE": 7.0}]).to_csv(t / "signals" / "20260824.csv", index=False)

    def rebuild(self, out, events="sec.csv", end=None):
        security_master.SECURITY_EVENTS = self.t / events
        return cr.rebuild(self.t / "base", self.t / "master.csv", self.t / "signals", self.t / out, "20260729", end,
                          fee_evidence=self.t / "ev.csv", corporate_actions=self.t / "none_ca.csv",
                          portfolio_inr=self.CAP, max_open_positions=2)

    def read(self, out):
        return {f: pd.read_csv(self.t / out / f) for f in ("positions.csv", "equity_log.csv", "realized_trades.csv")}

    def test_rebuild_uses_the_freed_slot_and_reconciles(self):
        m = self.rebuild("corr")
        r = self.read("corr")
        t = r["realized_trades.csv"].set_index("symbol")
        self.assertEqual((t.loc["S", "exit_date"], t.loc["S", "exit_reason"], t.loc["S", "pnl_inr"]),
                         ("2026-08-21", "TIME_STOP", 20.0))                      # 25 calendar days, at the close, no fee
        self.assertEqual((t.loc["A", "exit_date"], t.loc["A", "exit_reason"]), ("2026-08-21", "TIME_STOP"))
        pos = r["positions.csv"]
        new = pos[pos.entry_date == "2026-08-24"].set_index("symbol")
        self.assertEqual(sorted(new.index), ["N1", "N2"])                         # both slots refilled
        self.assertEqual(set(new.entry_fee_source), {ptm.FEE_NONE_BEFORE_MODEL})
        self.assertEqual(set(new.entry_cost_inr), {0.0})
        v = lv.verify_cash(pos, r["equity_log.csv"], r["realized_trades.csv"])
        self.assertTrue(v["verified"], v["check"])
        self.assertEqual(v["cash"], 9020.0)
        self.assertEqual(v["history"]["mismatches"], [])
        eq = r["equity_log.csv"]
        self.assertEqual(set(eq.loc[eq.DATE < "2026-07-29", "SOURCE"]), {cr.RECORDED})
        self.assertEqual(set(eq.loc[eq.DATE >= "2026-07-29", "SOURCE"]), {cr.LABEL})
        self.assertEqual((m["label"], m["start"], m["end"], list(m["signals_by_entry_session"])),
                         (cr.LABEL, "20260729", "20260825", ["20260824"]))
        self.assertEqual(json.loads((self.t / "corr" / "REPLAY_MANIFEST.json").read_text())["base"], str(self.t / "base"))
        # control: without the merger the lot stays held (stale) and only one of the two entries fits
        self.rebuild("ctrl", events="sec_none.csv")
        c = self.read("ctrl")["positions.csv"]
        self.assertEqual(sorted(c[c.entry_date == "2026-08-24"].symbol), ["N1"])
        self.assertEqual(c.loc[c.symbol == "M", "status"].item(), STALE)     # no prints: stale, its slot still taken

    def test_rebuild_is_repeatable_and_a_further_run_changes_nothing(self):
        self.rebuild("one"); self.rebuild("two")
        files = ("positions.csv", "equity_log.csv", "realized_trades.csv")
        for f in files:
            self.assertEqual((self.t / "one" / f).read_bytes(), (self.t / "two" / f).read_bytes(), f)
        args = argparse.Namespace(master=str(self.t / "master.csv"), signal=str(self.t / "none.csv"),
                                  positions=str(self.t / "one" / "positions.csv"),
                                  equity_log=str(self.t / "one" / "equity_log.csv"),
                                  realized=str(self.t / "one" / "realized_trades.csv"),
                                  corporate_actions=str(self.t / "none_ca.csv"), portfolio_inr=self.CAP, entry_day="mon",
                                  max_holding_days=0, max_open_positions=2, dry_run=False,
                                  fee_evidence=str(self.t / "ev.csv"), counterfactual=False)
        out = ptm.run(args)
        self.assertEqual((out["replays"], out["book"].trades), ([], []))       # no second merger, no second exit
        ptm.write_outputs(args, out)
        for f in files:
            self.assertEqual((self.t / "one" / f).read_bytes(), (self.t / "two" / f).read_bytes(), f)

    def test_never_written_into_the_live_folder(self):
        with self.assertRaises(ValueError):
            cr.rebuild(self.t / "base", self.t / "master.csv", self.t / "signals", cr.LIVE_DIR, "20260729")
        with self.assertRaises(ValueError):
            cr.rebuild(self.t / "base", self.t / "master.csv", self.t / "signals", self.t / "o", "20260728")  # base has it

    def test_a_live_run_marks_a_back_dated_replay_provisional_and_blocks_the_cash(self):
        self.rebuild("rec", events="sec_none.csv", end="20260824")       # history as recorded without the merger
        security_master.SECURITY_EVENTS = self.t / "sec.csv"               # the merger becomes known on 25 Aug
        args = argparse.Namespace(master=str(self.t / "master.csv"), signal=str(self.t / "none.csv"),
                                  positions=str(self.t / "rec" / "positions.csv"),
                                  equity_log=str(self.t / "rec" / "equity_log.csv"),
                                  realized=str(self.t / "rec" / "realized_trades.csv"),
                                  corporate_actions=str(self.t / "none_ca.csv"), portfolio_inr=self.CAP, entry_day="mon",
                                  max_holding_days=0, max_open_positions=2, dry_run=True,
                                  fee_evidence=str(self.t / "ev.csv"), counterfactual=False)
        out = ptm.run(args)
        s = next(t for t in out["book"].trades if t["symbol"] == "S")
        self.assertEqual((s["exit_date"], s["reason"]), ("20260821", "TIME_STOP"))
        self.assertIn("back-dated by a replay: S TIME_STOP on 2026-08-21", s["provisional"])
        self.assertIn("S closed 2026-08-21: provisional exit (back-dated by a replay", out["eq_row"]["CASH_NOTE"])
        eq = pd.concat([pd.read_csv(self.t / "rec" / "equity_log.csv"), pd.DataFrame([out["eq_row"]])], ignore_index=True)
        v = lv.verify_cash(out["positions"], eq, out["realized"])
        self.assertFalse(v["verified"])
        self.assertIn("provisional exit: back-dated by a replay", v["check"])

    def test_counterfactual_is_labelled_and_never_written(self):
        args = argparse.Namespace(master=str(self.t / "master.csv"), signal=str(self.t / "none.csv"),
                                  positions=str(self.t / "base" / "positions.csv"),
                                  equity_log=str(self.t / "base" / "equity_log.csv"),
                                  realized=str(self.t / "base" / "realized_trades.csv"),
                                  corporate_actions=str(self.t / "none_ca.csv"), portfolio_inr=self.CAP, entry_day="mon",
                                  max_holding_days=0, max_open_positions=2, dry_run=False,
                                  fee_evidence=str(self.t / "ev.csv"), counterfactual=True)
        out = ptm.run(args)
        self.assertEqual((out["counterfactual"], out["rules"]), (True, ptm.TODAY_RULES))
        self.assertFalse([t for t in out["book"].trades if t["reason"] == "TIME_STOP"])   # today's rules: no time stop
        with self.assertRaises(RuntimeError):
            ptm.write_outputs(args, out)
        before = {f.name: f.read_bytes() for f in (self.t / "base").iterdir()}
        argv = ["paper_trade_manager.py", "--master", args.master, "--signal", args.signal, "--positions", args.positions,
                "--equity-log", args.equity_log, "--realized", args.realized, "--corporate-actions", args.corporate_actions,
                "--portfolio-inr", "10000", "--fee-evidence", args.fee_evidence, "--counterfactual-today-rules"]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            ptm.main()
        self.assertEqual(before, {f.name: f.read_bytes() for f in (self.t / "base").iterdir()})


class StaleCounter(Patched):
    """Sessions without a valid print are counted from the price data on every run, so a holding becomes stale
    after 3 of them even when each session is processed by a separate daily run."""

    def setUp(self):
        super().setUp()
        t = self.t
        self.days = days = list(pd.bdate_range("2026-09-07", periods=12).strftime("%Y%m%d"))
        rows = []
        for i, d in enumerate(days):
            rows.append({"DATE": d, "SYMBOL": "O", "SERIES": "EQ", "OPEN": 10, "HIGH": 11, "LOW": 9, "CLOSE": 10,
                         "TRADED_VALUE": 1e8})
            if i <= 4 or i >= 10:                                          # H prints to 11 Sep and from 21 Sep
                rows.append({"DATE": d, "SYMBOL": "H", "SERIES": "EQ", "OPEN": 100, "HIGH": 101, "LOW": 99,
                             "CLOSE": 100, "TRADED_VALUE": 1e8})
            elif i == 5:                                                   # 14 Sep: a print with no valid close
                rows.append({"DATE": d, "SYMBOL": "H", "SERIES": "EQ", "OPEN": 100, "HIGH": 101, "LOW": 99,
                             "CLOSE": 0, "TRADED_VALUE": 1e8})
        pd.DataFrame(rows).to_csv(t / "master.csv", index=False)
        sec_events([]).to_csv(t / "sec.csv", index=False)
        pd.DataFrame([{"symbol": "H", "signal_date": "20260904", "entry_date": "2026-09-07", "entry_price": 100.0,
                       "qty": 10, "size_inr": 1000.0, "initial_stop": 80.0, "current_stop": 80.0,
                       "highest_close": 100.0, "atr_at_entry": 1.0, "status": "open", "entry_cost_inr": 5.0}]
                     ).to_csv(t / "positions.csv", index=False)
        pd.DataFrame([{"DATE": "2026-09-04", "EQUITY": 10000.0, "CASH": 10000.0, "INVESTED": 0.0, "REALIZED_PNL": 0.0,
                       "N_OPEN": 0},
                      {"DATE": "2026-09-07", "EQUITY": 9995.0, "CASH": 8995.0, "INVESTED": 1000.0, "REALIZED_PNL": 0.0,
                       "N_OPEN": 1}]).to_csv(t / "equity_log.csv", index=False)
        self.args = argparse.Namespace(master=str(t / "master.csv"), signal=str(t / "none.csv"),
                                       positions=str(t / "positions.csv"), equity_log=str(t / "equity_log.csv"),
                                       realized=str(t / "realized.csv"), corporate_actions=str(t / "none_ca.csv"),
                                       portfolio_inr=10000.0, entry_day="mon", max_holding_days=0,
                                       max_open_positions=15, dry_run=False, fee_evidence=str(t / "none_ev.csv"),
                                       counterfactual=False)

    def daily_run(self, d):
        full = ptm._load_market(self.t / "master.csv", "20260801", self.t / "none_ca.csv", (22,))
        out = ptm.run(self.args, market=lambda _s: cr._cut(full, d))
        ptm.write_outputs(self.args, out)
        row = pd.read_csv(self.t / "positions.csv", dtype=str).iloc[0]
        return row["status"], row["stale_reason"] if isinstance(row["stale_reason"], str) else ""

    def test_a_holding_without_valid_prints_becomes_stale_across_separate_daily_runs(self):
        seen = {d: self.daily_run(d) for d in self.days[1:11]}             # one run per session, 8 Sep .. 21 Sep
        self.assertEqual([seen[d][0] for d in self.days[1:7]], ["open"] * 6)  # 14 Sep (no close) and 15 Sep: 2 missing
        self.assertEqual(seen["20260916"], ("stale", "no print since 20260911"))   # the 3rd session without one
        self.assertEqual([seen[d][0] for d in ("20260917", "20260918")], ["stale", "stale"])
        self.assertEqual(seen["20260921"][0], "open")                      # prints again
        notes = pd.read_csv(self.t / "positions.csv", dtype=str).iloc[0]["notes"]
        self.assertIn("20260921: prints again after 5 missing sessions", notes)

    def test_a_missing_supplement_file_stops_the_run_clearly_before_writing(self):
        security_master.SUPPLEMENT = self.t / "absent.csv"
        with self.assertRaises(ptm.SupplementMissing) as cm:
            ptm._load_market(self.t / "master.csv", "20260801", self.t / "none_ca.csv", (22,))
        self.assertIn("supplementary price file not found", str(cm.exception))
        self.assertIn("Nothing was written", str(cm.exception))
        before = {f.name: f.read_bytes() for f in self.t.iterdir() if f.is_file()}
        argv = ["paper_trade_manager.py", "--master", self.args.master, "--signal", self.args.signal,
                "--positions", self.args.positions, "--equity-log", self.args.equity_log, "--realized",
                self.args.realized, "--corporate-actions", self.args.corporate_actions, "--portfolio-inr", "10000"]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print") as pr:
            with self.assertRaises(SystemExit) as ex:
                ptm.main()
        self.assertEqual(ex.exception.code, 2)
        self.assertIn("[ERROR] supplementary price file not found", pr.call_args_list[-1].args[0])
        self.assertEqual(before, {f.name: f.read_bytes() for f in self.t.iterdir() if f.is_file()})
        with self.assertRaises(ptm.SupplementMissing):
            cr.rebuild(self.t, self.t / "master.csv", self.t, self.t / "out", "20260908",
                       corporate_actions=self.t / "none_ca.csv")
        self.assertFalse((self.t / "out").exists())

    def test_a_header_only_supplement_and_corporate_actions_load_without_error(self):
        pd.DataFrame([{"SYMBOL": "O", "EX_DATE": self.days[6], "KIND": "split", "FACTOR": 0.5,
                       "FACTOR_TYPE": "official_ratio", "SOURCE": "nse", "STATUS": "ok", "OBS_RATIO": "",
                       "SUBJECT": "test"}]).to_csv(self.t / "ca.csv", index=False)
        raw, atr, lows, tv, sessions = ptm._load_market(self.t / "master.csv", "20260801", self.t / "ca.csv", (22,))
        self.assertEqual(sessions[-1], self.days[-1])


class CorporateActionUpdate(Patched):
    def test_a_top_up_keeps_every_stored_digit_of_untouched_factors(self):
        import corporate_actions
        t = self.t
        d = (pd.Timestamp.today() - pd.Timedelta(days=5)).strftime("%Y%m%d")
        pd.DataFrame([{"DATE": d, "SYMBOL": "O", "SERIES": "EQ", "OPEN": 10, "HIGH": 11, "LOW": 9, "CLOSE": 10,
                       "VOLUME": 1, "TRADED_VALUE": 1e8}]).to_csv(t / "master.csv", index=False)
        sec_events([]).to_csv(t / "sec.csv", index=False)
        line = "NYKAA,20221110,bonus,0.16666666666666666,official_ratio,nse,validated,0.1575,Bonus 5:1"
        (t / "ca.csv").write_text("\n".join([",".join(corporate_actions.EVENT_COLS), line, ""]))
        argv = ["corporate_actions.py", "--update", "--no-fetch", "--master", str(t / "master.csv"),
                "--events", str(t / "ca.csv"), "--days", "30"]
        with mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            corporate_actions.main()
        self.assertIn(line, (t / "ca.csv").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
