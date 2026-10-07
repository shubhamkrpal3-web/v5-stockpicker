"""
security_master.py
==================
Security identity and life-cycle events for NSE equities, from public records.

1. Symbol continuity
   data/market_database/derived/symbol_changes.csv is a dated snapshot of NSE's
   official symbol-change file. stitch() relabels, in memory, the rows of an old
   symbol dated before its change to the security's current symbol, so a
   renamed company keeps one continuous history (TATAMOTORS -> TMPV,
   KPIT -> BSOFT, GOLDSHARE -> GOLDBETA ...). NSE files corporate actions under
   the current symbol, so stitched histories also line up with those records.

2. Life-cycle events
   data/market_database/derived/security_events.csv records, per event, what a
   holder actually received and when it became tradable:

     TYPE              what a holder of SYMBOL receives
     merger_stock      RATIO_NUM/RATIO_DEN shares of SUCCESSOR per share
     merger_cash       CASH_PER_SHARE in cash (paid on CASH_PAY_DATE)
     delisting_cash    CASH_PER_SHARE in cash (paid on CASH_PAY_DATE). For an exit
                       offer: after LAST_TRADE_DATE the shares are locked (not
                       tradable, awaiting tender) until TENDER_DATE; from TENDER_DATE
                       the cash is a receivable; it is spendable from CASH_PAY_DATE.
                       TENDER_DATE_BASIS / PAY_DATE_BASIS label each date as verified
                       or as a modelling assumption.
     cash_dividend     keeps SYMBOL (still tradable), plus CASH_PER_SHARE gross in cash
                       for holders before EX_DATE, paid on CASH_PAY_DATE
     demerger          keeps SYMBOL, plus RATIO_NUM/RATIO_DEN shares of SUCCESSOR
     series_change     same shares, traded in another series (prices in the supplement)
     suspended         nothing tradable; position stays stale
     unresolved        no verified record; position stays stale

   Dates: LAST_TRADE_DATE (last session the old security traded), RECORD_DATE,
   ALLOT_DATE, TRADABLE_DATE (first session the received shares could be sold).
   An entitlement is PENDING (not tradable, no stop, no sale) until TRADABLE_DATE.
   Cash is PENDING until CASH_PAY_DATE; when that is blank the cash stays pending
   (never spendable). PAY_DATE_BASIS says whether CASH_PAY_DATE is a verified
   payment record or a modelling assumption (and which terms it was derived from).
   FRACTION_TREATMENT is "pending" unless the scheme/broker treatment is documented.
   PRICE_FACTOR / FACTOR_TYPE give a price factor for chart and stop continuity
   only; a price-derived estimate never replaces the share entitlements. For a
   cash_dividend the factor is (previous close - dividend) / previous close
   (FACTOR_TYPE official_cash_distribution).

3. Supplementary prices
   data/market_database/derived/supplementary_prices.csv holds date-verified
   non-EQ rows (e.g. the BE series a spin-off trades in before moving to EQ),
   so "first actually tradable price" is a real print.
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DERIVED = ROOT / "data" / "market_database" / "derived"
SYMBOL_CHANGES = DERIVED / "symbol_changes.csv"
SYMBOL_CHANGES_CURATED = DERIVED / "symbol_changes_curated.csv"
SECURITY_EVENTS = DERIVED / "security_events.csv"
SUPPLEMENT = DERIVED / "supplementary_prices.csv"
NSE_SYMBOL_CHANGE_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"

EVENT_COLS = ["EVENT_ID", "SYMBOL", "TYPE", "SUCCESSOR", "RATIO_NUM", "RATIO_DEN", "CASH_PER_SHARE",
              "LAST_TRADE_DATE", "EX_DATE", "RECORD_DATE", "ALLOT_DATE", "TRADABLE_DATE", "TENDER_DATE",
              "TENDER_DATE_BASIS", "CASH_PAY_DATE", "PAY_DATE_BASIS", "FRACTION_TREATMENT", "PRICE_FACTOR", "FACTOR_TYPE",
              "FACTOR_BASIS", "STATUS", "SOURCES", "NOTES",
              # fractional entitlements: filled only once the sale proceeds are disclosed (net rupees per whole
              # successor share; 'ceil_rupee' if each holder's amount is rounded up) and the payment date is known
              "FRACTION_CASH_PER_SHARE", "FRACTION_ROUNDING", "FRACTION_PAY_DATE"]
TERMINAL = {"merger_stock", "merger_cash", "delisting_cash", "suspended", "unresolved"}


# =========================================================
# Symbol changes
# =========================================================
def refresh_symbol_changes() -> pd.DataFrame:
    sys.path.insert(0, str(ROOT))
    from research_system.data.nse import NSE
    t = NSE().text(NSE_SYMBOL_CHANGE_URL)
    if not t:
        raise RuntimeError("NSE symbol-change file unavailable")
    sc = pd.read_csv(io.StringIO(t), header=None, names=["NAME", "OLD", "NEW", "DATE"])
    for c in ("OLD", "NEW", "NAME"):
        sc[c] = sc[c].astype(str).str.strip()
    sc["OLD"], sc["NEW"] = sc["OLD"].str.upper(), sc["NEW"].str.upper()
    sc["EFFECTIVE_DATE"] = pd.to_datetime(sc["DATE"].astype(str).str.strip(), format="%d-%b-%Y",
                                          errors="coerce").dt.strftime("%Y%m%d")
    sc = sc.dropna(subset=["EFFECTIVE_DATE"])
    sc["SOURCE"] = NSE_SYMBOL_CHANGE_URL
    sc["FETCHED"] = date.today().isoformat()
    out = sc[["OLD", "NEW", "EFFECTIVE_DATE", "NAME", "SOURCE", "FETCHED"]].sort_values("EFFECTIVE_DATE")
    DERIVED.mkdir(parents=True, exist_ok=True)
    out.to_csv(SYMBOL_CHANGES, index=False)
    return out


def load_symbol_changes() -> pd.DataFrame:
    """NSE's symbol-change snapshot plus curated renames that NSE's file omits
    (each curated row cites the NSE 'Name and Symbol Change' notice)."""
    cols = ["OLD", "NEW", "EFFECTIVE_DATE", "NAME", "SOURCE", "FETCHED"]
    parts = [pd.read_csv(p, dtype={"EFFECTIVE_DATE": str, "OLD": str, "NEW": str})
             for p in (SYMBOL_CHANGES, SYMBOL_CHANGES_CURATED) if p.exists()]
    if not parts:
        return pd.DataFrame(columns=cols)
    out = pd.concat(parts, ignore_index=True).reindex(columns=cols)
    return out.drop_duplicates(["OLD", "NEW", "EFFECTIVE_DATE"], keep="first")


def _chain(sc: pd.DataFrame) -> dict:
    """OLD -> (final symbol, effective date of the OLD->NEW change)."""
    nxt = {r.OLD: (r.NEW, r.EFFECTIVE_DATE) for r in sc.sort_values("EFFECTIVE_DATE").itertuples()}
    out = {}
    for old, (new, eff) in nxt.items():
        seen, cur = {old}, new
        while cur in nxt and cur not in seen and nxt[cur][1] >= eff:
            seen.add(cur)
            cur = nxt[cur][0]
        out[old] = (cur, eff)
    return out


def stitch(df: pd.DataFrame, sc: pd.DataFrame | None = None, verbose: bool = False) -> pd.DataFrame:
    """Relabel pre-change rows of renamed symbols to their current symbol.

    A mapping is skipped (and reported) if the target symbol already has rows
    on the same dates as the old symbol: that would mean two different
    securities, not a rename."""
    sc = load_symbol_changes() if sc is None else sc
    if sc.empty or df.empty:
        return df
    out = df.copy()
    if "ORIG_SYMBOL" not in out.columns:
        out["ORIG_SYMBOL"] = out["SYMBOL"]
    dkey = out["DATE"].astype(str)
    chain = _chain(sc)
    present = set(out["SYMBOL"].unique())
    skipped = []
    for old, (final, eff) in chain.items():
        if old not in present or old == final:
            continue
        m = (out["SYMBOL"] == old) & (dkey < eff)
        if not m.any():
            continue
        clash = set(dkey[m]) & set(dkey[out["SYMBOL"] == final])
        if clash:
            skipped.append((old, final, len(clash)))
            continue
        out.loc[m, "SYMBOL"] = final
    if verbose and skipped:
        print(f"[security_master] {len(skipped)} renames skipped (overlapping histories): {skipped[:5]}")
    return out


def canonical(symbol: str, sc: pd.DataFrame | None = None) -> str:
    """Current symbol of the security that traded as `symbol` (follows rename chains)."""
    sc = load_symbol_changes() if sc is None else sc
    ch = _chain(sc).get(symbol.upper())
    return ch[0] if ch else symbol.upper()


# =========================================================
# Life-cycle events
# =========================================================
def load_events(statuses: tuple | None = None) -> pd.DataFrame:
    if not SECURITY_EVENTS.exists():
        return pd.DataFrame(columns=EVENT_COLS)
    ev = pd.read_csv(SECURITY_EVENTS, dtype=str).fillna("")
    ev = ev.reindex(columns=list(dict.fromkeys(EVENT_COLS + list(ev.columns))), fill_value="")
    for c in ("RATIO_NUM", "RATIO_DEN", "CASH_PER_SHARE", "PRICE_FACTOR"):
        ev[c] = ev[c].map(_exact_float)      # exact parse, so a rewrite keeps every stored digit
    if statuses:
        ev = ev[ev["STATUS"].isin(statuses)]
    return ev.reset_index(drop=True)


def _exact_float(s) -> float:
    try:
        return float(s) if str(s).strip() else float("nan")
    except ValueError:
        return float("nan")


def ratio(e) -> float | None:
    if pd.notna(e.RATIO_NUM) and pd.notna(e.RATIO_DEN) and e.RATIO_DEN:
        return float(e.RATIO_NUM) / float(e.RATIO_DEN)
    return None


def upsert_events(rows: list[dict]) -> pd.DataFrame:
    DERIVED.mkdir(parents=True, exist_ok=True)
    cur = load_events()
    new = pd.DataFrame(rows).reindex(columns=EVENT_COLS)
    out = pd.concat([cur[~cur["EVENT_ID"].isin(new["EVENT_ID"])], new], ignore_index=True)
    # A verified record supersedes unverified rows describing the same ending of the same security.
    ver = out[(out["STATUS"] == "verified") & out["TYPE"].isin(["merger_stock", "merger_cash", "delisting_cash"])]
    dup = out["SYMBOL"].isin(ver["SYMBOL"]) & (out["STATUS"] != "verified") & \
        out["TYPE"].isin(["merger", "delisting", "unresolved", "suspended"])
    out = out[~dup]
    out = out.sort_values(["LAST_TRADE_DATE", "EX_DATE", "SYMBOL"]).reset_index(drop=True)
    out.to_csv(SECURITY_EVENTS, index=False)
    return out


# =========================================================
# Supplementary (non-EQ) prices
# =========================================================
def load_supplement(stitched: bool = True) -> pd.DataFrame:
    """Supplementary rows. The file keeps the symbol NSE used on each date (e.g. MAJESCO
    in 2021); with stitched=True rows are relabelled in memory to the current symbol
    (AURUM), exactly as the EQ history is."""
    if not SUPPLEMENT.exists():
        return pd.DataFrame(columns=["DATE", "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE",
                                     "VOLUME", "TRADED_VALUE", "SOURCE"])
    sup = pd.read_csv(SUPPLEMENT, dtype={"DATE": str, "SYMBOL": str, "SERIES": str})
    return stitch(sup) if stitched and len(sup) else sup


def add_supplement(symbol: str, start: str, end: str, sessions: list[str]) -> int:
    """Fetch date-verified rows for `symbol` in any non-EQ series between start
    and end (YYYYMMDD, inclusive) from NSE daily files."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from data_integrity import fetch_session
    rows = []
    for d in sessions:
        if start <= d <= end:
            x, url = fetch_session(d)
            x = x[(x["SYMBOL"] == symbol.upper()) & (x["SERIES"] != "EQ")]
            if len(x):
                rows.append(x.assign(SOURCE=url))
    if not rows:
        return 0
    return merge_supplement(pd.concat(rows, ignore_index=True))


