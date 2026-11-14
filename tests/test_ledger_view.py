"""Risk view of the paper ledger (ledger_view.py, research_system/portfolio.py, risk_engine.py) and the
holdings displays: every active state, defensible values, verified cash, and fail-closed approvals.
Synthetic ledgers only; TEST VALUES ONLY."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))

import ledger_view as lv  # noqa: E402
from research_system.portfolio import load_v5  # noqa: E402
from research_system.risk_engine import RiskEngine  # noqa: E402
from tests.helpers import FRESH, broad, cand, test_config  # noqa: E402

NEW_COLS = ["symbol", "signal_date", "entry_date", "entry_price", "qty", "size_inr", "initial_stop", "current_stop",
            "highest_close", "atr_at_entry", "status", "exit_date", "exit_price", "exit_reason", "realized_pnl_inr",
            "realized_pct", "entry_cost_inr", "exit_cost_inr", "ca_applied", "hid", "kind", "tradable_from", "origin",
            "stale_reason", "last_mark", "last_mark_date", "cost_basis", "cash_due", "cash_date", "notes"]
OLD_COLS = NEW_COLS[:18]


def row(**kw):
    base = {"entry_date": "2026-09-01", "entry_price": 100.0, "current_stop": 90.0, "kind": "shares", "origin": "entry"}
    return base | kw


# every active state, plus closed / converted rows that must be ignored; cost_basis lets cash be verified
NEW_ROWS = [
    row(symbol="AAA", status="open", qty=10, last_mark=100.0, last_mark_date="20261005", hid="H1", cost_basis=1000.0),
    row(symbol="BBB", status="stale", qty=5, last_mark=200.0, last_mark_date="20261001", hid="H2", cost_basis=1000.0,
        stale_reason="no print for 3 sessions"),
    row(symbol="CCC", status="pending", qty=8, last_mark=50.0, last_mark_date="20260930", hid="H3", cost_basis=400.0,
        ca_applied="sec:CCC-d:locked"),
    row(symbol="DDD", status="pending", qty=4, last_mark=30.0, last_mark_date="20261003", hid="M4", cost_basis=120.0,
        origin="merger:XXX-m", tradable_from="20261020"),
    row(symbol="EEE", status="pending", qty=6, entry_price=0.0, hid="D5", cost_basis=0.0, origin="demerger:YYY-d",
        tradable_from="20261025"),
    row(symbol="AAA", status="pending", qty=10, kind="cash", cash_due=50.0, cash_date="20261020", hid="V6",
        cost_basis=0.0, origin="dividend:AAA-20261001-div1"),
    row(symbol="FFF", status="pending", qty=10, kind="cash", cash_due=4800.0, cash_date="20261030", hid="H7",
        cost_basis=4500.0, ca_applied="sec:FFF-d:locked;sec:FFF-d"),
    row(symbol="DDD", status="pending", qty=0.5, kind="fraction", hid="F8", cost_basis=15.0, origin="fraction:XXX-m"),
    row(symbol="HHH", status="pending", qty=3, kind="cash", cash_due=12.0, cash_date="20261015", hid="V9",
        cost_basis=0.0, origin="dividend:HHH-20261002-div2"),
    row(symbol="GGG", status="closed", qty=7, last_mark=10.0, last_mark_date="20260920", hid="H10",
        realized_pnl_inr=100.0, exit_date="2026-09-20"),
    row(symbol="XXX", status="converted", qty=2, last_mark=60.0, last_mark_date="20260925", hid="H11", cost_basis=0.0),
]
SHARES = 10 * 100 + 5 * 200 + 8 * 50 + 4 * 30                       # open + stale + locked + priced entitlement
OWED = 50 + 4800 + 12
CASH = 10_000.0
COST = 1000 + 1000 + 400 + 120 + 0 + 0 + 4500 + 15 + 0             # cost_basis of every active row
REALISED = 100.0
CAPITAL = CASH + COST - REALISED                                    # 16,935: cash = capital - cost + realised


def full_ledger():
    """Every state, including two with no defensible value: EEE (demerged, no price) and the unpriced fraction."""
    return pd.DataFrame(NEW_ROWS).reindex(columns=NEW_COLS)


def clean_ledger():
    """Every state with a defensible value: no unpriced entitlement; the fraction has its successor's price."""
    df = full_ledger()
    df = df[df.hid != "D5"].copy()
    df.loc[df.hid == "F8", ["last_mark", "last_mark_date"]] = [30.0, "20261003"]
    return df


