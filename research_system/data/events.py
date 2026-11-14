"""Public company events: results calendar, announcements, credit ratings,
corporate actions. Public, lawfully published exchange filings only — never
leaked, private or material non-public information.

Store: data/research/events.sqlite
    calendar(symbol, event_date, purpose, description, source_url, fetched_at)
    announcements(id, symbol, published_at, category, subject, text, source_url, fetched_at)

    python -m research_system.data.events            # refresh calendar + last 3 days of filings
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from research_system import paths
from research_system.data.nse import NSE

BOARD_API = ("https://www.nseindia.com/api/corporate-board-meetings?index=equities"
             "&from_date={frm}&to_date={to}")
KEEP_DAYS = 45
ANN_API = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           "&from_date={frm}&to_date={to}")

# Keyword classifier for announcement subjects/text. Order matters: first match wins.
CATEGORIES = [
    ("credit_rating", r"credit rating|rating agenc|crisil|icra|care ratings|india ratings|brickwork"),
    ("results", r"financial results|outcome of board meeting.*result|quarterly results|audited results"),
    ("order_win", r"order|contract award|letter of award|bagged|secures"),
    ("m_and_a", r"acquisition|amalgamation|merger|demerger|scheme of arrangement|takeover|open offer"),
    ("capital_raise", r"qip|preferential|rights issue|fund raising|allotment of|ncd|debentures"),
    ("pledge", r"pledge|encumbrance"),
    ("management", r"resignation|appointment|cessation|change in director|key managerial|ceo|cfo"),
    ("regulatory", r"sebi|show cause|penalty|order passed|tax demand|gst|income tax|search|raid|court|tribunal|nclt"),
    ("guidance_call", r"analyst|investor meet|earnings call|conference call|investor presentation|transcript"),
    ("clarification", r"clarification|news item|media report"),
    ("corporate_action", r"dividend|bonus|split|record date|buyback|buy back"),
]
_CAT_RE = [(c, re.compile(p, re.I)) for c, p in CATEGORIES]


def classify(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    for cat, rx in _CAT_RE:
        if rx.search(blob):
            return cat
    return "other"


def _db() -> sqlite3.Connection:
    paths.ensure_dirs()
    con = sqlite3.connect(str(paths.EVENTS_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS calendar(
        symbol TEXT, event_date TEXT, purpose TEXT, description TEXT, source_url TEXT,
        fetched_at TEXT, PRIMARY KEY(symbol, event_date, purpose))""")
    con.execute("""CREATE TABLE IF NOT EXISTS announcements(
        id TEXT PRIMARY KEY, symbol TEXT, published_at TEXT, category TEXT, subject TEXT,
        text TEXT, source_url TEXT, fetched_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ann_sym ON announcements(symbol, published_at)")
    return con


def update_calendar(nse: NSE | None = None, back: int = 30, ahead: int = 60) -> int:
    """Board meetings from `back` days ago to `ahead` days out (results calendar)."""
    nse = nse or NSE()
    frm, to = date.today() - timedelta(days=back), date.today() + timedelta(days=ahead)
    url = BOARD_API.format(frm=frm.strftime("%d-%m-%Y"), to=to.strftime("%d-%m-%Y"))
    j = nse.json(url)
    if not isinstance(j, list):
        print("[events] board-meeting calendar unavailable")
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for x in j:
        d = pd.to_datetime(str(x.get("bm_date", "")), format="%d-%b-%Y", errors="coerce")
        if pd.isna(d):
            continue
        desc = str(x.get("bm_desc") or "")
        purpose = "results" if re.search(r"financial result|results", desc + str(x.get("bm_purpose")), re.I) \
            else str(x.get("bm_purpose") or "board_meeting").lower()
        rows.append((str(x.get("bm_symbol", "")).upper(), d.strftime("%Y%m%d"), purpose, desc[:1000],
                     x.get("attachment") or x.get("ixbrl") or url, now))
    con = _db()
    con.executemany("INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    print(f"[events] calendar: {len(rows)} board meetings stored")
    return len(rows)


def update_announcements(days: int = 3, nse: NSE | None = None) -> int:
    nse = nse or NSE()
    end, start = date.today(), date.today() - timedelta(days=days)
    j = nse.json(ANN_API.format(frm=start.strftime("%d-%m-%Y"), to=end.strftime("%d-%m-%Y")))
    if not isinstance(j, list):
        print("[events] announcements unavailable")
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for x in j:
        pub = pd.to_datetime(str(x.get("an_dt", "")), format="%d-%b-%Y %H:%M:%S", errors="coerce")
        if pd.isna(pub):
            continue
        sym = str(x.get("symbol", "")).upper()
        subj, text = str(x.get("desc") or ""), str(x.get("attchmntText") or "")
        url = x.get("attchmntFile") or ""
        rid = hashlib.md5(f"{sym}|{pub}|{subj}|{url}".encode()).hexdigest()
        rows.append((rid, sym, pub.isoformat(), classify(subj, text), subj[:300], text[:2000], url, now))
    con = _db()
    con.executemany("INSERT OR IGNORE INTO announcements VALUES (?,?,?,?,?,?,?,?)", rows)
    # keep the repository small: filings older than KEEP_DAYS are pruned (the source stays public)
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    con.execute("DELETE FROM announcements WHERE published_at < ?", (cutoff,))
    con.execute("DELETE FROM calendar WHERE event_date < ?", ((date.today() - timedelta(days=400)).strftime("%Y%m%d"),))
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"[events] announcements: {len(rows)} filings in {start}..{end}")
    return len(rows)


def calendar_for(symbols=None, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Scheduled events (board meetings / results) plus corporate-action ex-dates."""
    con = _db()
    cal = pd.read_sql("SELECT * FROM calendar", con)
    con.close()
    ca = pd.DataFrame()
    if paths.CORPORATE_ACTIONS.exists():
        ca = pd.read_csv(paths.CORPORATE_ACTIONS, dtype={"EX_DATE": str})
        ca = ca[ca["SOURCE"] == "nse"]
        ca = pd.DataFrame({"symbol": ca["SYMBOL"], "event_date": ca["EX_DATE"],
                           "purpose": "ex_date_" + ca["KIND"], "description": ca["SUBJECT"],
                           "source_url": "nse:corporate-actions", "fetched_at": ""})
    out = pd.concat([cal, ca], ignore_index=True)
    if symbols is not None:
        out = out[out["symbol"].isin([s.upper() for s in symbols])]
    if start:
        out = out[out["event_date"] >= start]
    if end:
        out = out[out["event_date"] <= end]
    return out.sort_values(["event_date", "symbol"]).reset_index(drop=True)


def announcements_for(symbol: str, since: str | None = None) -> pd.DataFrame:
    con = _db()
    q = "SELECT * FROM announcements WHERE symbol = ?"
    args = [symbol.upper()]
    if since:
        q += " AND published_at >= ?"
        args.append(since)
    df = pd.read_sql(q + " ORDER BY published_at DESC", con, params=args)
    con.close()
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    nse = NSE()
    update_calendar(nse)
    update_announcements(a.days, nse)


if __name__ == "__main__":
    main()
