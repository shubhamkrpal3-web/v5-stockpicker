"""
paper_trade_manager.py
======================
Automated paper-trade lifecycle. Replaces the manual Excel tracker.

Runs nightly after the bhavcopy download. All position rules live in
portfolio_accounting.py, shared with the backtest:

  * Real share counts at real prices. Official split/bonus ratios change share
    counts; renamed symbols carry over (security_master.py); price-derived
    factors never replace what a holder actually received.
  * Verified mergers convert a holding into its successor's shares (pending
    until the successor shares are tradable; fractions stay pending unless the
    treatment is documented). Verified demergers leave the parent holding and
    add a pending spin-off entitlement, sold at its first actually tradable
    open. Unexplained endings stay stale: untradeable, capital and slot occupied.
  * Stops are checked against each session's OPEN and LOW using the stop set at
    the previous close; a gap through the stop fills at the OPEN. The trailing
    stop is raised only after that check, by the exit rules in force on that
    session (PAPER_RULES): to the 2 Sep 2026 session, highest close - 3*ATR14 and
    the 22-session low, a 25-calendar-day time stop and no fees; from the 3 Sep
    2026 session, highest close - 4*ATR14, no time stop, fees on both legs. A stop
    is never lowered when the rule changes. --counterfactual-today-rules applies
    today's rules to every date instead: a labelled preview that never writes.
  * New Monday positions: quantity = SIZE_INR / the signal's close (fixed before
    the open), filled at the actual OPEN, with the signal's stop.
  * A holding touched by a verified life-cycle event that has not yet been
    applied is REPLAYED from its original entry and quantity through the event
    timeline, so stops never fire while shares were not tradable.
  * Sessions missed since the last run are processed in order.
  * Realised P&L is net of brokerage, STT, stamp duty, exchange, SEBI, GST and
    participation-scaled slippage (realistic_cost_model.py).
  * entry_cost_inr is the fee the paper ledger actually charged at entry, with its basis in
    entry_fee_source. Entries made before the cost model (3 Sep 2026) were charged no fee; that zero is
    recorded only where a verified record in data/ledger_evidence/historical_entry_fees.csv (code in force,
    the run that wrote the row) AND the ledger's own equity log for the entry day establish it. A zero
    without a stated basis is not accepted. Fees estimated with today's cost model for those entries live
    only in data/ledger_evidence/entry_fee_estimates.csv: estimates, never read into cash, cost or P&L.
  * A replay whose exit, settlement or change in slot use falls on a session the recorded history
    already covers is back-dated: every row it produced is flagged provisional and its cash is not
    verified (CASH_NOTE), because the later recorded entries, exits, cash and slots were produced without
    it. corrected_replay.py rebuilds that history session by session instead.
  * Dates are kept as text: blanks stay blank and YYYYMMDD never becomes YYYYMMDD.0.
  * An open holding becomes stale after 3 consecutive sessions without a valid print (open and close
    present and positive). The count is rebuilt on every run from the price data since the holding's
    last mark, so it carries across separate daily runs.
  * The supplementary price file (BE-series etc. bars for holdings that left the EQ series) must exist;
    without it the run stops before writing anything. A header-only file states that there are none.

Outputs:
  data/live_signals/positions.csv          - holdings ledger (open/pending/stale/closed/converted)
  data/live_signals/equity_log.csv         - daily NAV with stale/pending components
  data/live_signals/realized_trades.csv    - completed trade log

Usage:
  python paper_trade_manager.py --master ./data/market_database/master_history.csv \\
      --signal ./data/live_signals/daily_live_portfolio.csv \\
      --positions ./data/live_signals/positions.csv \\
      --equity-log ./data/live_signals/equity_log.csv \\
      --realized ./data/live_signals/realized_trades.csv --portfolio-inr 200000 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corporate_actions import adjust_long, integrity_layer, load_events, share_count_events  # noqa: E402
import security_master  # noqa: E402
from portfolio_accounting import CLOSED, OPEN, PENDING, STALE, Book, Holding  # noqa: E402

try:
    from realistic_cost_model import cost_of_trade
except Exception:  # pragma: no cover
    def cost_of_trade(notional_inr, side, adv_inr=None, custom_slippage_bps=None):
        return {"total_inr": notional_inr * 0.0030, "total_pct": 0.0030}


POSITIONS_COLS = [
    "symbol", "signal_date", "entry_date", "entry_price", "qty",
    "size_inr", "initial_stop", "current_stop", "highest_close",
    "atr_at_entry", "status", "exit_date", "exit_price", "exit_reason",
    "realized_pnl_inr", "realized_pct", "entry_cost_inr", "exit_cost_inr",
    "ca_applied", "hid", "kind", "tradable_from", "origin", "stale_reason",
    "last_mark", "last_mark_date", "cost_basis", "cash_due", "cash_date", "notes",
    # cost_basis is written only when the whole cost is recorded. When part of it was never recorded
    # (e.g. an entry cost from before the cost model), cost_basis stays blank, cost_known_part holds the
    # recorded part and cost_note says what is missing - a blank is never turned into a zero.
    "cost_known_part", "cost_note",
    # entry_fee_source: the basis of entry_cost_inr (charged at entry, or a historical zero and its evidence).
    # provisional: why the row's stops / exit are not final (a replay under undecided stop rules).
    "entry_fee_source", "provisional",
]
REALIZED_COLS = [
    "symbol", "entry_date", "entry_price", "qty", "exit_date",
    "exit_price", "exit_reason", "days_held", "gross_pnl_inr", "cost_inr",
    "pnl_inr", "pnl_pct", "r_multiple", "cost_note", "provisional",
]
TEXT_COLS = ["symbol", "signal_date", "entry_date", "status", "exit_date", "exit_reason", "ca_applied",
             "hid", "kind", "tradable_from", "origin", "stale_reason", "last_mark_date", "cash_date", "notes",
             "cost_note", "entry_fee_source", "provisional"]
ACTIVE = ("open", "pending", "stale")
FEE_EVIDENCE = Path(__file__).resolve().parent.parent / "data" / "ledger_evidence" / "historical_entry_fees.csv"
FEE_CHARGED = "charged by the paper ledger at entry (realistic_cost_model)"
# Exit rules and fees the paper ledger applied, by session date (git history of this file and of
# .github/workflows/daily_refresh.yml):
#   sessions to 2 Sep 2026  (238801a, 1d83c35, f800a30 with --max-holding-days 25): trailing stop = highest
#       close - 3 x ATR14, also raised to the lowest LOW of the last 22 sessions; time stop at the close once 25
#       calendar days have passed since entry; no fees
#   sessions from 3 Sep 2026 (a007948, e3f8a71 with --max-holding-days 0): trailing stop = highest close
#       - 4 x ATR14; no time stop; realistic_cost_model fees on both legs
# (from_date, chandelier multiple, rolling-low sessions, calendar-day time stop) - see portfolio_accounting.Book
PAPER_RULES = (("00000000", 3.0, 22, 25), ("20260903", 4.0, None, None))
COST_MODEL_FROM = "20260903"
TODAY_RULES = (("00000000", 4.0, None, None),)        # counterfactual only: today's rules on every date
COUNTERFACTUAL_LABEL = ("COUNTERFACTUAL: today's exit rules and fees applied to earlier dates - not what the paper "
                        "ledger did; never written to a ledger")
FEE_NONE_BEFORE_MODEL = "no fee: the paper ledger charged no fees before 3 Sep 2026 (rule in force on the entry date)"
DATE_COLS = ("signal_date", "entry_date", "exit_date", "tradable_from", "last_mark_date", "cash_date")


class SupplementMissing(RuntimeError):
    """The supplementary price file is missing: holdings that trade outside the EQ series would look unpriced."""


def _ymd(x) -> str:
    """Any date-like value -> YYYYMMDD ('' when missing)."""
    if x is None or (isinstance(x, float) and np.isnan(x)) or str(x).strip() in ("", "nan", "None"):
        return ""
    s = str(x).strip().split(".")[0]
    if len(s) == 8 and s.isdigit():
        return s
    return pd.Timestamp(s).strftime("%Y%m%d")


def _iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else ""


def _num(x, default=0.0) -> float:
    v = pd.to_numeric(x, errors="coerce")
    return float(v) if pd.notna(v) else default


def _text(x) -> str:
    return x.strip() if isinstance(x, str) and x.strip().lower() not in ("", "nan", "none") else ""


# =========================================================
# Loading
# =========================================================
def _date_text(v):
    """A date cell as text: blank stays blank (NaN); 'YYYYMMDD.0' left by an earlier float round trip -> 'YYYYMMDD'."""
    if not isinstance(v, str) or not v.strip() or v.strip().lower() in ("nan", "none"):
        return np.nan
    v = v.strip()
    head, _, tail = v.partition(".")
    return head if len(head) == 8 and head.isdigit() and tail.strip("0") == "" and tail != "" else v


def _load_positions(path: Path) -> pd.DataFrame:
    if path.exists():
        df = pd.read_csv(path, dtype={c: str for c in TEXT_COLS})     # dates and ids stay text
        for c in POSITIONS_COLS:
            if c not in df.columns:
                df[c] = None
        df = df[POSITIONS_COLS]
        for c in TEXT_COLS:
            df[c] = df[c].astype(object)
        for c in DATE_COLS:
            df[c] = df[c].map(_date_text).astype(object)
        return df
    return pd.DataFrame(columns=POSITIONS_COLS)


def _load_realized(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame(columns=REALIZED_COLS)


def _load_market(master: Path, since: str, ca_path: Path, low_lookbacks: tuple = (22,)):
    """Raw bars (integrity layer + rename stitching + supplementary series), and - all in each session's
    as-traded (raw) prices - ATR14 and the lowest LOW of the last n sessions for each n in low_lookbacks, from
    `since` (YYYYMMDD) onwards; plus EQ traded value by date. ATR and the rolling low are computed on prices
    adjusted for the corporate actions file (so a split inside the window does not distort them) and turned
    back into the session's raw prices with that session's factor - the basis of the bars stops are checked
    against. Returns (raw, atr, lows {n: series}, traded_value, sessions)."""
    df = pd.read_csv(master, low_memory=False)
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.upper() == "EQ"]
    df["DATE"] = df["DATE"].astype(str)
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    for c in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "TRADED_VALUE"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["DATE"] >= since]
    df = integrity_layer(df.drop_duplicates(["DATE", "SYMBOL"], keep="last"))
    df["DATE_DT"] = pd.to_datetime(df["DATE"], format="%Y%m%d")
    raw = df.set_index(["SYMBOL", "DATE"])[["OPEN", "HIGH", "LOW", "CLOSE"]].sort_index()
    if not security_master.SUPPLEMENT.exists():
        raise SupplementMissing(
            f"supplementary price file not found: {security_master.SUPPLEMENT}. It carries the bars of holdings that "
            "trade outside the EQ series; without it they would look unpriced (missed prints, wrong stops). Restore "
            "it, or create it with only its header row if there are deliberately no supplementary prices. Nothing "
            "was written.")
    sup = security_master.load_supplement()
    extra = pd.DataFrame(columns=["SYMBOL", "DATE", "OPEN", "HIGH", "LOW", "CLOSE"])
    if len(sup):
        sup = sup[sup["DATE"] >= since].drop_duplicates(["DATE", "SYMBOL"], keep="first")
        sup = sup.set_index(["SYMBOL", "DATE"])[["OPEN", "HIGH", "LOW", "CLOSE"]]
        extra = sup[~sup.index.isin(raw.index)]
        raw = pd.concat([raw, extra]).sort_index()
        extra = extra.reset_index()
    # The stop's ATR uses the same bars as execution and the trailing high: EQ rows, plus
    # supplement (e.g. BE-series) rows where EQ is missing, adjusted with the same factors.
    ex = pd.concat([df.assign(_SUP=False), extra.assign(_SUP=True)], ignore_index=True)
    ex["SYMBOL"] = ex["SYMBOL"].astype(str)          # one key dtype for the corporate-action join, rows or not
    ex["DATE_DT"] = pd.to_datetime(ex["DATE"], format="%Y%m%d")
    adj = adjust_long(ex, load_events(ca_path)) if ca_path.exists() else ex.assign(ADJ_FACTOR=1.0)
    adj = adj.sort_values(["SYMBOL", "DATE"])
    g = adj.groupby("SYMBOL", sort=False)
    prev = g["CLOSE"].shift(1)
    tr = np.maximum(adj["HIGH"] - adj["LOW"], np.maximum((adj["HIGH"] - prev).abs(), (adj["LOW"] - prev).abs()))
    adj["ATR_ADJ"] = tr.groupby(adj["SYMBOL"]).transform(lambda x: x.rolling(14, min_periods=10).mean())
    adj["ATR_RAW"] = adj["ATR_ADJ"] / adj["ADJ_FACTOR"]
    atr = adj.set_index(["SYMBOL", "DATE"])["ATR_RAW"]
    lows = {}
    for n in low_lookbacks:
        lo = adj.groupby("SYMBOL", sort=False)["LOW"].transform(lambda x: x.rolling(n, min_periods=1).min())
        lows[n] = (lo / adj["ADJ_FACTOR"]).set_axis(pd.MultiIndex.from_arrays([adj["SYMBOL"], adj["DATE"]]))
    eq_adj = adj[~adj["_SUP"].astype(bool)]
    tv = (eq_adj.set_index(["SYMBOL", "DATE"])["TRADED_VALUE"].sort_index() if "TRADED_VALUE" in eq_adj
          else pd.Series(dtype=float))
    sessions = sorted(df["DATE"].unique())
    return raw, atr, lows, tv, sessions


# =========================================================
# Ledger rows <-> holdings
# =========================================================
def _load_fee_evidence(path) -> pd.DataFrame:
    """Verified historical entry-fee records (data/ledger_evidence/historical_entry_fees.csv)."""
    p = Path(path) if path else None
    if p is None or not p.exists():
        return pd.DataFrame()
    ev = pd.read_csv(p, dtype=str).fillna("")
    return ev[ev["STATUS"].str.strip().str.lower() == "verified"] if "STATUS" in ev.columns else ev.iloc[0:0]


def _entry_fee_missing(r) -> bool:
    """An original entry whose fee was never written to its row."""
    origin = _text(getattr(r, "origin", "")) or "entry"
    return origin == "entry" and not np.isnan(_num(r.size_inr, np.nan)) and np.isnan(_num(r.entry_cost_inr, np.nan))


def _fee_from_evidence(r, ev: pd.DataFrame | None, eq_log: pd.DataFrame | None) -> tuple[float | None, str]:
    """The historical entry fee of an original entry whose fee was never written, when BOTH a verified evidence
    record (the code in force and the run that wrote the row) and the ledger's equity log for the entry day
    establish it. Only a zero fee can be established: the entry day's logged cash must be exactly starting
    capital - INVESTED + REALIZED_PNL, i.e. that run's cash had no fee term (the ledger before the cost model).
    Returns (fee, source), or (None, why not) - (None, '') when there is no evidence record for the row."""
    if ev is None or ev.empty or not _entry_fee_missing(r):
        return None, ""
    ed = _ymd(r.entry_date)
    m = ev[(ev["SYMBOL"].str.strip().str.upper() == str(r.symbol).strip().upper()) & (ev["ENTRY_DATE"].map(_ymd) == ed)]
    if m.empty:
        return None, ""
    e = m.iloc[0]
    qty, px, size = _num(r.qty, np.nan), _num(r.entry_price, np.nan), _num(r.size_inr, np.nan)
    if not (abs(qty - _num(e["QTY"], np.nan)) < 1e-9 and abs(px - _num(e["ENTRY_PRICE"], np.nan)) < 0.005
            and abs(size - _num(e["NOTIONAL_INR"], np.nan)) < 0.005):
        return None, (f"the evidence record ({e['QTY']} @ {e['ENTRY_PRICE']} = {e['NOTIONAL_INR']}) does not match "
                      f"the row ({qty:g} @ {px:g} = {size:g})")
    if _num(e["HISTORICAL_ENTRY_FEE_INR"], np.nan) != 0:
        return None, "only a historical fee of zero can be established from the run record"
    if eq_log is None or eq_log.empty or not {"DATE", "CASH", "INVESTED", "REALIZED_PNL"} <= set(eq_log.columns):
        return None, "no equity log to check the entry day's run record against"
    el = eq_log.assign(_d=eq_log["DATE"].map(_ymd)).sort_values("_d")
    day, before = el[el["_d"] == ed], el[el["_d"] < ed]
    if day.empty or before.empty:
        return None, f"the equity log has no row for the entry day {_iso(ed)} (or none before it)"
    first, d = el.iloc[0], day.iloc[-1]
    cash, capital = _num(d["CASH"], np.nan), _num(first["CASH"], np.nan)
    implied = capital - _num(d["INVESTED"], np.nan) + _num(d["REALIZED_PNL"], np.nan)
    if not abs(implied - cash) <= 0.01:
        return None, (f"the equity log on {_iso(ed)} (CASH Rs{cash:,.2f}) is not starting capital - INVESTED + "
                      f"REALIZED_PNL (Rs{implied:,.2f}): that run's cash may include fees")
    change = cash - _num(before.iloc[-1]["CASH"], np.nan)
    stated = _num(e.get("ENTRY_DAY_CASH_CHANGE_INR"), np.nan)
    if stated == stated and abs(stated - change) > 0.01:
        return None, f"the evidence's entry-day cash change ({stated:,.2f}) differs from the equity log ({change:,.2f})"
    return 0.0, (f"no fee charged by the paper ledger at entry: {e['CODE_IN_FORCE']}; row written by ledger commit "
                 f"{e['LEDGER_COMMIT']} (run {e['RUN_ID']}); equity log {_iso(ed)}: CASH Rs{cash:,.2f} = capital "
                 f"- INVESTED + REALIZED_PNL, day's cash change Rs{change:,.2f}")


def _cost_from_row(r, established: tuple = (None, "")) -> tuple[float, str]:
    """(cost basis, note). A blank is never read as zero: when part of the cost was never recorded, the
    recorded part is returned with a note saying what is missing. The running cash uses the recorded
    part (as the ledger always has); the note keeps the row - and the cash - explicitly unverified.
    An entry fee of 0 counts only with its stated basis (entry_fee_source); `established` is
    _fee_from_evidence's result for an original entry whose fee was never written."""
    note = r.cost_note if isinstance(r.cost_note, str) and r.cost_note.strip() else ""
    size, ec = _num(r.size_inr, np.nan), _num(r.entry_cost_inr, np.nan)
    entered = _iso(_ymd(r.entry_date))
    if ec == 0 and not _text(getattr(r, "entry_fee_source", "")):
        known = size if not np.isnan(size) else _num(r.cost_known_part, _num(r.cost_basis, 0.0))
        return known, f"entry fee recorded as 0 with no stated basis (entered {entered}); not accepted as a known cost"
    if _entry_fee_missing(r):
        fee, why = established
        if fee is not None:
            return size + fee, ""
        cb = _num(r.cost_basis, np.nan)
        if not np.isnan(cb) and not note:          # opened by this ledger before fees had their own column
            return cb, ""
        return size, (f"entry cost not recorded (entered {entered}); the cost basis excludes it"
                      + (f"; evidence not applied: {why}" if why else ""))
    cb = _num(r.cost_basis, np.nan)
    if not np.isnan(cb) and not note:
        return cb, ""
    known = _num(r.cost_known_part, np.nan)
    if not np.isnan(known):
        return known, note or "cost not fully recorded"
    if not np.isnan(cb):
        return cb, note
    if not np.isnan(size) and not np.isnan(ec):
        return size + ec, note
    if not np.isnan(size):
        return size, note or f"entry cost not recorded (entered {entered}); the cost basis excludes it"
    return 0.0, note or "cost not recorded"