def new_equity_log(equity=CASH + SHARES + OWED, cash=CASH, first=None):
    pre = {"DATE": "2026-06-08", "EQUITY": CAPITAL, "CASH": CAPITAL, "MTM_OPEN": 0.0, "INVESTED": 0.0,
           "REALIZED_PNL": 0.0, "N_OPEN": 0, "PENDING_REF": 0.0, "STALE_MARK": 0.0, "N_PENDING": 0, "N_STALE": 0}
    last = {"DATE": "2026-10-05", "EQUITY": equity, "CASH": cash, "MTM_OPEN": 1_000.0, "INVESTED": 1_000.0,
            "REALIZED_PNL": REALISED, "N_OPEN": 1, "PENDING_REF": 520.0 + OWED, "STALE_MARK": 1_000.0, "N_PENDING": 6,
            "N_STALE": 1}
    return pd.DataFrame([pre | (first or {}), last])


def old_ledger(qqq_entry_cost=0.5, rrr_realised=-30.0):
    return pd.DataFrame([
        {"symbol": "PPP", "status": "open", "qty": 10, "entry_price": 100.0, "size_inr": 1000.0, "entry_cost_inr": 1.5,
         "current_stop": 90.0, "entry_date": "2026-09-01"},
        {"symbol": "QQQ", "status": "open", "qty": 5, "entry_price": 40.0, "size_inr": 200.0,
         "entry_cost_inr": qqq_entry_cost, "current_stop": 35.0, "entry_date": "2026-09-08"},
        {"symbol": "RRR", "status": "closed", "qty": 3, "entry_price": 70.0, "size_inr": 210.0, "entry_cost_inr": 0.4,
         "realized_pnl_inr": rrr_realised, "current_stop": 60.0, "entry_date": "2026-08-01", "exit_date": "2026-08-20"},
    ]).reindex(columns=OLD_COLS)


OLD_CASH = 50_000.0 - (1000 + 1.5) - (200 + 0.5) - 30.0              # 48,768.00


def old_equity_log(cash=OLD_CASH, n_open=2, first=None):
    pre = {"DATE": "2026-06-08", "EQUITY": 50_000.0, "CASH": 50_000.0, "MTM_OPEN": 0.0, "INVESTED": 0.0,
           "REALIZED_PNL": 0.0, "N_OPEN": 0}
    last = {"DATE": "2026-10-05", "EQUITY": cash + 1_250.0, "CASH": cash, "MTM_OPEN": 1_250.0, "INVESTED": 1_200.0,
            "REALIZED_PNL": -30.0, "N_OPEN": n_open}
    return pd.DataFrame([pre | (first or {}), last])


OLD_PRICES = {"PPP": 105.0, "QQQ": 40.0}


def write(tmp, pos, eq):
    p, e = Path(tmp) / "positions.csv", Path(tmp) / "equity_log.csv"
    if pos is not None:
        pos.to_csv(p, index=False)
    if eq is not None:
        eq.to_csv(e, index=False)
    return p, e


