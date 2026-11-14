"""
corporate_actions.py
====================
Split / bonus / consolidation price adjustment for NSE end-of-day data.

THE PROBLEM
-----------
NSE bhavcopy prices are raw. On a 1:1 bonus the stock "falls" 50% overnight;
on a 10:1 split it "falls" 90%. Left unadjusted that corrupts every
return-based input for months afterwards (momentum, volatility, ATR, moving
averages) and makes a held position look like it crashed through its stop.
NSE's own PREV_CLOSE column is NOT adjusted, so it cannot be used to recover
the factor.

THE FIX
-------
1. Pull the official corporate-action list from NSE
   (www.nseindia.com/api/corporates-corporateActions) and parse exact factors:
       Bonus a:b                      -> factor = b / (a + b)
       Face value split Rs X -> Rs Y  -> factor = Y / X
       Consolidation  Rs X -> Rs Y    -> factor = Y / X  (> 1)
   `factor` multiplies every price BEFORE the ex-date (volume is divided by
   it). The latest prices are never changed, so they stay tradable prices.
2. Validate each event against the observed overnight gap. An official event
   whose factor does not match the tape is kept but marked `mismatch`.
3. Demergers / schemes of arrangement carry no ratio in the feed; their factor
   is taken from the observed open-vs-previous-close gap when it is material.
4. Gaps that no circuit band allows (one-day close ratio < 0.6 or > 1.8) with
   no official event nearby are recorded as `inferred`, snapped to a standard
   ratio when one is close. Inferred events adjust research data, but are
   never applied to live positions automatically (see paper_trade_manager).

Raw data is never rewritten. Adjustment happens in memory at load time from
data/market_database/corporate_actions.csv, which is plain text and auditable.

USAGE
    # build / refresh the event file (official feed + inference over the data)
    python corporate_actions.py --update --master data/market_database/master_history.csv.gz \
        --history-dir data/market_database/history --start 2016-01-01

    # daily top-up (last 30 days)
    python corporate_actions.py --update --master ... --days 30

    # library
    from corporate_actions import load_events, adjust_long
    df = adjust_long(df, load_events(path))
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

NSE_HOME = "https://www.nseindia.com/"
NSE_CA_API = ("https://www.nseindia.com/api/corporates-corporateActions"
              "?index=equities&from_date={frm}&to_date={to}")
EVENT_COLS = ["SYMBOL", "EX_DATE", "KIND", "FACTOR", "FACTOR_TYPE", "SOURCE", "STATUS", "OBS_RATIO", "SUBJECT"]
# FACTOR_TYPE:
#   official_ratio          bonus / split / consolidation ratio published by NSE: changes share counts
#   price_derived_estimate  demerger / arrangement / inferred gap: chart and stop continuity only,
#                           never a substitute for the shares a holder actually received
#   official_cash_distribution  verified cash dividend (security_events.csv, TYPE cash_dividend):
#                           (previous close - dividend) / previous close. Continuity only; the
#                           cash itself is a separate entitlement (portfolio_accounting.py)
OFFICIAL_SOURCES = ("nse", "security_events")
SHARE_COUNT_KINDS = ("bonus", "split", "consolidation")
PRICE_COLS = ("OPEN", "HIGH", "LOW", "CLOSE")

# Standard ratios an inferred gap is snapped to when within SNAP_TOL.
STANDARD_FACTORS = sorted({b / (a + b) for a in range(1, 11) for b in range(1, 11)}
                          | {1 / k for k in (2, 4, 5, 10, 20, 25, 50, 100)} | {2 / 5, 1 / 3})
SNAP_TOL = 0.04
# Tolerance when matching an official factor to the observed gap. Generous on
# purpose: the ex-day itself can move up to a circuit band.
VALIDATE_LO, VALIDATE_HI = 0.78, 1.28
# No NSE circuit band allows a genuine one-day move outside these ratios.
INFER_LO, INFER_HI = 0.60, 1.80


# =========================================================
# Parsing the official feed
# =========================================================
_BONUS = re.compile(r"bonus\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", re.I)
_FV = re.compile(r"from\s*r[se]\.?\s*(\d+(?:\.\d+)?)\D*?to\s*r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_NO_RATIO = re.compile(r"demerger|arrangement|arangement|capital reduction|amalgamation", re.I)


def parse_subject(subject: str) -> tuple[str | None, float | None]:
    """Return (kind, factor) for one corporate-action subject line.

    kind is None for actions that do not change the share count (dividends,
    AGMs, interest payments, debenture bonuses ...).
    """
    s = " ".join(str(subject).split())
    low = s.lower()
    if "debenture" in low:            # "Bonus - 1 Debenture For 1 Equity Share": no share-count change
        return None, None
    m = _BONUS.search(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            return "bonus", b / (a + b)
    if "split" in low or "sub-division" in low or "consolidat" in low:
        m = _FV.search(s)
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            if x > 0 and y > 0 and x != y:
                return ("split" if y < x else "consolidation"), y / x
    if _NO_RATIO.search(s):
        return "demerger" if "demerger" in low else "arrangement", None
    return None, None


def _nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
    })
    try:
        s.get(NSE_HOME, timeout=20)
    except Exception:
        pass
    return s


def fetch_official(start: date, end: date, pause: float = 1.5) -> pd.DataFrame:
    """Official split/bonus/demerger list from NSE, one calendar year per request."""
    if requests is None:
        raise RuntimeError("requests is not installed")
    sess = _nse_session()
    rows = []
    cur = start
    while cur <= end:
        stop = min(date(cur.year, 12, 31), end)
        url = NSE_CA_API.format(frm=cur.strftime("%d-%m-%Y"), to=stop.strftime("%d-%m-%Y"))
        for attempt in range(3):
            try:
                r = sess.get(url, timeout=60)
                if r.status_code == 200:
                    rows.extend(r.json())
                    break
                if r.status_code in (401, 403):      # cookie expired -> new session
                    sess = _nse_session()
            except Exception:
                pass
            time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"NSE corporate-actions feed unavailable for {cur}..{stop}")
        cur = stop + timedelta(days=1)
        time.sleep(pause)

    out = []
    for x in rows:
        if str(x.get("series", "")).strip().upper() != "EQ":
            continue
        kind, factor = parse_subject(x.get("subject", ""))
        if kind is None:
            continue
        ex = pd.to_datetime(str(x.get("exDate", "")).strip(), format="%d-%b-%Y", errors="coerce")
        if pd.isna(ex):
            continue
        out.append({"SYMBOL": str(x["symbol"]).strip().upper(), "EX_DATE": ex.strftime("%Y%m%d"),
                    "KIND": kind, "FACTOR": factor, "SOURCE": "nse",
                    "SUBJECT": " ".join(str(x.get("subject", "")).split())})
    df = pd.DataFrame(out, columns=["SYMBOL", "EX_DATE", "KIND", "FACTOR", "SOURCE", "SUBJECT"])
    return df.drop_duplicates(["SYMBOL", "EX_DATE", "KIND"])


# =========================================================
# Price data helpers
# =========================================================
def load_price_panel(master: Path | None, history_dir: Path | None,
                     start: str | None = None, integrity: bool = True) -> pd.DataFrame:
    """Long OHLCV frame (raw prices) from the yearly parquet archive + master CSV.

    With integrity=True (default) the derived-data layer is applied in memory:
    quarantined sessions are dropped/replaced (data_integrity.py) and renamed
    symbols are stitched to their current symbol (security_master.py). The raw
    files themselves are never modified."""
    cols = ["DATE", "SYMBOL", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]
    parts = []
    if history_dir is not None:
        for f in sorted(glob.glob(str(Path(history_dir) / "nse_*.parquet"))):
            parts.append(pd.read_parquet(f, columns=cols))
    if master is not None and Path(master).exists():
        m = pd.read_csv(master, low_memory=False)
        m.columns = [c.strip().upper().replace(" ", "_") for c in m.columns]
        if "SERIES" in m.columns:
            m = m[m["SERIES"].astype(str).str.upper() == "EQ"]
        parts.append(m[[c for c in cols if c in m.columns]])
    if not parts:
        raise FileNotFoundError("no price data found")
    for p in parts:
        p["DATE"] = p["DATE"].astype(str)
        p["SYMBOL"] = p["SYMBOL"].astype(str).str.strip().str.upper()
    df = pd.concat(parts, ignore_index=True)
    for c in cols[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if start:
        df = df[df["DATE"] >= pd.Timestamp(start).strftime("%Y%m%d")]
    df = df.drop_duplicates(["DATE", "SYMBOL"], keep="last")
    if integrity:
        df = integrity_layer(df)
    return df.sort_values(["SYMBOL", "DATE"]).reset_index(drop=True)


def integrity_layer(df: pd.DataFrame) -> pd.DataFrame:
    """Quarantine corrections, then symbol stitching (both in memory)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import data_integrity
    import security_master
    df = data_integrity.apply(df)
    return security_master.stitch(df)


