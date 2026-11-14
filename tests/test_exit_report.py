"""The one exit-reporting function (exit_report.py) and the two scheduled alerts that use it:
daily_status_broadcaster.py (V5 Daily Data Refresh) and telegram_broadcaster.py --type daily_exit
(V5 Daily Exit Check). Synthetic ledgers; Telegram sending is stubbed - nothing is sent."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v5_modules"))

import exit_report as er  # noqa: E402

# Ledger runs: 30 Sep, 01 Oct, then 05 Oct - a catch-up run that processed the missed 02 Oct session too
EQUITY_LOG = pd.DataFrame({"DATE": ["2026-09-30", "2026-10-01", "2026-10-05"], "EQUITY": [200000.0, 199000.0, 198500.0],
                           "CASH": [100000.0, 101000.0, 105000.0], "MTM_OPEN": [0.0] * 3, "INVESTED": [0.0] * 3,
                           "REALIZED_PNL": [0.0] * 3, "N_OPEN": [3, 3, 2]})
REALIZED = pd.DataFrame([
    {"symbol": "ONGC", "entry_date": "2026-09-15", "entry_price": 250.0, "qty": 21, "exit_date": "2026-10-01",
     "exit_price": 242.8, "exit_reason": "STOP_LOSS", "days_held": 12, "pnl_inr": -151.2, "pnl_pct": -0.0288},
    {"symbol": "HINDALCO", "entry_date": "2026-09-21", "entry_price": 1039.2, "qty": 5, "exit_date": "2026-10-02",
     "exit_price": 970.0, "exit_reason": "STOP_GAP_AT_OPEN", "days_held": 9, "pnl_inr": -346.0, "pnl_pct": -0.0666},
    {"symbol": "TCS", "entry_date": "2026-09-28", "entry_price": 3060.0, "qty": 2, "exit_date": "2026-10-05",
     "exit_price": 3000.0, "exit_reason": "STOP_LOSS", "days_held": 5, "pnl_inr": -120.0, "pnl_pct": -0.0196},
    {"symbol": "AAA", "entry_date": "2026-10-01", "entry_price": 0.0, "qty": 10, "exit_date": "2026-10-05",
     "exit_price": "", "exit_reason": "CASH_DIVIDEND", "days_held": 0, "pnl_inr": 50.0, "pnl_pct": ""},
    {"symbol": "FFF", "entry_date": "2026-09-10", "entry_price": 450.0, "qty": 10, "exit_date": "2026-10-02",
     "exit_price": "", "exit_reason": "CASH_CONSIDERATION", "days_held": 20, "pnl_inr": 300.0, "pnl_pct": 0.0667},
])
POSITIONS = pd.DataFrame([
    {"symbol": "AAA", "status": "closed", "kind": "cash", "cash_due": 50.0, "exit_date": "2026-10-05",
     "exit_reason": "CASH_DIVIDEND", "notes": "20261001: pending dividend Rs50.00 gross"},
    {"symbol": "FFF", "status": "closed", "kind": "cash", "cash_due": 4800.0, "exit_date": "2026-10-02",
     "exit_reason": "CASH_CONSIDERATION", "notes": "20261002: cash Rs4,800.00 received"},
    {"symbol": "BBB", "status": "stale", "kind": "shares", "notes": "20260915: entry | 20261002: stale: no print since 20260930"},
    {"symbol": "CCC", "status": "pending", "kind": "shares", "ca_applied": "sec:CCC-d:locked",
     "notes": "20261005: locked: no trading after 20261003; shares awaiting tender on 20261020"},
    {"symbol": "DDD", "status": "stale", "kind": "shares", "notes": "20260920: stale: unverified corporate action"},
    {"symbol": "EEE", "status": "open", "kind": "shares",
     "notes": "20261002: stale: no print since 20260930 | 20261005: prints again after 3 missing sessions"},
])


class Window(unittest.TestCase):
    def test_catch_up_run_covers_every_session_since_the_previous_run(self):
        rep = er.collect(REALIZED, POSITIONS, EQUITY_LOG)
        self.assertEqual((rep["window"]["start"], rep["window"]["end"]), ("20261001", "20261005"))
        self.assertEqual(rep["trades"].symbol.tolist(), ["HINDALCO", "TCS"])   # 02 Oct (catch-up) and 05 Oct
        self.assertNotIn("ONGC", rep["trades"].symbol.tolist())                # reported with the 01 Oct run
        self.assertIn("catch-up: 2 weekdays", er.coverage(rep))

    def test_dates_come_from_the_ledger_not_the_clock(self):
        with mock.patch("pandas.Timestamp.now", return_value=pd.Timestamp("2027-01-01")):
            rep = er.collect(REALIZED, POSITIONS, EQUITY_LOG)
        self.assertEqual(rep["trades"].symbol.tolist(), ["HINDALCO", "TCS"])

    def test_several_unreported_runs(self):
        rep = er.collect(REALIZED, POSITIONS, EQUITY_LOG, marker="20260930")
        self.assertEqual(rep["trades"].symbol.tolist(), ["ONGC", "HINDALCO", "TCS"])
        self.assertIn("2 ledger runs not reported before", er.coverage(rep))

    def test_already_reported_run_is_not_repeated(self):
        rep = er.collect(REALIZED, POSITIONS, EQUITY_LOG, marker="20261005")
        self.assertTrue(rep["window"]["already_reported"])
        self.assertIsNone(er.build_message(rep))

    def test_no_ledger_yet(self):
        self.assertIsNone(er.collect(REALIZED, POSITIONS, None))


class Events(unittest.TestCase):
    def setUp(self):
        self.rep = er.collect(REALIZED, POSITIONS, EQUITY_LOG)
        self.msg = er.build_message(self.rep)

    def test_cash_settlements_are_not_trade_exits(self):
        self.assertEqual(self.rep["cash"].symbol.tolist(), ["FFF", "AAA"])
        self.assertEqual(self.rep["cash"].amount.tolist(), [4800.0, 50.0])     # amount credited, not P&L
        self.assertIn("*Trade exits (2)* — already booked in the paper ledger", self.msg)
        self.assertIn("_Trade exits P&L:_ ₹-466", self.msg)                     # dividend / consideration excluded
        self.assertIn("*Cash settled (2)* — not trade exits", self.msg)
        self.assertIn("• `FFF` — cash consideration ₹4,800.00 credited 02 Oct 2026", self.msg)
        self.assertIn("• `AAA` — dividend ₹50.00 credited 05 Oct 2026", self.msg)
        self.assertIn("❌ `HINDALCO` — STOP_GAP_AT_OPEN at ₹970.00 on 02 Oct 2026  (-6.66% / ₹-346 / 9d)", self.msg)
        self.assertNotIn("Exit at next open", self.msg)

    def test_newly_stale_or_locked_holdings_are_flagged(self):
        a = {(x["symbol"], x["what"], x["date"]) for x in self.rep["attention"]}
        self.assertEqual(a, {("BBB", "stale", "20261002"), ("CCC", "locked", "20261005")})
        # DDD went stale before this window; EEE was stale but prints again (open now)
        self.assertIn("• `BBB` became stale on 02 Oct 2026 — no print since 20260930", self.msg)
        self.assertIn("• `CCC` locked for an exit offer on 05 Oct 2026 — no trading after 20261003", self.msg)

    def test_old_format_ledger(self):
        old = POSITIONS[["symbol", "status"]].iloc[2:]                        # no kind / notes / cash_due columns
        rep = er.collect(REALIZED, old, EQUITY_LOG)
        self.assertEqual(rep["trades"].symbol.tolist(), ["HINDALCO", "TCS"])
        self.assertEqual(rep["attention"], [])
        self.assertIn("amount not in ledger rows", er.build_message(rep))


class ScheduledAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.pos, self.real, self.eq = d / "positions.csv", d / "realized_trades.csv", d / "equity_log.csv"
        POSITIONS.to_csv(self.pos, index=False); REALIZED.to_csv(self.real, index=False); EQUITY_LOG.to_csv(self.eq, index=False)
        self.marker = d / "last_exit_report.txt"
        self.missing = str(d / "missing.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, module, argv, ok=True):
        sent = []
        fake = lambda msg, **kw: (sent.append(msg), {"ok": ok})[1]
        with mock.patch.object(module, "send", fake), mock.patch.object(sys, "argv", ["x"] + argv):
            module.main()
        return sent

    def test_nightly_status_reports_then_records_the_run(self):
        import daily_status_broadcaster as ds
        argv = ["--positions", str(self.pos), "--realized", str(self.real), "--equity-log", str(self.eq),
                "--regime-json", self.missing]
        sent = self.run_main(ds, argv)
        self.assertEqual(len(sent), 1)
        for part in ("V5 PAPER EXITS", "HINDALCO", "TCS", "Cash settled (2)", "Needs attention (2)", "catch-up"):
            self.assertIn(part, sent[0])
        self.assertEqual(er.read_marker(self.marker), "20261005")
        sent = self.run_main(ds, argv)                                          # same run again: heartbeat only
        self.assertIn("No new ledger session since the last report", sent[0])
        self.assertNotIn("HINDALCO", sent[0])

    def test_failed_send_does_not_record_the_run(self):
        import daily_status_broadcaster as ds
        argv = ["--positions", str(self.pos), "--realized", str(self.real), "--equity-log", str(self.eq),
                "--regime-json", self.missing]
        self.run_main(ds, argv, ok=False)
        # not marked as reported: the marker records where reporting stopped (the previous run), so the
        # next nightly report still covers the 02 Oct and 05 Oct sessions
        self.assertEqual(er.read_marker(self.marker), "20261001")
        er.write_marker(self.marker, "20260930")
        self.run_main(ds, argv, ok=False)                                # an existing marker is left alone
        self.assertEqual(er.read_marker(self.marker), "20260930")

    def test_failed_send_before_any_report(self):
        one_run = EQUITY_LOG.iloc[[0]]                                    # the very first ledger run
        rep = er.collect(REALIZED, POSITIONS, one_run)
        er.record(self.marker, rep, sent=False)
        self.assertEqual(er.read_marker(self.marker), er.BEFORE_FIRST)
        later = er.collect(REALIZED, POSITIONS, EQUITY_LOG, er.read_marker(self.marker))
        self.assertEqual(later["trades"].symbol.tolist(), ["ONGC", "HINDALCO", "TCS"])   # nothing skipped
        self.assertIn("every ledger run up to 05 Oct 2026 - none reported before", er.coverage(later))

    def test_exit_check_never_sends_exit_details(self):
        import telegram_broadcaster as tb
        argv = ["--type", "daily_exit", "--positions", str(self.pos), "--equity-log", str(self.eq),
                "--regime-json", self.missing]                       # realized defaults to the positions folder
        er.write_marker(self.marker, "20261001")                      # the nightly status did not send the 05 Oct run
        sent = self.run_main(tb, argv)
        self.assertEqual(len(sent), 1)
        self.assertIn("V5 exit report not sent", sent[0])
        self.assertIn("Pending: 2 trade exit(s), 2 cash settlement(s), 2 holding(s) needing attention", sent[0])
        for sym in ("HINDALCO", "TCS", "AAA", "FFF", "BBB", "CCC"):
            self.assertNotIn(sym, sent[0])                            # counts only, never the exits themselves
        self.assertEqual(er.read_marker(self.marker), "20261001")     # it cannot persist one, so it writes none

    def test_exit_check_stays_quiet_before_the_first_nightly_marker(self):
        import telegram_broadcaster as tb
        self.assertEqual(self.run_main(tb, ["--type", "daily_exit", "--positions", str(self.pos),
                                            "--equity-log", str(self.eq)]), [])
        self.assertFalse(self.marker.exists())

    def test_exit_check_skips_what_the_nightly_status_sent(self):
        import telegram_broadcaster as tb
        er.write_marker(self.marker, "20261005")
        self.assertEqual(self.run_main(tb, ["--type", "daily_exit", "--positions", str(self.pos),
                                            "--equity-log", str(self.eq)]), [])

    def test_build_daily_exit_is_the_notice(self):
        import telegram_broadcaster as tb
        er.write_marker(self.marker, "20261001")
        self.assertEqual(tb.build_daily_exit(str(self.pos)),
                         er.missing_report_notice(er.collect_paths(self.real, self.pos, self.eq)))

    def test_dry_runs_send_and_record_nothing(self):
        import daily_exit_broadcaster as dx
        import daily_status_broadcaster as ds
        import telegram_broadcaster as tb
        never = mock.Mock(side_effect=AssertionError("must not send"))
        common = ["--positions", str(self.pos), "--realized", str(self.real), "--equity-log", str(self.eq)]
        for module, argv in ((ds, common + ["--regime-json", self.missing, "--dry-run"]),
                             (tb, ["--type", "daily_exit"] + common + ["--dry-run"])):
            with mock.patch.object(module, "send", never), mock.patch.object(sys, "argv", ["x"] + argv):
                module.main()
        self.assertFalse(hasattr(dx, "send"))                         # the manual tool cannot send at all
        for extra in ([], ["--dry-run"]):
            with mock.patch.object(sys, "argv", ["x"] + common + extra), mock.patch("builtins.print") as out:
                dx.main()
            self.assertIn("preview only", str(out.call_args_list[-1]))
        self.assertFalse(self.marker.exists())


class SeparateRunners(unittest.TestCase):
    """The two workflows run on separate GitHub runners: each starts from a fresh checkout of the repo, and
    only V5 Daily Data Refresh commits data/ back. Exit details must reach Telegram exactly once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.origin = Path(self.tmp.name) / "origin" / "data" / "live_signals"     # the repository's committed state
        self.origin.mkdir(parents=True)
        POSITIONS.to_csv(self.origin / "positions.csv", index=False)
        REALIZED.to_csv(self.origin / "realized_trades.csv", index=False)
        EQUITY_LOG.to_csv(self.origin / "equity_log.csv", index=False)
        self.sent, self.n = [], 0

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, workflow, ok=True, dry=False):
        """One workflow run on a fresh runner: check out, run, commit back only if the workflow commits."""
        import daily_status_broadcaster as ds
        import telegram_broadcaster as tb
        self.n += 1
        work = Path(self.tmp.name) / f"runner{self.n}" / "data" / "live_signals"
        shutil.copytree(self.origin, work)
        args = ["--positions", str(work / "positions.csv"), "--realized", str(work / "realized_trades.csv"),
                "--equity-log", str(work / "equity_log.csv"), "--regime-json", str(work / "none.json")]
        module = ds if workflow == "refresh" else tb
        argv = args if workflow == "refresh" else ["--type", "daily_exit"] + args
        fake = lambda msg, **kw: (self.sent.append((workflow, msg, ok)), {"ok": ok})[1]
        with mock.patch.object(module, "send", fake), \
                mock.patch.object(sys, "argv", ["x"] + argv + (["--dry-run"] if dry else [])):
            module.main()
        if workflow == "refresh":                                     # 'git add -A data/' + push
            shutil.rmtree(self.origin)
            shutil.copytree(work, self.origin)

    def ledger_run(self, date, exits):
        """The next nightly ledger run, as committed: a new equity-log row and its exits."""
        e = pd.read_csv(self.origin / "equity_log.csv")
        pd.concat([e, pd.DataFrame([e.iloc[-1].to_dict() | {"DATE": date}])]).to_csv(self.origin / "equity_log.csv", index=False)
        r = pd.read_csv(self.origin / "realized_trades.csv")
        pd.concat([r, pd.DataFrame(exits)]).to_csv(self.origin / "realized_trades.csv", index=False)

    def delivered(self, *symbols):
        """How many messages that actually reached Telegram carry each exit's details."""
        return {s: sum(1 for _, m, ok in self.sent if ok and f"`{s}`" in m) for s in symbols}

    def test_success_nightly_sends_once_and_exit_check_stays_quiet(self):
        self.runner("refresh")
        self.assertEqual(er.read_marker(self.origin / er.MARKER_NAME), "20261005")   # persisted by the commit
        self.runner("exit_check")
        self.assertEqual([w for w, _, _ in self.sent], ["refresh"])
        self.assertEqual(self.delivered("HINDALCO", "TCS"), {"HINDALCO": 1, "TCS": 1})

    def test_failed_send_is_reported_once_by_the_next_nightly(self):
        self.runner("refresh", ok=False)                              # Telegram refused the message
        # the committed marker records where reporting stopped (01 Oct), not the unsent 05 Oct run
        self.assertEqual(er.read_marker(self.origin / er.MARKER_NAME), "20261001")
        self.runner("exit_check")                                     # warns, without the exits
        self.assertIn("V5 exit report not sent", self.sent[-1][1])
        self.assertNotIn("HINDALCO", self.sent[-1][1])
        self.ledger_run("2026-10-06", [{"symbol": "INFY", "entry_date": "2026-09-28", "entry_price": 1500.0, "qty": 3,
                                        "exit_date": "2026-10-06", "exit_price": 1440.0, "exit_reason": "STOP_LOSS",
                                        "days_held": 6, "pnl_inr": -180.0, "pnl_pct": -0.04}])
        self.runner("refresh")                                        # next night: both runs, catch-up
        self.assertIn("2 ledger runs not reported before", self.sent[-1][1])
        self.runner("exit_check")                                     # reported now: nothing
        self.assertEqual(self.delivered("HINDALCO", "TCS", "INFY"), {"HINDALCO": 1, "TCS": 1, "INFY": 1})
        self.assertEqual(er.read_marker(self.origin / er.MARKER_NAME), "20261006")
        self.assertEqual([w for w, _, _ in self.sent], ["refresh", "exit_check", "refresh"])

    def test_dry_run_records_nothing_and_does_not_block_the_real_report(self):
        self.runner("refresh", dry=True)
        self.assertEqual((self.sent, (self.origin / er.MARKER_NAME).exists()), ([], False))
        self.runner("refresh")
        self.runner("exit_check")
        self.assertEqual(self.delivered("HINDALCO", "TCS"), {"HINDALCO": 1, "TCS": 1})


if __name__ == "__main__":
    unittest.main()