def _holding_from_row(r, idx: int, sc, established: tuple = (None, "")) -> Holding:
    status = str(r.status)
    kind = r.kind if isinstance(r.kind, str) and r.kind else "shares"
    cost, cost_note = _cost_from_row(r, established)
    h = Holding(hid=str(r.hid) if isinstance(r.hid, str) and r.hid else f"L{idx}",
                symbol=security_master.canonical(str(r.symbol), sc), qty=_num(r.qty),
                state={"open": OPEN, "pending": PENDING, "stale": STALE}[status],
                entry_date=_ymd(r.entry_date), entry_price=_num(r.entry_price), cost_basis=cost,
                stop=_num(r.current_stop), stop0=_num(r.initial_stop), hi=_num(r.highest_close, _num(r.entry_price)),
                last_mark=_num(r.last_mark, np.nan), last_mark_date=_ymd(r.last_mark_date), kind=kind,
                tradable_from=_ymd(r.tradable_from), origin=r.origin if isinstance(r.origin, str) and r.origin else "entry",
                on_tradable="sell" if str(r.origin).startswith("demerger:") else "hold",
                stale_reason=r.stale_reason if isinstance(r.stale_reason, str) else "",
                cash_due=_num(r.cash_due), cash_date=_ymd(r.cash_date), cost_note=cost_note,
                provisional=_text(getattr(r, "provisional", "")))
    h.applied = [k for k in str(r.ca_applied or "").split(";") if k and k not in ("nan", "None")]
    # legacy keys from the earlier ledger format ("YYYYMMDD:kind") -> "share:YYYYMMDD:kind"
    h.applied = [k if ":" in k and k.split(":")[0] in ("share", "sec", "unverified") else f"share:{k}" for k in h.applied]
    h.notes = [str(r.notes)] if isinstance(r.notes, str) and r.notes else []
    return h