def _gaps(px: pd.DataFrame) -> pd.DataFrame:
    """Per symbol-day: previous stored close and the overnight ratios."""
    px = px.sort_values(["SYMBOL", "DATE"])
    g = px.groupby("SYMBOL", sort=False)
    out = px[["SYMBOL", "DATE", "OPEN", "CLOSE"]].copy()
    out["PREV_DATE"] = g["DATE"].shift(1)
    out["PREV_CLOSE"] = g["CLOSE"].shift(1)
    out["R_CLOSE"] = out["CLOSE"] / out["PREV_CLOSE"]
    out["R_OPEN"] = out["OPEN"] / out["PREV_CLOSE"]
    return out


def _snap(r: float) -> tuple[float, bool]:
    best = min(STANDARD_FACTORS, key=lambda f: abs(np.log(r / f)))
    return (best, True) if abs(r / best - 1) <= SNAP_TOL else (r, False)


# =========================================================
# Build the event table
# =========================================================
def build_events(official: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """Validate official events against prices and add inferred ones."""
    gaps = _gaps(px)
    sessions = np.sort(px["DATE"].unique())
    prev_session = dict(zip(sessions[1:], sessions[:-1]))
    # True when the previous stored close is from the immediately prior market
    # session. A gap across days without trading is a real move, not a split.
    gaps["CONTIG"] = gaps["PREV_DATE"] == gaps["DATE"].map(prev_session)
    by_sym = {s: g for s, g in gaps.groupby("SYMBOL", sort=False)}

    # Attach to each official event the first stored session on/after its
    # ex-date: that is where the gap shows in the data.
    recs = []
    for e in official.itertuples(index=False):
        g = by_sym.get(e.SYMBOL)
        row = g[g["DATE"] >= e.EX_DATE].head(1) if g is not None else None
        hit = row is not None and not row.empty and pd.notna(row["PREV_CLOSE"].iloc[0])
        obs, contig = np.nan, False
        if hit:
            r = row.iloc[0]
            obs = float(r["R_OPEN"] if pd.notna(r["R_OPEN"]) and r["R_OPEN"] > 0 else r["R_CLOSE"])
            contig = bool(r["CONTIG"])
        recs.append({"SYMBOL": e.SYMBOL, "EX_DATE": e.EX_DATE, "KIND": e.KIND,
                     "FACTOR": e.FACTOR, "SOURCE": "nse", "SUBJECT": e.SUBJECT,
                     "FACTOR_TYPE": "official_ratio" if e.KIND in SHARE_COUNT_KINDS else "price_derived_estimate",
                     "SESSION": row["DATE"].iloc[0] if hit else None, "CONTIG": contig,
                     "OBS_RATIO": round(obs, 4) if hit else np.nan, "STATUS": "no_price"})
    off = pd.DataFrame(recs)

    events = []
    if not off.empty:
        off["FACTOR"] = pd.to_numeric(off["FACTOR"], errors="coerce")
        # Events that land on the same session (split + bonus together, or two
        # ex-dates with no trading in between) are validated as one product.
        for _, grp in off.groupby(["SYMBOL", off["SESSION"].fillna("NA_" + off["EX_DATE"])], sort=False):
            obs = grp["OBS_RATIO"].iloc[0]
            known = grp["FACTOR"].dropna()
            for i, e in grp.iterrows():
                rec = e.drop(labels=["SESSION", "CONTIG"]).to_dict()
                if pd.isna(obs):
                    events.append(rec)
                    continue
                if pd.isna(e["FACTOR"]):
                    # demerger / arrangement: use the tape when the gap is material
                    if known.empty and obs < 0.95:
                        rec["FACTOR"], rec["STATUS"] = obs, "from_gap"
                    else:
                        rec["STATUS"] = "immaterial"
                else:
                    ok = VALIDATE_LO <= obs / known.prod() <= VALIDATE_HI
                    rec["STATUS"] = "validated" if ok else "mismatch"
                events.append(rec)
            # A residual gap no circuit band allows means a concurrent action the
            # feed missed (typically a split listed under another date). Infer it.
            if pd.notna(obs) and not known.empty and grp["CONTIG"].iloc[0]:
                resid = obs / known.prod()
                if resid < INFER_LO or resid > INFER_HI:
                    f, snapped = _snap(float(resid))
                    events.append({"SYMBOL": grp["SYMBOL"].iloc[0], "EX_DATE": grp["SESSION"].iloc[0],
                                   "KIND": "inferred", "FACTOR": f, "FACTOR_TYPE": "price_derived_estimate", "SOURCE": "inferred",
                                   "STATUS": "residual", "OBS_RATIO": round(float(resid), 4),
                                   "SUBJECT": "gap left after official factors "
                                              f"({'snapped' if snapped else 'raw'})"})

    ev = pd.DataFrame(events, columns=EVENT_COLS)
    known = ev[ev["FACTOR"].notna()]

    # Inferred: impossible one-day moves between consecutive sessions with no
    # official event within a week. Mostly ETF unit splits, which the equities
    # corporate-action feed does not list.
    # A share-count change shows at the OPEN; an intraday crash (DHFL 2018,
    # YESBANK 2020) opens near the prior close and must not be adjusted away.
    gap_open = gaps["R_OPEN"].isna() | (gaps["R_OPEN"] < INFER_LO) | (gaps["R_OPEN"] > INFER_HI)
    cand = gaps[((gaps["R_CLOSE"] < INFER_LO) | (gaps["R_CLOSE"] > INFER_HI))
                & gaps["CONTIG"] & gap_open].dropna(subset=["PREV_CLOSE"])
    if not cand.empty:
        kd = known.assign(EXD=pd.to_datetime(known["EX_DATE"]))
        for c in cand.itertuples(index=False):
            d = pd.Timestamp(c.DATE)
            near = kd[(kd["SYMBOL"] == c.SYMBOL) & ((kd["EXD"] - d).abs() <= pd.Timedelta(days=7))]
            if not near.empty:
                continue
            obs = c.R_OPEN if pd.notna(c.R_OPEN) and c.R_OPEN > 0 else c.R_CLOSE
            f, snapped = _snap(float(obs))
            events.append({"SYMBOL": c.SYMBOL, "EX_DATE": c.DATE,
                           "KIND": "inferred", "FACTOR": f, "FACTOR_TYPE": "price_derived_estimate", "SOURCE": "inferred",
                           "STATUS": "snapped" if snapped else "raw_gap",
                           "OBS_RATIO": round(float(obs), 4),
                           "SUBJECT": f"one-day ratio {c.R_CLOSE:.3f} outside circuit limits"})

    ev = pd.DataFrame(events, columns=EVENT_COLS)
    ev = ev[ev["FACTOR"].notna() & (ev["FACTOR"] > 0)]
    return ev.sort_values(["EX_DATE", "SYMBOL"]).reset_index(drop=True)


def cash_distribution_events(px: pd.DataFrame, sec: pd.DataFrame | None = None) -> pd.DataFrame:
    """Continuity factors for verified cash dividends recorded in security_events.csv:
    factor = (last stored close before EX_DATE - dividend) / that close."""
    if sec is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import security_master
        sec = security_master.load_events(statuses=("verified",))
    sec = sec[sec["TYPE"] == "cash_dividend"] if len(sec) else sec
    rows = []
    for e in sec.itertuples(index=False):
        g = px[(px["SYMBOL"] == e.SYMBOL)].sort_values("DATE")
        prev = g[g["DATE"] < e.EX_DATE].tail(1)
        on = g[g["DATE"] >= e.EX_DATE].head(1)
        if prev.empty or pd.isna(e.CASH_PER_SHARE):
            continue
        pc, div = round(float(prev["CLOSE"].iloc[0]), 2), float(e.CASH_PER_SHARE)   # NSE prices are in paise
        if not 0 < div < pc:
            continue
        obs = float(on["OPEN"].iloc[0]) / pc if len(on) and on["OPEN"].iloc[0] > 0 else np.nan
        rows.append({"SYMBOL": e.SYMBOL, "EX_DATE": e.EX_DATE, "KIND": "cash_distribution",
                     "FACTOR": (pc - div) / pc, "FACTOR_TYPE": "official_cash_distribution",
                     "SOURCE": "security_events", "STATUS": "validated",
                     "OBS_RATIO": round(obs, 4) if obs == obs else np.nan,
                     "SUBJECT": f"{e.EVENT_ID}: cash dividend Rs{div:g}/share; factor ({pc:g} - {div:g}) / {pc:g}"})
    return pd.DataFrame(rows, columns=EVENT_COLS)


def load_events(path: Path | str, sources: tuple[str, ...] | None = None) -> pd.DataFrame:
    """Read the event file. `sources` filters e.g. ("nse",)."""
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=EVENT_COLS)
    ev = pd.read_csv(p, dtype={"EX_DATE": str, "SYMBOL": str})
    ev["FACTOR"] = pd.to_numeric(ev["FACTOR"], errors="coerce")
    if "FACTOR_TYPE" not in ev.columns:
        ev["FACTOR_TYPE"] = np.where((ev["SOURCE"] == "nse") & ev["KIND"].isin(SHARE_COUNT_KINDS),
                                     "official_ratio", "price_derived_estimate")
    ev = ev[ev["FACTOR"].notna() & (ev["FACTOR"] > 0)]
    if sources:
        ev = ev[ev["SOURCE"].isin(sources)]
    return ev.reset_index(drop=True)


