"""
data_integrity.py
=================
Derived-data correction layer for NSE end-of-day prices.

Raw price files (master_history.csv.gz, history/nse_YYYY.parquet) are never
rewritten. Known-bad sessions are listed, with evidence, in

    data/market_database/derived/quarantine.csv
        DATE, ACTION (drop_session | replace_session | relabel_session), REPLACEMENT,
        REASON, EVIDENCE, ADDED

  drop_session     the stored session is not a real session (e.g. a holiday copy)
  replace_session  rows replaced by corrections/<file> (REPLACEMENT)
  relabel_session  the rows are a real session stored under the wrong date; they are
                   moved to the date written inside the NSE file (REPLACEMENT, YYYYMMDD)

and corrected rows live in data/market_database/derived/corrections/. Every
loader applies the list in memory (apply()), so signals, backtests and the
paper ledger all see the same corrected data.

Corrections are built only from an NSE file whose OWN date field matches the
session (fetch_session), never from interpolation.

    python data_integrity.py --scan                 # flag suspect sessions (no changes)
    python data_integrity.py --repair 20190930 --reason "..." --evidence "..."
    python data_integrity.py --drop 20260528 --reason "..." --evidence "..."
    python data_integrity.py --relabel 20211105 --to 20211104 --reason "..." --evidence "..."
    python data_integrity.py --list
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DERIVED = ROOT / "data" / "market_database" / "derived"
QUARANTINE = DERIVED / "quarantine.csv"
CORRECTIONS = DERIVED / "corrections"
COLS = ["DATE", "ACTION", "REPLACEMENT", "REASON", "EVIDENCE", "ADDED"]
PRICE_COLS = ["DATE", "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"]


# =========================================================
# Quarantine list
# =========================================================
def load_quarantine(path: Path | None = None) -> pd.DataFrame:
    p = Path(path) if path else QUARANTINE
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    q = pd.read_csv(p, dtype={"DATE": str, "REPLACEMENT": str}).fillna("")
    return q


def _replacement_rows(rel: str) -> pd.DataFrame:
    p = DERIVED / rel
    df = pd.read_csv(p, dtype={"DATE": str, "SYMBOL": str, "SERIES": str})
    return df


def apply(df: pd.DataFrame, quarantine: pd.DataFrame | None = None, series: str | None = "EQ") -> pd.DataFrame:
    """Drop / replace quarantined sessions in a long OHLCV frame.

    `df` must carry DATE as YYYYMMDD (string or int). Replacement rows are
    restricted to `series` (default EQ, matching the stored data) and to the
    columns present in `df`.
    """
    q = load_quarantine() if quarantine is None else quarantine
    if q.empty or df.empty:
        return df
    out = df.copy()
    as_int = pd.api.types.is_integer_dtype(out["DATE"])
    key = out["DATE"].astype(str)
    relabel = {str(r.DATE): str(r.REPLACEMENT) for r in q[q["ACTION"] == "relabel_session"].itertuples(index=False)}
    bad = set(q.loc[q["ACTION"] != "relabel_session", "DATE"].astype(str))
    out = out[~key.isin(bad)]
    if relabel:
        key = out["DATE"].astype(str)
        clash = set(relabel.values()) & set(key)
        if clash:
            raise ValueError(f"relabel target date(s) already hold stored rows: {sorted(clash)}")
        m = key.isin(relabel)
        if m.any():
            new = key[m].map(relabel)
            out.loc[m, "DATE"] = new.astype(int) if as_int else new
            if "DATE_DT" in out.columns:
                out.loc[m, "DATE_DT"] = pd.to_datetime(new, format="%Y%m%d")
    adds = []
    for r in q[q["ACTION"] == "replace_session"].itertuples(index=False):
        rep = _replacement_rows(r.REPLACEMENT)
        if series and "SERIES" in rep.columns:
            rep = rep[rep["SERIES"].astype(str).str.upper() == series]
        keep = [c for c in out.columns if c in rep.columns]
        rep = rep[keep].copy()
        if as_int:
            rep["DATE"] = rep["DATE"].astype(int)
        adds.append(rep)
    if adds:
        out = pd.concat([out] + adds, ignore_index=True)
        if "DATE_DT" in df.columns and "DATE_DT" not in adds[0].columns:
            out["DATE_DT"] = pd.to_datetime(out["DATE"].astype(str), format="%Y%m%d")
    sort = [c for c in ("SYMBOL", "DATE") if c in out.columns]
    return out.sort_values(sort).reset_index(drop=True)


def _append(row: dict) -> None:
    DERIVED.mkdir(parents=True, exist_ok=True)
    q = load_quarantine()
    q = q[q["DATE"] != row["DATE"]]
    q = pd.concat([q, pd.DataFrame([row])], ignore_index=True)[COLS].sort_values("DATE")
    q.to_csv(QUARANTINE, index=False)


# =========================================================
# Authoritative re-fetch of one session
# =========================================================
def fetch_session(d: str) -> tuple[pd.DataFrame, str]:
    """All-series rows for session `d` (YYYYMMDD) from an NSE file whose own
    date field equals `d`. Tries the full-format file, then the legacy zip, first
    under d's own name and then under any name the relabel list records for d
    (e.g. the 4-Nov-2021 Muhurat session is in NSE's file named 05112021)."""
    sys.path.insert(0, str(ROOT))
    from research_system.data.nse import NSE
    n = NSE()
    dt = pd.Timestamp(d)
    q = load_quarantine()
    names = [d] + [str(r.DATE) for r in q[(q["ACTION"] == "relabel_session") &
                                          (q["REPLACEMENT"].astype(str) == d)].itertuples(index=False)]
    for name in names:
        fn = pd.Timestamp(name)
        url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{fn:%d%m%Y}.csv"
        t = n.text(url)
        if t:
            x = pd.read_csv(io.StringIO(t))
            x.columns = [c.strip() for c in x.columns]
            own = pd.to_datetime(x["DATE1"].astype(str).str.strip(), errors="coerce").mode().iloc[0]
            if own == dt:
                x = x.rename(columns={"OPEN_PRICE": "OPEN", "HIGH_PRICE": "HIGH", "LOW_PRICE": "LOW",
                                      "CLOSE_PRICE": "CLOSE", "TTL_TRD_QNTY": "VOLUME"})
                x["TRADED_VALUE"] = pd.to_numeric(x["TURNOVER_LACS"], errors="coerce") * 100_000
                return _norm(x, d), url
        mon = fn.strftime("%b").upper()
        url = f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{fn:%Y}/{mon}/cm{fn:%d}{mon}{fn:%Y}bhav.csv.zip"
        raw = n.zip_member(url)
        if raw is not None:
            x = pd.read_csv(io.BytesIO(raw))
            x.columns = [c.strip() for c in x.columns]
            own = pd.to_datetime(x["TIMESTAMP"].astype(str).str.strip(), errors="coerce").mode().iloc[0]
            if own == dt:
                x = x.rename(columns={"TOTTRDQTY": "VOLUME", "TOTTRDVAL": "TRADED_VALUE"})
                return _norm(x, d), url
    raise RuntimeError(f"no NSE file with its own date = {d} was found")


def _norm(x: pd.DataFrame, d: str) -> pd.DataFrame:
    x["DATE"] = d
    for c in ("SYMBOL", "SERIES"):
        x[c] = x[c].astype(str).str.strip().str.upper()
    for c in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x[PRICE_COLS]


def repair(d: str, reason: str, evidence: str) -> Path:
    rows, url = fetch_session(d)
    CORRECTIONS.mkdir(parents=True, exist_ok=True)
    rel = f"corrections/{d}_all_series.csv"
    rows.assign(SOURCE=url).to_csv(DERIVED / rel, index=False)
    _append({"DATE": d, "ACTION": "replace_session", "REPLACEMENT": rel, "REASON": reason,
             "EVIDENCE": f"{evidence} | replacement source: {url}", "ADDED": date.today().isoformat()})
    return DERIVED / rel


def drop(d: str, reason: str, evidence: str) -> None:
    _append({"DATE": d, "ACTION": "drop_session", "REPLACEMENT": "", "REASON": reason,
             "EVIDENCE": evidence, "ADDED": date.today().isoformat()})


def relabel(d: str, to: str, reason: str, evidence: str) -> None:
    """Move a real session stored under the wrong date to the date inside its NSE file."""
    _append({"DATE": d, "ACTION": "relabel_session", "REPLACEMENT": to, "REASON": reason,
             "EVIDENCE": evidence, "ADDED": date.today().isoformat()})


# =========================================================
# Detection (reports only; never edits)
# =========================================================
def scan(px: pd.DataFrame, n_names: int = 300, dup_thresh: float = 0.8) -> pd.DataFrame:
    """Flag sessions whose closes duplicate ANY other session, and sessions
    where many always-traded names spike and revert the next day."""
    w = px.pivot_table(index="DATE", columns="SYMBOL", values="CLOSE", aggfunc="last")
    core = w.notna().mean().sort_values(ascending=False).index[:n_names]
    arr = w[core].to_numpy()
    dates = w.index.astype(str).to_numpy()
    out = []
    for i in range(len(dates)):
        m = ~np.isnan(arr[i])
        same = (np.abs(arr[:, m] - arr[i][m]) < 0.006).mean(axis=1)
        same[i] = 0
        j = int(same.argmax())
        if same[j] > dup_thresh:
            out.append({"DATE": dates[i], "CHECK": "duplicate_of", "DETAIL": dates[j], "SCORE": round(float(same[j]), 3)})
    r = w[core].pct_change(fill_method=None)
    spk = ((r > 0.3) & (r.shift(-1) < -0.23)).sum(axis=1) + ((r < -0.23) & (r.shift(-1) > 0.3)).sum(axis=1)
    for d, k in spk[spk > 20].items():
        out.append({"DATE": str(d), "CHECK": "spike_and_revert", "DETAIL": f"{int(k)} names", "SCORE": float(k)})
    return pd.DataFrame(out, columns=["DATE", "CHECK", "DETAIL", "SCORE"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--repair")
    ap.add_argument("--drop")
    ap.add_argument("--relabel")
    ap.add_argument("--to")
    ap.add_argument("--reason", default="")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.repair:
        print("wrote", repair(a.repair, a.reason, a.evidence))
    if a.drop:
        drop(a.drop, a.reason, a.evidence)
        print("quarantined", a.drop)
    if a.relabel:
        if not a.to:
            ap.error("--relabel needs --to YYYYMMDD")
        relabel(a.relabel, a.to, a.reason, a.evidence)
        print("relabelled", a.relabel, "->", a.to)
    if a.scan:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from corporate_actions import load_price_panel
        px = load_price_panel(ROOT / "data/market_database/master_history.csv.gz",
                              ROOT / "data/market_database/history", start="2016-01-01", integrity=False)
        print(scan(px).to_string(index=False))
    if a.list or not (a.repair or a.drop or a.relabel or a.scan):
        print(load_quarantine().to_string(index=False))


if __name__ == "__main__":
    main()