def _row_from_holding(h: Holding, base: dict | None = None) -> dict:
    row = dict(base or {})
    status = {OPEN: "open", PENDING: "pending", STALE: "stale"}.get(h.state, "closed")
    if h.state == CLOSED and any("converted under" in n for n in h.notes[-2:]):
        status = "converted"
    row.update({"symbol": h.symbol, "entry_date": _iso(h.entry_date), "entry_price": round(h.entry_price, 4),
                "qty": round(h.qty, 6), "current_stop": round(h.stop, 4), "initial_stop": round(h.stop0, 4),
                "highest_close": round(h.hi, 4), "status": status, "ca_applied": ";".join(h.applied),
                "hid": h.hid, "kind": h.kind, "tradable_from": h.tradable_from, "origin": h.origin,
                "stale_reason": h.stale_reason, "last_mark": round(h.last_mark, 4) if h.last_mark == h.last_mark else "",
                "last_mark_date": h.last_mark_date,
                "cost_basis": "" if h.cost_note else round(h.cost_basis, 2),
                "cost_known_part": round(h.cost_basis, 2) if h.cost_note else "", "cost_note": h.cost_note,
                "cash_due": round(h.cash_due, 2) if h.kind == "cash" or (h.kind == "fraction" and h.cash_due > 0) else "",
                "cash_date": h.cash_date if h.kind in ("cash", "fraction") else "",
                "provisional": h.provisional,
                "notes": " | ".join(h.notes[-6:])})
    if status == "converted":
        row["exit_reason"] = "CONVERTED"
    return row