def share_count_events(ev: pd.DataFrame) -> pd.DataFrame:
    """Only events that change how many shares a holder owns (official NSE ratios)."""
    return ev[(ev["FACTOR_TYPE"] == "official_ratio") & ev["KIND"].isin(SHARE_COUNT_KINDS)].reset_index(drop=True)


def merge_events(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """New rows win on (SYMBOL, EX_DATE, SOURCE); older history is kept."""
    out = pd.concat([old, new], ignore_index=True)
    # KIND is part of the key: a split and a bonus can share an ex-date.
    out = out.drop_duplicates(["SYMBOL", "EX_DATE", "SOURCE", "KIND"], keep="last")
    # An official event (NSE feed, or a verified cash dividend from security_events.csv)
    # supersedes an inferred one for the same move (residuals are the exception: they
    # complete an official event).
    off = out[out["SOURCE"].isin(OFFICIAL_SOURCES)].assign(EXD=lambda d: pd.to_datetime(d["EX_DATE"]))
    inf = out[(out["SOURCE"] == "inferred") & (out["STATUS"] != "residual")]
    drop = []
    for i, r in inf.iterrows():
        near = off[(off["SYMBOL"] == r["SYMBOL"]) &
                   ((off["EXD"] - pd.Timestamp(r["EX_DATE"])).abs() <= pd.Timedelta(days=7))]
        if not near.empty:
            drop.append(i)
    out = out.drop(index=drop)
    return out.reindex(columns=EVENT_COLS).sort_values(["EX_DATE", "SYMBOL"]).reset_index(drop=True)


# =========================================================
# Applying adjustments
# =========================================================
def _cum_table(events: pd.DataFrame, last_date=None) -> pd.DataFrame:
    """Per (SYMBOL, EX_DATE): product of this and every later factor.

    A row dated d is multiplied by the CUM of the first event with EX_DATE > d.
    Events after `last_date` (announced, not yet ex) are ignored: applying them
    would rescale today's prices before the share count has changed.
    """
    if events.empty:
        return pd.DataFrame(columns=["SYMBOL", "EX_DT", "CUM"])
    ev = events.copy()
    ev["EX_DT"] = pd.to_datetime(ev["EX_DATE"].astype(str), format="%Y%m%d")
    if last_date is not None:
        ev = ev[ev["EX_DT"] <= pd.Timestamp(last_date)]
        if ev.empty:
            return pd.DataFrame(columns=["SYMBOL", "EX_DT", "CUM"])
    ev = ev.groupby(["SYMBOL", "EX_DT"], as_index=False)["FACTOR"].prod()
    ev = ev.sort_values(["SYMBOL", "EX_DT"], ascending=[True, False])
    ev["CUM"] = ev.groupby("SYMBOL")["FACTOR"].cumprod()
    return ev[["SYMBOL", "EX_DT", "CUM"]]


def adjustment_factor(df: pd.DataFrame, events: pd.DataFrame,
                      date_col: str = "DATE_DT") -> pd.Series:
    """Multiplier for each row of a long frame (1.0 where nothing applies)."""
    if df.empty:
        return pd.Series(1.0, index=df.index)
    cum = _cum_table(events, last_date=pd.to_datetime(df[date_col]).max())
    if cum.empty:
        return pd.Series(1.0, index=df.index)
    left = df[["SYMBOL", date_col]].copy()
    left["_i"] = np.arange(len(left))
    left[date_col] = pd.to_datetime(left[date_col])
    left = left.sort_values(date_col)
    right = cum.rename(columns={"EX_DT": date_col}).sort_values(date_col)
    # first event strictly AFTER the row date
    m = pd.merge_asof(left, right, on=date_col, by="SYMBOL",
                      direction="forward", allow_exact_matches=False)
    f = m.sort_values("_i")["CUM"].fillna(1.0).to_numpy()
    return pd.Series(f, index=df.index)


def adjust_long(df: pd.DataFrame, events: pd.DataFrame, date_col: str = "DATE_DT") -> pd.DataFrame:
    """Back-adjust OHLC (and VOLUME) of a long frame in place of the raw columns.

    Adds ADJ_FACTOR. TRADED_VALUE is left alone: rupee turnover is unaffected.
    """
    out = df.copy()
    if date_col not in out.columns:
        out[date_col] = pd.to_datetime(out["DATE"].astype(str), format="%Y%m%d", errors="coerce")
    f = adjustment_factor(out, events, date_col)
    for c in PRICE_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce") * f
    if "VOLUME" in out.columns:
        out["VOLUME"] = pd.to_numeric(out["VOLUME"], errors="coerce") / f
    out["ADJ_FACTOR"] = f
    return out


def adjust_wide(W: dict, events: pd.DataFrame) -> dict:
    """Back-adjust a dict of wide (date x symbol) matrices from load_wide()."""
    idx = W["CLOSE"].index
    cum = _cum_table(events, last_date=idx.max())
    if cum.empty:
        return W
    F = pd.DataFrame(1.0, index=idx, columns=W["CLOSE"].columns)
    for sym, grp in cum.groupby("SYMBOL"):
        if sym not in F.columns:
            continue
        col = np.ones(len(idx))
        # newest first, so each older event overwrites only the dates before it
        for ex_dt, c in sorted(zip(grp["EX_DT"], grp["CUM"]), reverse=True):
            col[idx < ex_dt] = c
        F[sym] = col
    out = dict(W)
    for c in PRICE_COLS:
        if c in out:
            out[c] = W[c] * F
    if "VOLUME" in out:
        out["VOLUME"] = W["VOLUME"] / F
    return out


# =========================================================
# CLI
# =========================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="Fetch the official feed and rebuild events.")
    ap.add_argument("--master", default="./data/market_database/master_history.csv.gz")
    ap.add_argument("--history-dir", default=None)
    ap.add_argument("--events", default="./data/market_database/corporate_actions.csv")
    ap.add_argument("--start", default=None, help="Full rebuild from this date (YYYY-MM-DD).")
    ap.add_argument("--days", type=int, default=30, help="Top-up window when --start is not given.")
    ap.add_argument("--ahead", type=int, default=45, help="Also fetch announced actions this many days ahead.")
    ap.add_argument("--no-fetch", action="store_true", help="Inference only (no network).")
    args = ap.parse_args()

    events_path = Path(args.events)
    if not args.update:
        ev = load_events(events_path)
        print(ev.groupby(["SOURCE", "STATUS"]).size().to_string() if len(ev) else "no events")
        return

    # Look ahead: NSE lists actions before their ex-date. Upcoming events feed the
    # event calendar; adjustment ignores them until the ex-date is in the data.
    end = date.today() + timedelta(days=args.ahead)
    start = (pd.Timestamp(args.start).date() if args.start else date.today() - timedelta(days=args.days))
    # price context starts a little earlier so the first gap has a previous close
    px = load_price_panel(Path(args.master), Path(args.history_dir) if args.history_dir else None,
                          start=(pd.Timestamp(start) - pd.Timedelta(days=15)).strftime("%Y-%m-%d"))

    official = pd.DataFrame(columns=["SYMBOL", "EX_DATE", "KIND", "FACTOR", "SOURCE", "SUBJECT"])
    fetched = False
    if not args.no_fetch:
        try:
            official, fetched = fetch_official(start, end), True
        except Exception as e:
            # Degrade to inference only; previously stored official rows are kept.
            print(f"[WARN] official feed unavailable ({e}); inference only this run")
    print(f"[INFO] official share-count events {start}..{end}: {len(official)}")
    upcoming = official[official["EX_DATE"] > date.today().strftime("%Y%m%d")]
    if len(upcoming):
        print("[INFO] upcoming: " + ", ".join(f"{r.SYMBOL} {r.KIND} {r.EX_DATE}" for r in upcoming.itertuples()))

    win = start.strftime("%Y%m%d")
    new = pd.concat([build_events(official, px), cash_distribution_events(px)], ignore_index=True)
    new = new[new["EX_DATE"] >= win]
    # round_trip: the default parser drops the 17th digit, so every update would rewrite untouched factors
    old = (pd.read_csv(events_path, dtype={"EX_DATE": str}, float_precision="round_trip") if events_path.exists()
           else pd.DataFrame(columns=EVENT_COLS))
    # The window is recomputed in full; keep official rows only if we did not refetch them.
    keep = (old["EX_DATE"] < win) | ((old["SOURCE"] == "nse") & (not fetched))
    merged = merge_events(old[keep], new)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(events_path, index=False)
    print(f"[DONE] {len(merged)} events in {events_path}")
    print(merged.groupby(["SOURCE", "STATUS"]).size().to_string())


if __name__ == "__main__":
    main()
