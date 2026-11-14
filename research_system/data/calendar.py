"""NSE trading calendar and session phases.

Holidays come from NSE's published holiday master (capital-market segment)
and are cached in data/research/nse_holidays.json. Session times are taken
from config (defaults follow NSE's published equity schedule: pre-open
09:00-09:15 IST, regular 09:15-15:30 IST). Verify special sessions (e.g.
Muhurat trading) against the exchange calendar; the cached file records the
morning/evening session fields NSE publishes for such days.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from research_system import paths
from research_system.data.nse import NSE

HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"
IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_SESSION = {"pre_open_start": "09:00", "pre_open_end": "09:15",
                   "regular_start": "09:15", "regular_end": "15:30"}


def update(nse: NSE | None = None) -> dict:
    paths.ensure_dirs()
    nse = nse or NSE()
    j = nse.json(HOLIDAY_API)
    if not isinstance(j, dict) or "CM" not in j:
        print("[calendar] holiday master unavailable; keeping cached calendar")
        return load()
    out = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "holidays": [], "special": []}
    for x in j["CM"]:
        d = pd.to_datetime(str(x.get("tradingDate", "")), format="%d-%b-%Y", errors="coerce")
        if pd.isna(d):
            continue
        rec = {"date": d.strftime("%Y-%m-%d"), "description": x.get("description")}
        if x.get("morning_session") or x.get("evening_session"):
            rec.update(morning_session=x.get("morning_session"), evening_session=x.get("evening_session"))
            out["special"].append(rec)
        out["holidays"].append(rec)
    paths.CALENDAR.write_text(json.dumps(out, indent=2))
    print(f"[calendar] {len(out['holidays'])} holidays cached ({len(out['special'])} with special sessions)")
    return out


def load() -> dict:
    if paths.CALENDAR.exists():
        return json.loads(paths.CALENDAR.read_text())
    return {"fetched_at": None, "holidays": [], "special": []}


def holidays() -> set[str]:
    return {h["date"] for h in load()["holidays"]}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in holidays()


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_days_between(a: date, b: date) -> int:
    """Number of trading sessions in (a, b]."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += is_trading_day(d)
    return n


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def phase(now: datetime | None = None, session: dict | None = None) -> str:
    """One of: closed, pre_market, pre_open, regular, post_close."""
    session = {**DEFAULT_SESSION, **(session or {})}
    now = (now or datetime.now(IST)).astimezone(IST)
    if not is_trading_day(now.date()):
        return "closed"
    t = now.time()
    if t < _t(session["pre_open_start"]):
        return "pre_market"
    if t < _t(session["pre_open_end"]):
        return "pre_open"
    if t < _t(session["regular_end"]):
        return "regular"
    return "post_close"


if __name__ == "__main__":
    update()
    print("phase now:", phase())