def _original(h: Holding, row, share_ev: pd.DataFrame) -> Holding:
    """The holding as it was at entry: undo any share-count events already applied."""
    f = 1.0
    for k in h.applied:
        if k.startswith("share:"):
            _, ex, kind = k.split(":")
            m = share_ev[(share_ev["SYMBOL"] == h.symbol) & (share_ev["EX_DATE"] == ex) & (share_ev["KIND"] == kind)]
            if len(m):
                f *= float(m["FACTOR"].iloc[0])
    o = Holding(hid=h.hid, symbol=h.symbol, qty=h.qty * f, entry_date=h.entry_date, entry_price=h.entry_price / f,
                cost_basis=h.cost_basis, stop=_num(row.initial_stop) / f, stop0=_num(row.initial_stop) / f,
                hi=h.entry_price / f, last_mark=h.entry_price / f, last_mark_date=h.entry_date, origin=h.origin,
                cost_note=h.cost_note, provisional=h.provisional)
    o.notes = [f"{h.entry_date}: replayed from original entry ({h.qty * f:g} sh @ {h.entry_price / f:.4f})"]
    return o


def _fill_last_mark(h: Holding, raw: pd.DataFrame, mark_cutoff: str) -> None:
    """A reloaded row without a last price gets the last traded close on or before the last
    processed session. Cash entitlements are valued at cash_due and need none; a symbol with no
    prices in the loaded window (e.g. delisted long ago) falls back to its entry price."""
    if h.last_mark == h.last_mark or h.kind == "cash":
        return
    syms = raw.index.get_level_values(0)
    px = raw.loc[h.symbol]["CLOSE"] if h.symbol in syms else None
    px = px[px.index <= mark_cutoff].dropna() if px is not None else None
    if px is not None and len(px):
        h.last_mark, h.last_mark_date = float(px.iloc[-1]), px.index[-1]
    else:
        h.last_mark, h.last_mark_date = h.entry_price, h.entry_date


