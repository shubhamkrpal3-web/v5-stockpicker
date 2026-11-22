"""Operational safeguards for the nightly run (2026-11 incident: six failed nightly refreshes left the price master,
the signal file and the ledger at 12 Nov):

* bhavcopy_downloader catches up every day after the master's last date instead of only the last 7 days;
* paper_trade_manager makes no entries from a stale, same-day or undated signal file;
* the Monday signal message shows the signal's close date (with a stale warning) and real YTD / drawdown;
* email_sender fails fast with a clear message when Gmail refuses the login.
No network: SMTP is replaced; files live in temporary folders."""
import argparse
import smtplib
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "v5_modules"))
import bhavcopy_downloader as bd  # noqa: E402
import email_sender  # noqa: E402
import paper_trade_manager as ptm  # noqa: E402
import telegram_broadcaster as tb  # noqa: E402
from portfolio_accounting import Book  # noqa: E402

D = lambda s: date.fromisoformat(s)  # noqa: E731


class CatchUpDates(unittest.TestCase):
    def test_gap_after_failed_runs_is_fetched_oldest_first(self):
        # master at Thu 12 Nov; next run Mon 23 Nov: the old 7-day window (17..23 Nov) would skip 13 and 16 Nov
        got = bd.dates_to_fetch(D("2026-11-23"), 7, D("2026-11-12"), 30)
        self.assertEqual(got, [D(x) for x in ("2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18",
                                              "2026-11-19", "2026-11-20", "2026-11-23")])

    def test_up_to_date_master_keeps_the_usual_window(self):
        got = bd.dates_to_fetch(D("2026-11-20"), 7, D("2026-11-19"), 30)
        self.assertEqual(got, [D(x) for x in ("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20")])
        self.assertEqual(bd.dates_to_fetch(D("2026-11-20"), 7, None, 30), got)

    def test_catch_up_is_bounded(self):
        got = bd.dates_to_fetch(D("2026-11-23"), 7, D("2026-09-01"), 10)
        self.assertEqual(got[0], D("2026-11-16"))                     # 10 calendar days back at most
        self.assertTrue(all(d.weekday() < 5 for d in got))

    def test_master_last_date(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "m.csv"
            self.assertIsNone(bd.master_last_date(p))
            pd.DataFrame({"SYMBOL": ["A", "B", "A"], "DATE": ["20261111", "20261112", "20261110"]}).to_csv(p, index=False)
            self.assertEqual(bd.master_last_date(p), D("2026-11-12"))


class StaleSignal(unittest.TestCase):
    SESSIONS = ["20261112", "20261113", "20261116", "20261117", "20261118", "20261119", "20261120", "20261123"]

    def sig(self, *dates):
        return pd.DataFrame({"DATE": list(dates), "SYMBOL": [f"S{i}" for i in range(len(dates))]})

    def test_refusal_reasons(self):
        r = ptm._signal_refusal
        self.assertEqual(r(self.sig(20261120), "20261123", self.SESSIONS, 3), "")             # Friday -> Monday
        self.assertEqual(r(self.sig("20261118"), "20261123", self.SESSIONS, 3), "")           # 3 sessions: allowed
        self.assertIn("4 sessions old", r(self.sig(20261117), "20261123", self.SESSIONS, 3))
        self.assertIn("7 sessions old", r(self.sig(20261112), "20261123", self.SESSIONS, 3))
        self.assertIn("not before the entry session", r(self.sig(20261123), "20261123", self.SESSIONS, 3))
        self.assertIn("no single DATE", r(self.sig(20261120, 20261119), "20261123", self.SESSIONS, 3))
        self.assertIn("no single DATE", r(pd.DataFrame({"SYMBOL": ["A"]}), "20261123", self.SESSIONS, 3))

    def enter(self, signal_date):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "signal.csv"
            pd.DataFrame([{"DATE": signal_date, "SYMBOL": "NEW", "CLOSE": 20.0, "SIZE_INR": 400.0, "STOP_PRICE": 18.0,
                           "CONFIDENCE": 7.0}]).to_csv(p, index=False)
            bar = lambda s, d: {"open": 21.0, "high": 21.0, "low": 21.0, "close": 21.0}  # noqa: E731
            book = Book(100000.0, bar, lambda s, d: 1.0, lambda n, side, s, d: 0.0)
            args = argparse.Namespace(entry_day="mon", signal=str(p), max_open_positions=15, max_signal_age_sessions=3)
            ptm._enter(book, args, "20261123", bar, self.SESSIONS)
            return [(h.symbol, h.qty) for h in book.holdings]

    def test_fresh_signal_is_entered_stale_one_is_not(self):
        self.assertEqual(self.enter(20261120), [("NEW", 20.0)])          # 400 / 20 from the signal close
        self.assertEqual(self.enter(20261112), [])


class WeeklySignalMessage(unittest.TestCase):
    def build(self, signal_date, now):
        with tempfile.TemporaryDirectory() as t:
            sig, eq = Path(t) / "sig.csv", Path(t) / "eq.csv"
            pd.DataFrame([{"DATE": signal_date, "SYMBOL": "AAA", "CLOSE": 100.0, "STOP_PRICE": 95.0, "SIZE_INR": 5000.0,
                           "CONFIDENCE": 6.0, "ACTION": "ENTER"}]).to_csv(sig, index=False)
            pd.DataFrame({"DATE": ["2026-06-15", "2026-10-01", "2026-11-12"],
                          "EQUITY": [200000.0, 210000.0, 203600.0]}).to_csv(eq, index=False)
            return tb.build_weekly_signal(str(sig), equity_log_path=str(eq), now=pd.Timestamp(now))

    def test_stale_signal_is_dated_and_flagged(self):
        msg = self.build(20261112, "2026-11-23 09:30")
        self.assertIn("_Signal from the 12 Nov 2026 close_", msg)
        self.assertIn("11 days old", msg)
        self.assertIn("_Portfolio YTD:_ +1.80%   _DD from peak:_ -3.05%", msg)     # 203600/200000, 203600/210000
        self.assertNotIn("Kill switch", msg)

    def test_fresh_signal_has_no_warning(self):
        msg = self.build(20261120, "2026-11-23 09:30")
        self.assertIn("_Signal from the 20 Nov 2026 close_", msg)
        self.assertNotIn("days old", msg)

    def test_missing_equity_log_shows_na(self):
        with tempfile.TemporaryDirectory() as t:
            sig = Path(t) / "sig.csv"
            pd.DataFrame([{"DATE": 20261120, "SYMBOL": "AAA", "CLOSE": 100.0}]).to_csv(sig, index=False)
            msg = tb.build_weekly_signal(str(sig), equity_log_path=str(Path(t) / "none.csv"),
                                         now=pd.Timestamp("2026-11-23"))
        self.assertIn("_Portfolio YTD:_ n/a   _DD from peak:_ n/a", msg)


class EmailLogin(unittest.TestCase):
    ENV = {"EMAIL_FROM": "from@example.com", "EMAIL_TO": "to@example.com", "GMAIL_APP_PASSWORD": "test-only"}

    def smtp(self, login_error=None):
        server = mock.MagicMock()
        server.__enter__.return_value = server
        if login_error:
            server.login.side_effect = login_error
        return mock.patch.object(email_sender.smtplib, "SMTP_SSL", return_value=server), server

    def test_refused_login_exits_with_a_hint_and_uses_a_timeout(self):
        patch, server = self.smtp(smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
        with mock.patch.dict("os.environ", self.ENV), patch as ssl_cls:
            with self.assertRaises(SystemExit) as cm:
                email_sender.send_email("subject", "body")
        self.assertIn("GMAIL_APP_PASSWORD", str(cm.exception.code))
        self.assertEqual(ssl_cls.call_args.kwargs.get("timeout"), 60)
        server.send_message.assert_not_called()

    def test_successful_send(self):
        patch, server = self.smtp()
        with mock.patch.dict("os.environ", self.ENV), patch:
            email_sender.send_email("subject", "body")
        server.login.assert_called_once_with("from@example.com", "test-only")
        server.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
