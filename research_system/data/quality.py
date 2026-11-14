"""Data freshness and quality. Every engine and the risk engine read this.

A dataset is FRESH when its latest observation is the latest completed
trading session (or newer); STALE when it lags by N sessions; MISSING when
absent. Thresholds per dataset live in config ([data] section).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pandas as pd

from research_system import paths
from research_system.data import calendar


def _last_date_parquet(path, col="DATE") -> str | None:
    if not path.exists():
        return None
    try:
        return str(pd.read_parquet(path, columns=[col])[col].max())
    except Exception:
        return None


def _last_date_master() -> str | None:
    p = paths.MASTER_CSV if paths.MASTER_CSV.exists() else paths.MASTER
    if not p.exists():
        return None
    s = pd.read_csv(p, usecols=["DATE"])["DATE"].astype(str)
    return s.max()


def expected_session(today: date | None = None, after_close: bool = True) -> date:
    """The latest session whose EOD data should exist by now."""
    today = today or datetime.now(calendar.IST).date()
    if after_close and calendar.is_trading_day(today) and calendar.phase() == "post_close":
        return today
    return calendar.previous_trading_day(today)


def check(today: date | None = None) -> dict:
    exp = expected_session(today)
    items = {
        "equity_eod": _last_date_master(),
        "indices": _last_date_parquet(paths.INDICES),
        "macro": _last_date_parquet(paths.MACRO),
        "fno_summary": _last_date_parquet(paths.FNO_DIR / "summary.parquet"),
        "participant_oi": _last_date_parquet(paths.PARTICIPANT_OI),
        "fii_dii_cash": (pd.read_csv(paths.FII_DII_CASH, dtype={"DATE": str})["DATE"].max()
                         if paths.FII_DII_CASH.exists() else None),
        "corporate_actions": (datetime.fromtimestamp(paths.CORPORATE_ACTIONS.stat().st_mtime).strftime("%Y%m%d")
                              if paths.CORPORATE_ACTIONS.exists() else None),
        "fundamentals": (datetime.fromtimestamp(paths.FUNDAMENTALS_DB.stat().st_mtime).strftime("%Y%m%d")
                         if paths.FUNDAMENTALS_DB.exists() else None),
    }
    out = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "expected_session": exp.strftime("%Y%m%d"), "datasets": {}}
    for k, last in items.items():
        if last is None:
            out["datasets"][k] = {"status": "MISSING", "last": None, "lag_sessions": None}
            continue
        last_d = pd.Timestamp(str(last)).date()
        lag = calendar.trading_days_between(last_d, exp) if last_d < exp else 0
        out["datasets"][k] = {"status": "FRESH" if lag == 0 else "STALE", "last": str(last), "lag_sessions": lag}
    paths.ensure_dirs()
    paths.QUALITY.write_text(json.dumps(out, indent=2))
    return out


def load() -> dict:
    if paths.QUALITY.exists():
        return json.loads(paths.QUALITY.read_text())
    return check()


if __name__ == "__main__":
    print(json.dumps(check(), indent=2))