def _needs_replay(h: Holding, sec: pd.DataFrame) -> bool:
    if h.state not in (OPEN, STALE) or h.kind != "shares" or h.origin != "entry":
        return False
    ev = sec[(sec["SYMBOL"] == h.symbol) & (sec["STATUS"] == "verified")]
    for e in ev.itertuples(index=False):
        when = e.EX_DATE if e.TYPE in ("demerger", "cash_dividend") else e.LAST_TRADE_DATE
        key = f"sec:{e.EVENT_ID}"
        started = any(k == key or k.startswith(key + ":") for k in h.applied)   # e.g. an exit offer already locked
        if when and when >= h.entry_date and not started:
            return True
    return False


def _trade_to_realized(t: dict) -> dict:
    gross = t["qty"] * (t["exit"] - t["entry"]) if t["exit"] == t["exit"] else t["pnl"]
    return {"symbol": t["symbol"], "entry_date": _iso(t["entry_date"]), "entry_price": round(t["entry"], 4),
            "qty": t["qty"], "exit_date": _iso(t["exit_date"]),
            "exit_price": round(t["exit"], 4) if t["exit"] == t["exit"] else "",
            "exit_reason": t["reason"], "days_held": t["days"], "gross_pnl_inr": round(gross, 2),
            "cost_inr": round(gross - t["pnl"], 2), "pnl_inr": round(t["pnl"], 2),
            "pnl_pct": round(t["pnl"] / t["cost_basis"], 4) if t["cost_basis"] else "",
            "r_multiple": round(t["r_mult"], 2) if t["r_mult"] == t["r_mult"] else "",
            "cost_note": t.get("cost_note", ""), "provisional": t.get("provisional", "")}


def _replay_backdated(rb: Book, cutoff: str, last_run: str) -> str:
    """Why a replay's result cannot simply be added to the recorded history ('' if it can): a cash movement
    (exit, settlement) or a change in the lot's slot use dated before `cutoff`, the first session this run
    records. The recorded sessions from that date to `last_run` were produced without it."""
    early = sorted((t["exit_date"], t["symbol"], t["reason"]) for t in rb.trades if t["exit_date"] < cutoff)
    if early:
        d, sym, reason = early[0]
        what = f"{sym} {reason}"
    else:
        slots = sum(1 for h in rb.active() if h.kind != "fraction" and not rb.is_dividend_receivable(h))
        changes = sorted(e["date"] for e in rb.log if e["date"] < cutoff and "replayed from" not in e["event"])
        if slots == 1 or not changes:
            return ""
        d, what = changes[0], f"slot use ({slots} slots instead of 1)"
    return (f"back-dated by a replay: {what} on {_iso(d)} falls before sessions already recorded (to "
            f"{_iso(last_run)}); the later entries, exits, cash and slots need a corrected replay from {_iso(d)}")


def _mark_replay(rb: Book, why: str) -> None:
    """Flag every row a back-dated replay produced: the rows it closed (their cash is not verified) and the
    holdings it leaves active (whatever they later become carries the flag)."""
    for t in rb.trades:
        t["provisional"] = t.get("provisional") or why
    for h in rb.holdings:
        h.provisional = h.provisional or why