def merge_supplement(new: pd.DataFrame) -> int:
    """Add date-verified non-EQ rows (NSE's symbol on that date, SOURCE = the NSE file) to the
    supplement; a later row for the same DATE/SYMBOL/SERIES replaces an earlier one."""
    if new.empty:
        return 0
    cols = ["DATE", "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE", "SOURCE"]
    new = new[cols].copy()
    if (new["SERIES"] == "EQ").any() or new["SOURCE"].isna().any():
        raise ValueError("supplement rows must be non-EQ and carry their NSE source link")
    # Work in text so rows already in the file are rewritten exactly as they were.
    num = lambda v: "" if pd.isna(v) else (str(int(float(v))) if float(v).is_integer() else repr(float(v)))
    for c in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"):
        new[c] = new[c].map(num)
    new = new.astype(str)
    cur = pd.read_csv(SUPPLEMENT, dtype=str, keep_default_na=False) if SUPPLEMENT.exists() else pd.DataFrame(columns=cols)
    out = pd.concat([cur, new], ignore_index=True).drop_duplicates(["DATE", "SYMBOL", "SERIES"], keep="last")
    DERIVED.mkdir(parents=True, exist_ok=True)
    out.sort_values(["SYMBOL", "DATE"]).to_csv(SUPPLEMENT, index=False)
    return len(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-symbols", action="store_true")
    ap.add_argument("--events", action="store_true")
    a = ap.parse_args()
    if a.refresh_symbols:
        sc = refresh_symbol_changes()
        print(f"{len(sc)} symbol changes -> {SYMBOL_CHANGES}")
    if a.events or not a.refresh_symbols:
        ev = load_events()
        print(ev.groupby(["TYPE", "STATUS"]).size().to_string() if len(ev) else "no events")


if __name__ == "__main__":
    main()