class Classification(unittest.TestCase):
    def test_every_active_row_in_exactly_one_bucket(self):
        rows = lv.classify(full_ledger())
        self.assertEqual(len(rows), 9)                                   # closed and converted ignored
        self.assertEqual(rows.state.tolist(), [lv.OPEN, lv.STALE, lv.LOCKED, lv.PENDING, lv.UNPRICED, lv.DIV_OWED,
                                               lv.CONSIDERATION_OWED, lv.FRACTION, lv.DIV_OWED])
        for r in rows.itertuples():
            self.assertLessEqual(int(r.exposure > 0) + int(r.owed > 0), 1, r.symbol)   # never both shares and cash
        self.assertAlmostEqual(rows.exposure.sum(), SHARES)
        self.assertAlmostEqual(rows.owed.sum(), OWED)
        self.assertEqual(rows.loc[rows.state == lv.UNPRICED, "exposure"].iloc[0], 0.0)
        self.assertEqual(rows.loc[rows.state == lv.FRACTION, ["exposure", "owed"]].sum().sum(), 0.0)

    def test_untradeable_shares_never_take_a_fresh_price(self):
        rows = lv.classify(full_ledger(), {"AAA": 110.0, "BBB": 999.0, "CCC": 999.0, "DDD": 999.0, "EEE": 999.0})
        r = rows.set_index("hid")
        self.assertEqual(r.loc["H1", "mark"], 110.0)                       # open: latest close
        for hid, mark in (("H2", 200.0), ("H3", 50.0), ("M4", 30.0)):    # stale / locked / pending: last valid mark
            self.assertEqual(r.loc[hid, "mark"], mark)
        self.assertTrue(pd.isna(r.loc["D5", "mark"]))                     # unpriced stays unpriced

    def test_summary_separates_the_quantities(self):
        s = lv.summary(clean_ledger(), new_equity_log())
        self.assertEqual(s["format"], "new")
        self.assertAlmostEqual(s["share_exposure"], SHARES)
        self.assertAlmostEqual(s["owed"], OWED)
        self.assertEqual(s["owed_by_state"], {lv.CONSIDERATION_OWED: 4800.0, lv.DIV_OWED: 62.0})
        self.assertEqual((s["available_cash"], s["cash_verified"]), (CASH, True))
        self.assertTrue(s["cash_check"].startswith("verified: logged cash Rs10,000.00 = starting capital Rs16,935.00"))
        self.assertAlmostEqual(s["marked_equity"], CASH + SHARES + OWED)
        self.assertAlmostEqual(s["marked_equity"], s["ledger_equity"])
        self.assertEqual(s["slots"], 5)                                   # not the fraction or the two dividends owed
        self.assertEqual(s["held_symbols"], ["AAA", "BBB", "CCC", "DDD", "FFF"])   # HHH: only a dividend owed
        self.assertEqual(s["undefended"], [])

    def test_holdings_without_a_defensible_value_are_listed(self):
        s = lv.summary(full_ledger(), new_equity_log())
        self.assertEqual(s["undefended"], [
            "EEE: pending entitlement with no price yet (demerger:YYY-d; tradable from 20261025)",
            "DDD: fractional entitlement with no price and no documented settlement"])
        self.assertTrue(s["cash_verified"])                               # cash is fine; the valuation is not
        self.assertIsNone(s["marked_equity"])
        pos = clean_ledger()
        pos.loc[pos.hid == "H2", ["last_mark", "last_mark_date"]] = None  # stale shares lost their mark
        pos.loc[pos.hid == "H7", "cash_due"] = None                        # cash owed with no amount
        self.assertEqual(lv.summary(pos, new_equity_log())["undefended"], [
            "BBB: stale (untradeable) shares with no valid mark",
            "FFF: cash consideration owed with no amount recorded"])
        rows = lv.classify(old_ledger(), {"PPP": 105.0})                  # QQQ has no close and no stored mark
        self.assertEqual(lv.undefended(rows),
                         ["QQQ: open holding with no latest close and no stored mark (an entry price is not a valuation)"])