# =========================================================
# Main
# =========================================================
def run(args, market=None) -> dict:
    """One ledger run. `market(since)` may supply _load_market's result (corrected_replay.py cuts one load
    to each session); otherwise the price file is read."""
    counterfactual = bool(getattr(args, "counterfactual", False))
    rules = TODAY_RULES if counterfactual else PAPER_RULES
    positions = _load_positions(Path(args.positions))
    realized_log = _load_realized(Path(args.realized))
    sc = security_master.load_symbol_changes()
    sec = security_master.load_events()
    ca_path = Path(args.corporate_actions)
    ev = load_events(ca_path) if ca_path.exists() else pd.DataFrame(columns=["SYMBOL", "EX_DATE", "KIND", "FACTOR", "FACTOR_TYPE"])
    share_ev = share_count_events(ev) if len(ev) else ev
    other_ev = ev[ev["FACTOR_TYPE"] != "official_ratio"] if len(ev) else ev

    active_rows = positions[positions["status"].isin(ACTIVE)]
    first = min([_ymd(x) for x in active_rows["entry_date"]] + ["99999999"])
    since = (pd.Timestamp(min(first, pd.Timestamp.today().strftime("%Y%m%d"))) - pd.Timedelta(days=45)).strftime("%Y%m%d")
    lookbacks = tuple(sorted({r[2] for r in rules if r[2]}))
    raw, atr_s, lows, tv, sessions = (market(since) if market is not None
                                      else _load_market(Path(args.master), since, ca_path, lookbacks))
    today = sessions[-1]
    tv = tv[tv.index.get_level_values(1) <= today] if len(tv) else tv
    tv20 = tv.groupby(level=0).apply(lambda x: x.tail(20).mean()) if len(tv) else pd.Series(dtype=float)

    def bar(sym, d):
        try:
            o, h, l, c = raw.loc[(sym, d)]
        except KeyError:
            return None
        ok = o == o and c == c and o > 0 and c > 0            # a valid print: open and close present and positive
        return {"open": float(o), "high": float(h), "low": float(l), "close": float(c)} if ok else None

    def atr(sym, d):
        v = atr_s.get((sym, d))
        return float(v) if v is not None and v == v and v > 0 else None

    def low(sym, d, n):
        s_ = lows.get(n)
        v = s_.get((sym, d)) if s_ is not None else None
        return float(v) if v is not None and v == v and v > 0 else None

    def cost(n, side, sym, d):
        if not counterfactual and d < COST_MODEL_FROM:
            return 0.0                    # the paper ledger charged no fees before the cost model
        adv = tv20.get(sym)
        return cost_of_trade(n, side, float(adv) if adv == adv and adv else None)["total_inr"]

    def new_book(cash):
        b = Book(cash, bar, atr, cost, share_ev, other_ev, sec,
                 time_stop=(args.max_holding_days or None), stop_rules=rules, low=low)
        b.tag = today + "-"
        return b

    # Cash implied by the ledger: capital - cost of everything still held + realised P&L. Only recorded
    # amounts are summed; whatever was never recorded is listed in the equity log's CASH_NOTE (never read
    # as zero), and the risk view will not treat the cash as verified while that note is non-empty.
    closed = positions[positions["status"] == "closed"]
    realised = pd.to_numeric(closed["realized_pnl_inr"], errors="coerce").sum()      # recorded values only
    eq_path = Path(args.equity_log)
    eq_log = pd.read_csv(eq_path) if eq_path.exists() else None
    last_run = _ymd(eq_log["DATE"].max()) if eq_log is not None and len(eq_log) else ""
    # holdings are marked as of the last processed session
    mark_cutoff = last_run if last_run else (sessions[-2] if len(sessions) > 1 else today)
    # historical entry fees established by evidence + the equity log (written to the rows below)
    fee_ev = _load_fee_evidence(getattr(args, "fee_evidence", FEE_EVIDENCE))
    fee_rec, holdings = {}, []
    for i, r in zip(active_rows.index, active_rows.itertuples(index=False)):
        est = _fee_from_evidence(r, fee_ev, eq_log)
        h = _holding_from_row(r, i, sc, est)
        if est[0] is not None:
            fee_rec[h.hid] = {"entry_cost_inr": est[0], "entry_fee_source": est[1]}
        holdings.append((i, r, h))
    for i, r, h in holdings:
        _fill_last_mark(h, raw, mark_cutoff)
        # consecutive sessions without a valid print since the last mark, counted from the price data so the
        # count survives separate daily runs (Book.check_exits makes the holding stale at stale_after)
        if h.state in (OPEN, STALE) and h.kind == "shares" and h.last_mark_date:
            h.missing = sum(1 for s in sessions if h.last_mark_date < s <= mark_cutoff and bar(h.symbol, s) is None)
    cash = args.portfolio_inr - sum(h.cost_basis for _, _, h in holdings) + realised
    # Sessions not yet processed. If the last run already covered `today`, nothing is
    # re-processed (that would reuse same-day information); holdings are only reconciled and revalued.
    todo = [d for d in sessions if (last_run and d > last_run) or (not last_run and d == today)]
    boundary = todo[0] if todo else "99999999"

    book = new_book(cash)
    replays = []
    for i, r, h in holdings:
        if _needs_replay(h, sec):
            rb = new_book(0.0)
            o = _original(h, r, share_ev)
            o.notes.append(f"{o.entry_date}: " + (COUNTERFACTUAL_LABEL if counterfactual else
                                                    "exit rules and fees as in force on each date (PAPER_RULES)"))
            rb.holdings.append(o)
            # replay up to (not including) the first session the main loop processes below
            for d in [x for x in sessions if o.entry_date <= x < boundary]:
                if d > o.entry_date:
                    rb.apply_events(d); rb.settle_pending(d); rb.check_exits(d)
                rb.end_of_day(d)
            why = _replay_backdated(rb, todo[0] if todo else today, last_run)
            if why:
                _mark_replay(rb, why)
            book.cash += rb.cash
            book.holdings += rb.holdings
            book.trades += rb.trades
            book.log += rb.log
            replays.append((h.hid, h.symbol, rb))
        else:
            book.holdings.append(h)
    replayed = {hid for hid, _, _ in replays}

    for d in todo:
        book.apply_events(d)
        book.settle_pending(d)
        book.check_exits(d)
        if d == today:
            _enter(book, args, d, bar)
        book.end_of_day(d)
    comp = book.value(today)

    # ---- rebuild the ledger ----
    keep = positions[~positions["status"].isin(ACTIVE)].copy()
    base_rows = {h.hid: r._asdict() for _, r, h in holdings}
    rows = []
    for h in book.holdings:
        if h.hid in base_rows:
            base = base_rows[h.hid]
        elif h.origin == "entry":       # opened this run: notional and the fee charged, recorded separately
            notional = h.entry_price * h.qty
            base = {"signal_date": "", "size_inr": round(notional, 2),
                    "entry_cost_inr": round(h.cost_basis - notional, 2),
                    "entry_fee_source": FEE_CHARGED if counterfactual or h.entry_date >= COST_MODEL_FROM
                    else FEE_NONE_BEFORE_MODEL}
        else:
            base = {"signal_date": "", "size_inr": "" if h.cost_note else round(h.cost_basis, 2)}
        row = _row_from_holding(h, base)
        row.update(fee_rec.get(h.hid, {}))
        t = next((t for t in book.trades if t["hid"] == h.hid), None)
        if h.state == CLOSED and t:
            row.update({"exit_date": _iso(t["exit_date"]), "exit_price": round(t["exit"], 4) if t["exit"] == t["exit"] else "",
                        "exit_reason": t["reason"], "realized_pnl_inr": round(t["pnl"], 2),
                        "realized_pct": round(t["pnl"] / t["cost_basis"], 4) if t["cost_basis"] else ""})
        rows.append(row)
    # rows stay where they were in the file (new rows follow, in order), so a rerun rewrites it unchanged
    where = {h.hid: i for i, _, h in holdings}
    new = pd.DataFrame(rows)
    if len(new):
        new["_k"] = [where.get(r["hid"], len(positions) + j) for j, r in enumerate(rows)]
    new_positions = (pd.concat([keep.assign(_k=keep.index), new], ignore_index=True)
                     .sort_values("_k", kind="stable").drop(columns="_k").reset_index(drop=True)
                     .reindex(columns=POSITIONS_COLS))
    new_realized = pd.concat([realized_log, pd.DataFrame([_trade_to_realized(t) for t in book.trades])],
                             ignore_index=True) if book.trades else realized_log
    realised_total = pd.to_numeric(new_positions.loc[new_positions["status"] == "closed", "realized_pnl_inr"],
                                   errors="coerce").sum()                          # recorded values only
    # the logged cash is the ledger identity over the values as written (rounded to paise), so the next run
    # rebuilds exactly the same figure from the files
    cash_rec = args.portfolio_inr + realised_total - sum(
        _cost_from_row(r)[0] for r in new_positions[new_positions["status"].isin(ACTIVE)].itertuples(index=False))
    if abs(cash_rec - comp["cash"]) > 1.0:
        print(f"[WARN] cash from the written rows Rs{cash_rec:,.2f} differs from the book Rs{comp['cash']:,.2f}")
    other = comp["equity"] - comp["cash"]
    eq_row = {"DATE": _iso(today), "EQUITY": round(cash_rec + other, 2), "CASH": round(cash_rec, 2),
              "MTM_OPEN": round(comp["open_mtm"], 2),
              "INVESTED": round(sum(h.cost_basis for h in book.active() if h.state == OPEN), 2),
              "REALIZED_PNL": round(realised_total, 2), "N_OPEN": comp["n_open"],
              "PENDING_REF": round(comp["pending_ref"], 2), "STALE_MARK": round(comp["stale_mark"], 2),
              "N_PENDING": comp["n_pending"], "N_STALE": comp["n_stale"],
              "FRACTION_REF_UNCONFIRMED": round(comp["fraction_ref_unconfirmed"], 2),
              "EQUITY_STALE_AT_ZERO": round(cash_rec + other - comp["stale_mark"], 2),
              "CASH_NOTE": _cash_note(new_positions)}
    return {"positions": new_positions, "realized": new_realized, "eq_row": eq_row, "comp": comp,
            "replays": replays, "replayed": replayed, "book": book, "today": today, "rules": rules,
            "counterfactual": counterfactual}


