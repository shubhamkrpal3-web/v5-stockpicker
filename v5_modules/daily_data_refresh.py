"""
daily_data_refresh.py
=====================
Nightly orchestrator. Called by cron at 21:30 IST.

Sequence:
  1. Download today's NSE bhavcopy (idempotent)
  2. Refresh stale Screener.in fundamentals (>7 days old)
  3. Generate today's V5 signals → writes daily_live_portfolio.csv
  4. Update positions ledger with today's close prices
  5. Run exit engine; write exit signals if any
  6. Log everything

The script is designed to fail loudly via Telegram if a critical step breaks,
so you find out via your phone the next morning.

Usage from cron:
    /home/ubuntu/v5env/bin/python /home/ubuntu/v5/daily_data_refresh.py \\
        --base-dir /home/ubuntu/v5 \\
        --creds /home/ubuntu/v5/secrets/telegram_creds.txt
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd


def run_step(name: str, cmd: list, log_file: Path) -> tuple[bool, str]:
    """Run a subprocess step. Returns (success, log_tail)."""
    print(f"\n=== STEP: {name} ===")
    start = time.time()
    try:
        with open(log_file, "a") as lf:
            lf.write(f"\n=== {datetime.utcnow().isoformat()} {name} ===\n")
            lf.write(f"CMD: {' '.join(cmd)}\n")
            lf.flush()
            r = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, timeout=1800)
        ok = (r.returncode == 0)
        # Pull last 30 lines of log for telegram error reporting
        with open(log_file) as lf:
            tail = "".join(lf.readlines()[-30:])
        elapsed = time.time() - start
        print(f"  done in {elapsed:.1f}s  status={'OK' if ok else 'FAIL'}")
        return ok, tail
    except Exception as e:
        return False, str(e) + "\n" + traceback.format_exc()


def telegram_alert(msg: str, creds_path: str | None):
    """Best-effort send. Don't crash if telegram fails."""
    try:
        from telegram_broadcaster import send
        send(msg, creds_path=creds_path)
    except Exception as e:
        print(f"[WARN] Telegram alert failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-dir", required=True,
                    help="Root path (e.g. /home/ubuntu/v5)")
    ap.add_argument("--creds", default=None)
    ap.add_argument("--portfolio-inr", type=float, default=200000.0)
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--skip-bhavcopy", action="store_true")
    args = ap.parse_args()

    base = Path(args.base_dir).resolve()
    sys.path.insert(0, str(base))
    sys.path.insert(0, str(base / "v5_modules"))

    log_dir = base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today_log = log_dir / f"refresh_{datetime.utcnow().strftime('%Y%m%d')}.log"

    master = base / "data" / "market_database" / "master_history.csv"
    fundamentals_db = base / "data" / "fundamentals" / "screener.sqlite"
    output_signals = base / "data" / "live_signals"

    py = sys.executable
    modules = base / "v5_modules"
    overall_ok = True
    summary = {"started_at": datetime.utcnow().isoformat()}

    # Step 1: bhavcopy download
    if not args.skip_bhavcopy:
        ok, tail = run_step(
            "bhavcopy_download",
            [py, str(modules / "bhavcopy_downloader.py"),
             "--master", str(master), "--max-back-days", "7"],
            today_log,
        )
        summary["bhavcopy"] = "OK" if ok else "FAIL"
        if not ok:
            overall_ok = False
            telegram_alert(f"⚠️ V5 nightly: bhavcopy download FAILED\n```\n{tail[-600:]}\n```", args.creds)

    # Step 2: refresh stale fundamentals (top-1000 by traded value)
    # We always run it; the scraper has its own TTL check
    if (base / "v5_modules" / "screener_scraper.py").exists() and (base / "symbols.txt").exists():
        ok, tail = run_step(
            "fundamentals_refresh",
            [py, str(modules / "screener_scraper.py"),
             "--bootstrap", str(base / "symbols.txt"),
             "--cache-dir", str(base / "data" / "fundamentals")],
            today_log,
        )
        summary["fundamentals"] = "OK" if ok else "FAIL"
        if not ok:
            overall_ok = False
            telegram_alert(f"⚠️ V5 nightly: fundamentals refresh FAILED\n```\n{tail[-600:]}\n```", args.creds)

    # Step 3: generate V5 signals
    ok, tail = run_step(
        "v5_signal_generator",
        [py, str(modules / "v5_signal_generator.py"),
         "--master", str(master),
         "--fundamentals", str(fundamentals_db),
         "--output-dir", str(output_signals),
         "--top-n", str(args.top_n),
         "--portfolio-inr", str(args.portfolio_inr)],
        today_log,
    )
    summary["signals"] = "OK" if ok else "FAIL"
    if not ok:
        overall_ok = False
        telegram_alert(f"❌ V5 nightly: signal generation FAILED\n```\n{tail[-600:]}\n```", args.creds)

    # Step 4: update positions with EOD close + run exit engine (placeholder)
    # The actual position management requires more state — to be wired in once
    # paper trading begins.

    summary["finished_at"] = datetime.utcnow().isoformat()
    summary["overall"] = "OK" if overall_ok else "FAIL"
    with open(log_dir / "refresh_summary_latest.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n=== OVERALL: {summary['overall']} ===")
    if not overall_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