class CashVerification(unittest.TestCase):
    def test_new_format_verified_from_ledger_records(self):
        v = lv.verify_cash(clean_ledger(), new_equity_log())
        self.assertEqual((v["verified"], v["cash"], v["as_of"]), (True, CASH, "2026-10-05"))

    def test_new_format_files_from_different_runs(self):
        s = lv.summary(clean_ledger(), new_equity_log(equity=CASH + SHARES + OWED + 500))
        self.assertFalse(s["cash_verified"])
        self.assertIn("not from the same run", s["cash_check"])

    def test_old_format_verifiable(self):
        v = lv.verify_cash(old_ledger(), old_equity_log())
        self.assertTrue(v["verified"])
        self.assertEqual(v["cash"], OLD_CASH)
        self.assertEqual(v["check"], "verified: logged cash Rs48,768.00 = starting capital Rs50,000.00 (2026-06-08) "
                                     "- cost of 2 active holdings Rs1,202.00 + realised P&L of 1 closed rows Rs-30.00; "
                                     "cash history: the logged cash matches the recorded movements on all 2 dates")

    def test_old_format_missing_entry_cost_is_unverifiable(self):
        v = lv.verify_cash(old_ledger(qqq_entry_cost=None), old_equity_log(cash=OLD_CASH + 0.5))
        self.assertFalse(v["verified"])
        self.assertIsNone(v["cash"])
        self.assertTrue(v["check"].startswith("unverifiable: QQQ entered 2026-09-08 (no entry cost recorded)"))
        self.assertIn("zero would give Rs48,768.50, which equals the logged cash", v["check"])
        self.assertIn("the records do not say whether zero is correct", v["check"])

    def test_old_format_without_a_pre_trading_row_is_unverifiable(self):
        v = lv.verify_cash(old_ledger(), old_equity_log(first={"N_OPEN": 1, "INVESTED": 500.0}))
        self.assertFalse(v["verified"])
        self.assertIn("is not a pre-trading row, so the starting capital is not recorded", v["check"])

    def test_old_format_cash_mismatch(self):
        v = lv.verify_cash(old_ledger(), old_equity_log(cash=49_000.0))
        self.assertFalse(v["verified"])
        self.assertIn("mismatch: logged cash Rs49,000.00 != starting capital Rs50,000.00", v["check"])

    def test_closed_row_without_realised_pnl_is_unverifiable(self):
        v = lv.verify_cash(old_ledger(rrr_realised=None), old_equity_log())
        self.assertFalse(v["verified"])
        self.assertIn("RRR closed 2026-08-20 (no realised P&L recorded)", v["check"])

    def test_no_equity_log_is_unverifiable(self):
        v = lv.verify_cash(old_ledger(), None)
        self.assertEqual((v["verified"], v["check"]),
                         (False, "unverifiable: no equity log, so neither the starting capital nor the cash is recorded"))