def write_outputs(args, out: dict) -> None:
    """Write one run's positions, realised trades and equity-log row (replacing that date's row)."""
    if out.get("counterfactual"):
        raise RuntimeError("a counterfactual run is never written to a ledger")
    Path(args.positions).parent.mkdir(parents=True, exist_ok=True)
    out["positions"].to_csv(args.positions, index=False)
    out["realized"].to_csv(args.realized, index=False)
    eq_path = Path(args.equity_log)
    eq = pd.read_csv(eq_path) if eq_path.exists() else pd.DataFrame()
    row = dict(out["eq_row"])
    if len(eq):
        same = eq[eq["DATE"] == row["DATE"]]
        if len(same) and "SOURCE" in same.columns:      # a rerun of a labelled date keeps its label
            row.setdefault("SOURCE", same.iloc[-1]["SOURCE"])
        eq = eq[eq["DATE"] != row["DATE"]]
    eq = pd.concat([eq, pd.DataFrame([row])], ignore_index=True).sort_values("DATE")
    eq.to_csv(eq_path, index=False)


def _cash_note(pos: pd.DataFrame) -> str:
    """What the ledger's cash leaves out because it was never recorded ('' = nothing missing)."""
    status = pos["status"].astype(str)
    has = lambda v: isinstance(v, str) and v.strip() != ""
    notes = [f"{r.symbol}: {r.cost_note}" for r in pos[status.isin(ACTIVE)].itertuples(index=False) if has(r.cost_note)]
    for r in pos[status == "closed"].itertuples(index=False):
        if pd.isna(pd.to_numeric(r.realized_pnl_inr, errors="coerce")):
            notes.append(f"{r.symbol} closed {r.exit_date}: realised P&L not recorded")
        elif has(r.cost_note):
            notes.append(f"{r.symbol} closed {r.exit_date}: realised P&L excludes an unrecorded cost ({r.cost_note})")
        if has(getattr(r, "provisional", "")):
            notes.append(f"{r.symbol} closed {r.exit_date}: provisional exit ({r.provisional})")
    return "; ".join(notes)


def _enter(book: Book, args, d: str, bar) -> None:
    dow = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "any": -1}.get(args.entry_day, 0)
    if not (dow == -1 or pd.Timestamp(d).weekday() == dow):
        print(f"[INFO] not entry day ({pd.Timestamp(d):%A}); skipping new entries")
        return
    if not Path(args.signal).exists():
        return
    sig = pd.read_csv(args.signal)
    sig.columns = [c.strip().upper() for c in sig.columns]
    if sig.empty:
        return
    if "CONFIDENCE" in sig.columns:
        sig = sig.sort_values("CONFIDENCE", ascending=False)
    opened = 0
    for p in sig.itertuples(index=False):
        if book.slots_used() >= args.max_open_positions:
            print(f"[INFO] capacity reached ({book.slots_used()}/{args.max_open_positions}, stale and pending included)")
            break
        sym = str(p.SYMBOL).upper()
        if book.holds(sym):
            continue
        b = bar(sym, d)
        ref = _num(getattr(p, "CLOSE", np.nan), np.nan)
        if b is None or not ref or ref != ref:
            print(f"[WARN] {sym}: no print today or no signal close; skipped")
            continue
        qty = int(_num(p.SIZE_INR) / ref)                 # fixed from the signal close, before the open
        h = book.open_position(sym, d, qty, b["open"], None, stop=_num(p.STOP_PRICE))
        if h is not None:
            h.notes.append(f"{d}: signal {getattr(p, 'DATE', '')} qty {qty} from close {ref:.2f}")
            opened += 1
    print(f"[INFO] opened {opened} new positions at today's open")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", required=True)
    ap.add_argument("--signal", default="./data/live_signals/daily_live_portfolio.csv")
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--corporate-actions", default="./data/market_database/corporate_actions.csv")
    ap.add_argument("--portfolio-inr", type=float, default=200000.0)
    ap.add_argument("--entry-day", default="mon",
                    help="Day-of-week when new positions are opened. mon/tue/wed/thu/fri or 'any' for daily")
    ap.add_argument("--max-holding-days", type=int, default=0,
                    help="Time stop in sessions held. 0 = disabled (recommended).")
    ap.add_argument("--max-open-positions", type=int, default=15,
                    help="Capacity across open, pending and stale holdings.")
    ap.add_argument("--fee-evidence", default=str(FEE_EVIDENCE),
                    help="Verified historical entry-fee records (data/ledger_evidence/historical_entry_fees.csv).")
    ap.add_argument("--dry-run", action="store_true", help="Compute and print; write nothing.")
    ap.add_argument("--counterfactual-today-rules", dest="counterfactual", action="store_true",
                    help="Preview only: apply today's exit rules and fees to every date (never written).")
    args = ap.parse_args()
    if args.counterfactual:
        args.dry_run = True
        print(f"[INFO] {COUNTERFACTUAL_LABEL}")

    try:
        out = run(args)
    except SupplementMissing as e:
        print(f"[ERROR] {e}")
        sys.exit(2)
    for hid, sym, rb in out["replays"]:
        print(f"[INFO] replayed {sym} ({hid}) from its original entry through verified events:")
        for e in rb.log:
            print(f"         {e['date']} {e['symbol']}: {e['event']}")
    if out["book"].trades:
        print("[INFO] closed: " + ", ".join(f"{t['symbol']}({t['reason']})" for t in out["book"].trades))
    if not args.dry_run:
        write_outputs(args, out)
    e = out["eq_row"]
    label = " (COUNTERFACTUAL, nothing written)" if args.counterfactual else (" (dry run, nothing written)"
                                                                               if args.dry_run else "")
    print(f"\n=== {e['DATE']} summary{label} ===")
    print(f"  Open {e['N_OPEN']} | pending {e['N_PENDING']} | stale {e['N_STALE']}")
    print(f"  Cash Rs{e['CASH']:,.2f} | open MTM Rs{e['MTM_OPEN']:,.2f} | pending ref Rs{e['PENDING_REF']:,.2f} "
          f"| stale mark Rs{e['STALE_MARK']:,.2f}")
    print(f"  Equity Rs{e['EQUITY']:,.2f} (stale at zero: Rs{e['EQUITY_STALE_AT_ZERO']:,.2f}; "
          f"unconfirmed fractions Rs{e['FRACTION_REF_UNCONFIRMED']:,.2f} not included)")
    print(f"  Return vs Rs{args.portfolio_inr:,.0f}: {(e['EQUITY'] / args.portfolio_inr - 1) * 100:+.2f}%")


if __name__ == "__main__":
    main()