class RiskView(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, pos, eq, prices=None):
        p, e = write(self.tmp.name, pos, eq)
        return load_v5(1_000_000, p, e, prices)

    def first_fail(self, c):
        return next(r for r in c.risk_checks if r["result"] == "FAIL")

    def test_loader_sees_every_active_state(self):
        b = self.book(clean_ledger(), new_equity_log())
        self.assertEqual(sorted((p.symbol, p.state) for p in b.positions),
                         [("AAA", "open"), ("BBB", "stale"), ("CCC", "locked"), ("DDD", "pending entitlement")])
        self.assertAlmostEqual(b.gross(), SHARES)
        self.assertAlmostEqual(b.owed(), OWED)
        self.assertEqual(b.count(), 5)
        for s in ("AAA", "BBB", "CCC", "DDD", "FFF"):
            self.assertTrue(b.holds(s), s)
        self.assertFalse(b.holds("HHH"))                                  # only a dividend owed: may be bought again
        self.assertEqual((b.available_cash, b.problems), (CASH, []))

    def test_old_format_loader_verified(self):
        b = self.book(old_ledger(), old_equity_log(), OLD_PRICES)
        self.assertEqual([(p.symbol, p.state, p.last_price) for p in b.positions],
                         [("PPP", "open", 105.0), ("QQQ", "open", 40.0)])
        self.assertEqual((b.count(), b.owed(), b.available_cash, b.problems), (2, 0.0, OLD_CASH, []))

    def test_engine_counts_all_states_and_caps_by_verified_cash(self):
        c = RiskEngine(test_config(), self.book(clean_ledger(), new_equity_log()), broad(), FRESH).evaluate(
            cand("ZZZ", unit_risk=1.0, price=100.0))
        self.assertEqual(c.status, "approved")
        self.assertEqual(c.sizing.quantity, 100)                           # Rs10,000 of verified cash / Rs100
        self.assertIn("capped by available_cash", "; ".join(c.sizing.notes))
        view = next(r for r in c.risk_checks if r["check"] == "book_view")
        self.assertEqual(view["result"], "PASS")
        for word in ("1 open", "1 stale", "1 locked", "1 pending entitlement", "owed Rs4,862", "slots 5",
                     "verified against ledger records"):
            self.assertIn(word, view["detail"])

    def test_free_capital_excludes_cash_owed(self):
        cfg = test_config(account__capital_inr=10_000, limits__max_position_pct=1.0)
        b = self.book(clean_ledger(), new_equity_log())
        b.available_cash = None                                            # isolate the configured-capital check
        c = RiskEngine(cfg, b, broad(), FRESH).evaluate(cand("ZZZ", unit_risk=0.01, price=20.0))
        self.assertEqual(c.sizing.quantity, (10_000 - SHARES - OWED) // 20)  # 2,618 free -> 130 shares

    def test_later_approvals_use_up_ledger_cash(self):
        eng = RiskEngine(test_config(limits__per_trade_max_loss_inr=600), self.book(clean_ledger(), new_equity_log()),
                         broad(), FRESH)
        out = eng.evaluate_all([cand("ZZ1", unit_risk=10.0, price=100.0), cand("ZZ2", unit_risk=10.0, price=100.0, sector="IT")])
        self.assertEqual([c.sizing.quantity for c in out], [60, 40])

    def test_duplicates_and_slots_follow_the_ledger(self):
        eng = RiskEngine(test_config(), self.book(clean_ledger(), new_equity_log()), broad(), FRESH)
        for sym in ("BBB", "CCC", "FFF"):                                 # stale, locked, consideration owed
            c = eng.evaluate(cand(sym, unit_risk=1.0, price=100.0))
            self.assertEqual((c.status, self.first_fail(c)["check"]), ("rejected", "duplicate"), sym)
        self.assertEqual(eng.evaluate(cand("HHH", unit_risk=1.0, price=100.0)).status, "approved")
        full = RiskEngine(test_config(limits__max_open_positions=5), self.book(clean_ledger(), new_equity_log()), broad(), FRESH)
        self.assertEqual(self.first_fail(full.evaluate(cand("ZZZ", unit_risk=1.0, price=100.0)))["check"], "position_count")

    # ---------- fail-closed ----------
    def assert_blocked(self, book, *phrases):
        c = RiskEngine(test_config(), book, broad(), FRESH).evaluate(cand("ZZZ"))
        fail = self.first_fail(c)
        self.assertEqual((c.status, fail["check"]), ("rejected", "book_view"))
        for ph in phrases:
            self.assertIn(ph, fail["detail"])
        return fail["detail"]

    def test_unpriced_entitlement_blocks_approvals(self):
        self.assert_blocked(self.book(full_ledger(), new_equity_log()), "no defensible value",
                            "EEE: pending entitlement with no price yet (demerger:YYY-d; tradable from 20261025)",
                            "DDD: fractional entitlement with no price")

    def test_shares_without_a_mark_block_approvals(self):
        pos = clean_ledger()
        pos.loc[pos.hid == "H2", ["last_mark", "last_mark_date"]] = None
        self.assert_blocked(self.book(pos, new_equity_log()), "BBB: stale (untradeable) shares with no valid mark")

    def test_open_holding_without_a_close_blocks_approvals(self):
        self.assert_blocked(self.book(old_ledger(), old_equity_log(), {"PPP": 105.0}),
                            "QQQ: open holding with no latest close and no stored mark")

    def test_unverifiable_or_mismatched_cash_blocks_approvals(self):
        self.assert_blocked(self.book(old_ledger(qqq_entry_cost=None), old_equity_log(cash=OLD_CASH + 0.5), OLD_PRICES),
                            "ledger cash: unverifiable: QQQ entered 2026-09-08 (no entry cost recorded)")
        self.assert_blocked(self.book(old_ledger(), old_equity_log(cash=49_000.0), OLD_PRICES), "ledger cash: mismatch")
        self.assert_blocked(self.book(clean_ledger(), new_equity_log(equity=1.0)), "not from the same run")

    def test_missing_ledger_blocks_approvals(self):
        self.assert_blocked(self.book(old_ledger(), None, OLD_PRICES), "ledger cash: unverifiable: no equity log")
        with tempfile.TemporaryDirectory() as empty:                      # neither file exists
            b = load_v5(1_000_000, Path(empty) / "positions.csv", Path(empty) / "equity_log.csv")
            self.assert_blocked(b, "no paper ledger found")

    def test_ledger_before_the_first_trade(self):
        pre = pd.DataFrame([{"DATE": "2026-06-08", "EQUITY": 200_000.0, "CASH": 200_000.0, "MTM_OPEN": 0.0,
                             "INVESTED": 0.0, "REALIZED_PNL": 0.0, "N_OPEN": 0}])
        b = self.book(pd.DataFrame(columns=NEW_COLS), pre)
        self.assertEqual((b.count(), b.problems, b.available_cash), (0, [], 200_000.0))


class Displays(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pos, self.eq = write(self.tmp.name, full_ledger(), new_equity_log())

    def tearDown(self):
        self.tmp.cleanup()

    def assert_all_states(self, text):
        for label in ("stale", "locked (exit offer)", "pending entitlement", "no price yet", "dividend owed",
                      "cash consideration owed", "fraction"):
            self.assertIn(label, text)
        for sym in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "HHH"):
            self.assertIn(sym, text)
        self.assertNotIn("GGG", text)                                     # closed

    def test_weekly_signal_hold_list(self):
        import telegram_broadcaster as tb
        sig = Path(self.tmp.name) / "signal.csv"
        pd.DataFrame(columns=["SYMBOL", "CLOSE", "STOP_PRICE", "SIZE_INR", "CONFIDENCE"]).to_csv(sig, index=False)
        text = tb.build_weekly_signal(str(sig), positions_path=str(self.pos))
        self.assertIn("HOLD (9 active)", text)
        self.assert_all_states(text)

    def test_email_lists_other_active_holdings(self):
        import email_body_builder as eb
        missing = Path(self.tmp.name) / "missing.csv"
        text = eb.build_body(self.eq, self.pos, missing, missing, missing)
        self.assertIn("OPEN POSITIONS (1)", text)
        self.assertIn("OTHER ACTIVE HOLDINGS (8)", text)
        self.assert_all_states(text)

    def test_excel_sheets_list_other_active_holdings(self):
        import excel_report_generator as xr
        wb = Workbook()
        xr._build_dashboard(wb.active, pd.read_csv(self.pos), pd.read_csv(self.eq), "RISK_ON", pd.Timestamp("2026-10-05"))
        ws = wb.create_sheet("Open Positions")
        xr._build_open_positions(ws, pd.read_csv(self.pos), pd.DataFrame(columns=["CLOSE"]))
        text = " ".join(str(c.value) for sheet in wb.worksheets for r in sheet.iter_rows() for c in r if c.value is not None)
        self.assertIn("Other active holdings", text)
        self.assertIn("Other Active Holdings", text)
        self.assert_all_states(text)

    def run_script(self, script, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        out = subprocess.run([sys.executable, "-B", str(ROOT / "v5_modules" / script), *args, "--dry-run"],
                             capture_output=True, text=True, env=env, encoding="utf-8", timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        return out.stdout

    def test_daily_status_and_weekly_summary_messages(self):
        missing = str(Path(self.tmp.name) / "missing")
        for script, extra in (("daily_status_broadcaster.py", ["--regime-json", missing]),
                              ("weekly_pnl_broadcaster.py", ["--regime-json", missing, "--benchmark-json", missing])):
            text = self.run_script(script, "--positions", str(self.pos), "--equity-log", str(self.eq),
                                   "--realized", missing, *extra)
            self.assertIn("Also active (not open)", text, script)
            self.assert_all_states(text)
        self.assertFalse((Path(self.tmp.name) / "last_exit_report.txt").exists())   # dry run records nothing


if __name__ == "__main__":
    unittest.main()
