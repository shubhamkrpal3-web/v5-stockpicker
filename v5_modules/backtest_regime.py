"""
backtest_regime.py
==================
STANDALONE RESEARCH BACKTEST — old vs fixed regime logic, 2023-2026.

Purpose
-------
The live regime detector compared the AVERAGE VOLATILITY OF INDIVIDUAL STOCKS
(~35-44% annualised) against a 22% threshold that actually means INDEX-level
volatility (~13-21%). That units mismatch made the "VIX" trigger impossible to
pass, so RISK_ON was unreachable and the system was permanently capped at 70%
gross exposure. This script quantifies what that cost — and, more importantly,
whether the fix genuinely helps across many market conditions rather than just
the recent rally we happened to notice it in.

SAFETY: this script is READ-ONLY with respect to live state. It never touches
positions.csv, equity_log.csv, realized_trades.csv, daily_regime.json or the
benchmark files. All output goes to a separate --output-dir (default ./backtests).

Method
------
* Point-in-time: factors use data up to the PREVIOUS close; entries fill at the
  next session's OPEN. Share quantities are fixed from the previous close
  (an order is sized before the open), sized from equity at the previous close.
* Weekly entries on Mondays (matching live behaviour), max 15 concurrent holdings.
  Stale and pending holdings occupy capacity.
* New positions (--entry-allocation, see allocate_entries): by default ('rank_order')
  each pick is sized with the formula of v5_signal_generator.size_positions
  (risk-parity, 10% cap, the top-N list renormalised so a full book fits
  equity x regime gross) and the picks are funded in rank order; the last one
  takes what is left of the regime budget and lower-ranked picks wait for a later
  week. 'pro_rata_leftover' reproduces the original rule, which scaled every pick
  down to share the leftover budget (tiny positions, one per free slot).
  Neither setting matches the paper ledger or the research risk engine yet: the
  capital base, candidate set, ordering and caps still differ.
  'shared' sizes the same ranked list with the shared allocator (allocator.py),
  the rules the research risk engine uses: loss budget over stop (2 ATR) + gap
  allowance + round-trip costs, position / liquidity / capital / cash caps, the
  regime-scaled gross cap, net and sector caps, slots and duplicates. It needs a
  configured policy (--risk-config), sectors (--sectors) and a gap allowance
  ([engines.swing] gap_allowance_atr); without them it opens nothing. Capital
  follows equity at the previous close and the per-trade loss budget keeps its
  ratio to capital.
* Initial stop at entry - 2*ATR(14) (ATR at the previous close), live from the
  entry fill; chandelier trail (highest close - 4*ATR). A stop applies from the
  session after it was set. A gap through the stop fills at the OPEN.
* Costs: cost_bps per side on traded notional.
* Signals use split/bonus back-adjusted prices (corporate_actions.py) after the
  derived-data layer (data_integrity.py quarantine, security_master.py rename
  stitching). Execution and valuation use REAL prices and share counts
  (portfolio_accounting.py): official split/bonus ratios change share counts;
  verified mergers/demergers create pending entitlements that are tradable only
  from their documented first tradable session; unexplained or unverified
  endings stay stale (untradeable, capital and slot occupied) and are reported
  with recovery scenarios instead of being sold.
* The Rs 20 price floor uses the raw (as-traded) previous close.
* Benchmark: an equal-weighted index of the liquid universe (index-style:
  adjusted returns), membership from the PREVIOUS session's 20-day turnover.
* Score: 62.5% momentum + 37.5% low-vol (breadth sleeve removed 2026-10-04).

Two variants are run over identical data:
    OLD   - vol trigger = mean of individual-stock 20d vol   (the bug)
    FIXED - vol trigger = 20d vol of the equal-weighted index, plus debounce

Usage
-----
    python backtest_regime.py \
        --master data/market_database/master_history.csv \
        --output-dir ./backtests \
        --start 2023-01-01 --top-n 15 --cost-bps 25
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import glob
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corporate_actions import load_events, adjust_long, integrity_layer, share_count_events  # noqa: E402
import security_master  # noqa: E402
from portfolio_accounting import Book  # noqa: E402
import allocator  # noqa: E402


# ---------------------------------------------------------------
# Load & shape
# ---------------------------------------------------------------
def load_wide(master_path: Path, start: str, history_dir: Path | None = None,
              events_path: Path | None = None) -> dict:
    """Load OHLCV (yearly parquet archive + master), apply the derived-data layer,
    and return adjusted matrices (signals) plus raw matrices (execution) under "RAW"."""
    use = ["DATE", "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"]
    df = pd.read_csv(master_path, low_memory=False)
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    keep = [c for c in use if c in df.columns]
    df = df[keep]
    if history_dir is not None:
        hist = [pd.read_parquet(f, columns=[c for c in use if c != "SERIES"])
                for f in sorted(glob.glob(str(Path(history_dir) / "nse_*.parquet")))]
        if hist:
            for h in hist:
                h["DATE"] = h["DATE"].astype(str)
                h["SYMBOL"] = h["SYMBOL"].astype(str)
                h["SERIES"] = "EQ"          # the archive stores EQ rows only
            df["DATE"] = df["DATE"].astype(str)
            df = pd.concat(hist + [df], ignore_index=True)
    df["DATE"] = df["DATE"].astype(str)
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.upper() == "EQ"]
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    for c in ["OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates(subset=["DATE", "SYMBOL"], keep="last")
    df = integrity_layer(df)                     # quarantine corrections + rename stitching
    df["DATE_DT"] = pd.to_datetime(df["DATE"], format="%Y%m%d", errors="coerce")
    df = df[df["DATE_DT"].notna() & (df["DATE_DT"] >= pd.Timestamp(start) - pd.Timedelta(days=400))]

    raw = {c: df.pivot(index="DATE_DT", columns="SYMBOL", values=c).sort_index()
           for c in ["OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"]}
    # Non-EQ prints (e.g. a spin-off's first BE-series sessions, or a held stock moved to
    # BE) are tradable: execution and the stop's ATR only, never signals
    sup = security_master.load_supplement()
    sup_rows = pd.DataFrame()
    if len(sup):
        sup = sup[sup["DATE"].astype(str) >= (pd.Timestamp(start) - pd.Timedelta(days=400)).strftime("%Y%m%d")].copy()
        sup["DATE"] = sup["DATE"].astype(str)
        sup["DATE_DT"] = pd.to_datetime(sup["DATE"], format="%Y%m%d")
        sup = sup.drop_duplicates(["DATE_DT", "SYMBOL"], keep="first")
        for c in ["OPEN", "HIGH", "LOW", "CLOSE"]:
            extra = sup.pivot(index="DATE_DT", columns="SYMBOL", values=c)
            raw[c] = raw[c].combine_first(extra)       # EQ rows win; supplement fills gaps
        idx, cols = raw["CLOSE"].index, raw["CLOSE"].columns
        for c in raw:
            raw[c] = raw[c].reindex(index=idx, columns=cols)
        have = set(zip(df["SYMBOL"], df["DATE"]))
        sup_rows = sup[[(s, d) not in have for s, d in zip(sup["SYMBOL"], sup["DATE"])]]
        sup_rows = sup_rows[sup_rows["DATE_DT"].isin(idx)][["DATE", "SYMBOL", "OPEN", "HIGH", "LOW", "CLOSE", "DATE_DT"]]
    # Adjust EQ and supplement rows in one pass so both share the same factors
    both = pd.concat([df.assign(_SUP=False), sup_rows.assign(_SUP=True)], ignore_index=True)
    if events_path is not None and Path(events_path).exists():
        both = adjust_long(both, load_events(events_path))
        print(f"[INFO] signals use split/bonus back-adjusted prices ({events_path})")
    else:
        print("[WARN] no corporate-actions file: signal prices are RAW")
    df = both[~both["_SUP"]]
    out = {c: df.pivot(index="DATE_DT", columns="SYMBOL", values=c).sort_index()
           for c in ["OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE"]}
    # EXEC: adjusted bars a holding actually traded on (EQ, else supplement), for the stop's ATR
    out["EXEC"] = {c: both.pivot(index="DATE_DT", columns="SYMBOL", values=c).sort_index()
                   .reindex(index=raw["CLOSE"].index, columns=raw["CLOSE"].columns) for c in ["HIGH", "LOW", "CLOSE"]}
    out["RAW"] = raw
    print(f"[INFO] loaded {out['CLOSE'].shape[0]} sessions x {out['CLOSE'].shape[1]} symbols")
    return out


# ---------------------------------------------------------------
# Factors & regime inputs (all vectorised on wide matrices)
# ---------------------------------------------------------------
def atr14(H: pd.DataFrame, L: pd.DataFrame, C: pd.DataFrame) -> pd.DataFrame:
    """ATR(14) on wide matrices: mean true range over 14 sessions, at least 10 observed."""
    prev_c = C.shift(1)
    tr = pd.concat([(H - L).stack(), (H - prev_c).abs().stack(), (L - prev_c).abs().stack()], axis=1).max(axis=1)
    return tr.unstack().reindex(index=C.index, columns=C.columns).rolling(14, min_periods=10).mean()


def build_factors(W: dict) -> dict:
    C, H, L, TV = W["CLOSE"], W["HIGH"], W["LOW"], W["TRADED_VALUE"]
    F = {}
    F["MOM_3_1"] = C.shift(21) / C.shift(63) - 1
    F["MOM_6_1"] = C.shift(21) / C.shift(126) - 1
    ret1 = C.pct_change(fill_method=None)
    F["RET1"] = ret1
    F["VOL90"] = ret1.rolling(90, min_periods=60).std() * np.sqrt(252)
    F["VOL20"] = ret1.rolling(20, min_periods=10).std()
    F["ATR14"] = atr14(H, L, C)
    F["SMA50"] = C.rolling(50, min_periods=30).mean()
    F["SMA200"] = C.rolling(200, min_periods=120).mean()
    F["LOW22"] = L.rolling(22, min_periods=10).min()
    return F


def regime_series(W: dict, F: dict, variant: str, debounce: int = 2,
                  breadth_thr: float = 0.55, vol_thr: float = 0.22) -> pd.DataFrame:
    """Compute the daily regime for a variant: 'old' or 'fixed'."""
    C, TV = W["CLOSE"], W["TRADED_VALUE"]
    dates = C.index
    rows = []
    prev_state, pending, pcount = None, None, 0

    # Equal-weighted index of the liquid-100 (recomputed daily membership is slow;
    # use a rolling liquid universe based on 20d average traded value)
    tv20 = TV.rolling(20, min_periods=10).mean()
    ret1 = F["RET1"]

    for i, d in enumerate(dates):
        tv_row = tv20.loc[d].dropna()
        if len(tv_row) < 100:
            rows.append((d, None, np.nan, np.nan, None)); continue
        liquid = tv_row.nlargest(100).index

        c_row = C.loc[d, liquid].dropna()
        s200 = F["SMA200"].loc[d, liquid].dropna()
        trend_ok = bool(len(s200) > 30 and c_row.mean() > s200.mean())

        s50_all = F["SMA50"].loc[d].dropna()
        c_all = C.loc[d, s50_all.index].dropna()
        common = s50_all.index.intersection(c_all.index)
        breadth = (c_all[common] > s50_all[common]).mean() if len(common) > 100 else 0.5
        breadth_ok = bool(breadth > breadth_thr)

        if variant == "old":
            v = F["VOL20"].loc[d, liquid].dropna()
            vol = float(v.mean() * np.sqrt(252)) if len(v) > 30 else np.nan
        else:
            # index-level: volatility of the equal-weighted liquid-100 return series
            lo = max(0, i - 25)
            idx_ret = ret1.iloc[lo:i + 1][liquid].mean(axis=1)
            vol = float(idx_ret.tail(20).std() * np.sqrt(252)) if idx_ret.notna().sum() >= 10 else np.nan
        vol_ok = bool(vol == vol and vol < vol_thr)

        score = int(trend_ok) + int(breadth_ok) + int(vol_ok)
        raw = "RISK_ON" if score == 3 else ("RISK_NEU" if score == 2 else "RISK_OFF")

        if variant == "old" or debounce <= 1:
            state = raw
        else:
            if prev_state is None:
                state = raw
            elif raw == prev_state:
                state, pending, pcount = prev_state, None, 0
            else:
                if raw == pending:
                    pcount += 1
                else:
                    pending, pcount = raw, 1
                if pcount >= debounce:
                    state, pending, pcount = raw, None, 0
                else:
                    state = prev_state
        prev_state = state
        rows.append((d, state, breadth, vol, raw))

    r = pd.DataFrame(rows, columns=["DATE", "state", "breadth", "vol", "raw"]).set_index("DATE")
    return r


GROSS = allocator.REGIME_GROSS                       # RISK_ON 1.00 / RISK_NEU 0.70 / RISK_OFF 0.40
ENTRY_ALLOCATIONS = allocator.LEGACY_METHODS + ("shared",)
allocate_entries = allocator.legacy_allocate           # rank_order / pro_rata_leftover, unchanged (see allocator.py)


def accounting_book_state(book: Book, sectors: dict) -> allocator.BookState:
    """The backtest Book as the shared allocator sees it (the same states the paper ledger view uses):
    shares (open, stale, priced pending) are exposure at their last mark; cash owed is committed and holds its
    slot unless it is a dividend; fractions hold no slot; pending shares with no price yet leave the book
    incomplete, so nothing is approved until they are priced. Cash is the book's cash now."""
    ex, cl, problems = [], [], []
    for h in book.active():
        if h.kind == "shares" and (h.state != "pending" or h.last_mark_date):
            val = h.qty * h.last_mark if h.last_mark and h.last_mark > 0 else None
            ex.append(allocator.Exposure(h.symbol, "swing", val, sectors.get(h.symbol), "cash", h.symbol))
        elif h.kind == "shares":
            problems.append(f"{h.symbol}: pending entitlement with no price yet ({h.origin})")
            cl.append(allocator.Claim(h.symbol, "swing", 0.0, True, True))
        elif h.kind == "cash":
            div = Book.is_dividend_receivable(h)
            cl.append(allocator.Claim(h.symbol, "swing", float(h.cash_due), slot=not div, held=not div))
        else:
            cl.append(allocator.Claim(h.symbol, "swing", float(h.cash_due), slot=False, held=True))
    return allocator.BookState(tuple(ex), tuple(cl), float(book.cash), tuple(problems))


# ---------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------
def simulate(W: dict, F: dict, reg: pd.DataFrame, start: str,
             top_n: int = 15, max_pos: int = 15, risk_pct: float = 0.01,
             max_w: float = 0.10, cost_bps: float = 35.0,
             min_close: float = 20.0, min_tv: float = 1e7,
             time_stop: int | None = None, chandelier: float = 4.0,
             capital: float = 200000.0, events_path: Path | None = None,
             entry_allocation: str = "rank_order", policy: "allocator.Policy | None" = None,
             sectors: dict | None = None, gap_allowance_atr: float | None = None) -> dict:
    """Weekly entries with share-based accounting (see portfolio_accounting.py).
    entry_allocation: how new positions share the regime budget - see allocate_entries, or "shared" for the
    shared allocator with `policy` (unset by default: nothing is opened), `sectors` {symbol: sector} and
    `gap_allowance_atr`; max_pos and the risk_pct / max_w sizing parameters are then not used."""
    if entry_allocation not in ENTRY_ALLOCATIONS:
        raise ValueError(f"entry_allocation must be one of {ENTRY_ALLOCATIONS}, not {entry_allocation!r}")
    shared = entry_allocation == "shared"
    policy = policy if policy is not None else allocator.Policy()
    sectors = sectors or {}
    R = W["RAW"]
    RO, RH, RL, RC = R["OPEN"], R["HIGH"], R["LOW"], R["CLOSE"]
    C, TV = W["CLOSE"], W["TRADED_VALUE"]
    # Stops: the trailing high comes from the bars a holding actually trades on (EQ, or the
    # BE supplement when EQ is missing), so its ATR is computed on those same bars.
    # Identical to the signal ATR for every symbol without supplement rows.
    X = W.get("EXEC")
    if X is not None:
        atr_adj, factor = atr14(X["HIGH"], X["LOW"], X["CLOSE"]), X["CLOSE"] / RC.reindex_like(X["CLOSE"])
    else:
        atr_adj, factor = F["ATR14"], C / RC.reindex_like(C)   # adjusted = raw x factor
    atr_raw = atr_adj / factor                          # ATR in each day's as-traded price terms
    cost = cost_bps / 10000.0
    sess = [t for t in RC.index if t >= pd.Timestamp(start)]
    key = {t: t.strftime("%Y%m%d") for t in RC.index}
    rev = {v: k for k, v in key.items()}

    def bar(sym, d):
        t = rev.get(d)
        if t is None or sym not in RC.columns:
            return None
        o, c = RO.at[t, sym], RC.at[t, sym]
        if not (o == o and c == c and o > 0):
            return None
        return {"open": float(o), "high": float(RH.at[t, sym]), "low": float(RL.at[t, sym]), "close": float(c)}

    def atr(sym, d):
        t = rev.get(d)
        if t is None or sym not in atr_raw.columns:
            return None
        a = atr_raw.at[t, sym]
        return float(a) if a == a and a > 0 else None

    ev = load_events(events_path) if events_path and Path(events_path).exists() else pd.DataFrame()
    share_ev = share_count_events(ev) if len(ev) else ev
    other_ev = ev[ev["FACTOR_TYPE"] != "official_ratio"] if len(ev) else ev
    book = Book(capital, bar, atr, lambda n, side, sym, d: n * cost, share_ev, other_ev,
                security_master.load_events(), chandelier=chandelier, time_stop=time_stop)

    score = (0.625 * F["MOM_3_1"].rank(axis=1, pct=True).add(F["MOM_6_1"].rank(axis=1, pct=True)).div(2)
             + 0.375 * (1 - F["VOL90"].rank(axis=1, pct=True)))
    tv20 = TV.rolling(20, min_periods=10).mean() if shared else None
    comps, last_comp, alloc_log = [], None, []

    def enter_shared(d, prev, cands, equity, state):
        """The ranked list sized by the shared allocator, against the book as it stands before the open."""
        raw_state = reg["state"].get(prev)
        regime = raw_state if isinstance(raw_state, str) and raw_state else None    # unknown: nothing approved
        reqs = []
        for i, sym in enumerate(cands):
            a, cp = atr(sym, key[prev]), RC.at[prev, sym]
            ok = bool(a) and cp == cp and cp > 0
            reqs.append(allocator.Request(
                key=sym, symbol=sym, engine="swing", priority=float(len(cands) - i),   # the score ranking
                price=float(cp) if ok else None, stop_loss=2.0 * a if ok else None,
                gap_allowance=gap_allowance_atr * a if ok and gap_allowance_atr is not None else None,
                costs=2.0 * cost * float(cp) if ok else None,                         # cost_bps on both legs
                sector=sectors.get(sym), adv_inr=tv20.at[prev, sym] if sym in tv20.columns else None))
        decisions = allocator.allocate(reqs, accounting_book_state(book, sectors), policy.at_capital(equity), regime)
        for rank, dec in enumerate(decisions, 1):
            opened = None
            if dec.status == "approved" and (b := bar(dec.symbol, d)) is not None:
                opened = book.open_position(dec.symbol, d, dec.quantity, b["open"], atr(dec.symbol, key[prev]),
                                            regime=state)
            fail = dec.failed
            alloc_log.append({"date": d, "mode": "shared", "rank": rank, "symbol": dec.symbol, "regime": regime,
                              "status": dec.status, "qty": dec.quantity, "notional_ref": dec.notional_inr,
                              "opened_qty": opened.qty if opened else 0, "check": fail[0] if fail else "",
                              "detail": fail[2] if fail else "; ".join(dec.notes)})

    for t in sess:
        d = key[t]
        book.apply_events(d)
        book.settle_pending(d)
        book.check_exits(d)

        # ---------- weekly entries (Mondays), decided at the previous close ----------
        k = RC.index.get_loc(t)
        if t.weekday() == 0 and k > 0 and last_comp is not None and (shared or book.slots_used() < max_pos):
            prev = RC.index[k - 1]
            state = reg["state"].get(prev, "RISK_NEU") or "RISK_NEU"
            gross = GROSS.get(state, 0.7)
            equity = last_comp["equity"]                         # valued at the previous close
            occupied = last_comp["open_mtm"] + last_comp["pending_ref"] + last_comp["stale_mark"]
            sc = score.loc[prev].dropna() if prev in score.index else pd.Series(dtype=float)
            sc = sc[sc.index.isin(RC.columns)]
            px_prev = RC.loc[prev, sc.index]
            elig = sc.index[(px_prev >= min_close).to_numpy() & (TV.loc[prev, sc.index] >= min_tv).to_numpy()]
            elig = [x for x in elig if not book.holds(x)]
            cands = list(sc[elig].nlargest(top_n).index)
            if shared:
                enter_shared(d, prev, cands, equity, state)
            else:
                picks = cands[:max_pos - book.slots_used()]
                intended = {}
                for sym in cands:
                    a, cp = atr(sym, key[prev]), RC.at[prev, sym]
                    if not (a and cp == cp and cp > 0):
                        continue
                    notional = (equity * risk_pct / (2.0 * a)) * cp
                    intended[sym] = min(notional, equity * max_w)
                sizes = allocate_entries(intended, picks, equity, gross, max(equity * gross - occupied, 0),
                                         entry_allocation)
                for rank, sym in enumerate(cands, 1):
                    opened = None
                    if sym in sizes and (b := bar(sym, d)) is not None:
                        qty = int(sizes[sym] / RC.at[prev, sym])  # quantity fixed before the open
                        opened = book.open_position(sym, d, qty, b["open"], atr(sym, key[prev]), regime=state)
                    alloc_log.append({"date": d, "mode": entry_allocation, "rank": rank, "symbol": sym,
                                      "regime": state, "status": "approved" if sym in sizes else "not funded",
                                      "qty": int(sizes[sym] / RC.at[prev, sym]) if sym in sizes else 0,
                                      "notional_ref": round(sizes.get(sym, 0.0), 2),
                                      "opened_qty": opened.qty if opened else 0, "check": "", "detail": ""})

        book.end_of_day(d)
        last_comp = book.value(d)
        last_comp["state"] = reg["state"].get(t, None)
        comps.append(last_comp)

    comp = pd.DataFrame(comps)
    comp.index = pd.to_datetime(comp["date"], format="%Y%m%d")
    holdings_end = pd.DataFrame([h.to_dict() for h in book.active()])
    occupied = comp["open_mtm"] + comp["pending_ref"] + comp["stale_mark"]
    return dict(equity=comp["equity"], components=comp, trades=pd.DataFrame(book.trades),
                holdings_end=holdings_end, log=pd.DataFrame(book.log),
                exposure=pd.DataFrame({"exposure": occupied / comp["equity"], "state": comp["state"]}),
                allocations=pd.DataFrame(alloc_log))


def benchmark_series(W: dict, start: str) -> pd.Series:
    """Equal-weighted index of the liquid universe (proxy for a broad index)."""
    C, TV = W["CLOSE"], W["TRADED_VALUE"]
    tv20 = TV.rolling(20, min_periods=10).mean()
    ret = C.pct_change(fill_method=None)
    vals, idx = [], []
    lvl = 100.0
    for i, d in enumerate(C.index):
        if d < pd.Timestamp(start) or i == 0:
            continue
        row = tv20.iloc[i - 1].dropna()     # membership known at the previous close
        if len(row) < 100:
            continue
        liquid = row.nlargest(100).index
        r = ret.loc[d, liquid].mean()
        if r == r:
            lvl *= (1 + r)
        vals.append(lvl); idx.append(d)
    return pd.Series(vals, index=idx)


def stats(eq: pd.Series, bench: pd.Series) -> dict:
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    tot = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + tot) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan
    b = bench.reindex(eq.index).ffill()
    br = b.pct_change().dropna()
    btot = b.iloc[-1] / b.iloc[0] - 1
    ex = (r - br.reindex(r.index)).dropna()
    ir = ex.mean() / ex.std() * np.sqrt(252) if ex.std() > 0 else np.nan
    return dict(total_return_pct=round(tot * 100, 2), cagr_pct=round(cagr * 100, 2),
                max_drawdown_pct=round(dd * 100, 2), sharpe=round(float(sharpe), 2),
                benchmark_total_return_pct=round(btot * 100, 2),
                excess_vs_benchmark_pct=round((tot - btot) * 100, 2),
                information_ratio=round(float(ir), 2), years=round(yrs, 2))


def unresolved_impact(sim: dict) -> dict:
    """What a headline result depends on: holdings that are not open at the end,
    stale exposure during the run, and recovery scenarios."""
    comp = sim["components"]
    end = comp.iloc[-1]
    he = sim["holdings_end"]
    rows = []
    for h in (he.itertuples(index=False) if len(he) else []):
        val = h.cash_due if h.kind == "cash" else h.qty * h.last_mark
        rows.append({"symbol": h.symbol, "state": h.state, "kind": h.kind, "last_mark_date": h.last_mark_date,
                     "reason": h.stale_reason or h.origin, "value_at_last_mark": round(float(val), 2),
                     "pct_of_equity": round(float(val) / float(end["equity"]) * 100, 2)})
    stale_val = sum(r["value_at_last_mark"] for r in rows if r["state"] == "stale")
    return {
        "end_components": {k: (round(float(v), 2) if isinstance(v, (int, float, np.floating)) else v)
                           for k, v in end.items()},
        "holdings_not_open_at_end": [r for r in rows if r["state"] != "open"],
        "recovery_scenarios_final_equity": {
            "stale_at_last_mark": round(float(end["equity"]), 2),
            "stale_at_50pct": round(float(end["equity"] - 0.5 * stale_val), 2),
            "stale_at_zero": round(float(end["equity_stale_at_zero"]), 2),
        },
        "max_stale_value_pct_of_equity": round(float((comp["stale_mark"] / comp["equity"]).max() * 100), 2),
        "sessions_with_any_stale_holding": int((comp["n_stale"] > 0).sum()),
        "unconfirmed_fraction_value_end": round(float(end["fraction_ref_unconfirmed"]), 2),
        "unpriced_pending_indicative_end": round(float(end["pending_unpriced_indicative"]), 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="./data/market_database/master_history.csv")
    ap.add_argument("--output-dir", default="./backtests")
    ap.add_argument("--history-dir", default=None,
                    help="Yearly parquet archive (data/market_database/history) for runs before 2023.")
    ap.add_argument("--corporate-actions", default="./data/market_database/corporate_actions.csv")
    ap.add_argument("--variants", default="old,fixed", help="Regime variants to run.")
    ap.add_argument("--start", default="2023-06-01")
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--max-pos", type=int, default=15)
    ap.add_argument("--cost-bps", type=float, default=35.0)
    ap.add_argument("--time-stop", type=int, default=0,
                    help="0 = disabled (new default). Old behaviour was 25.")
    ap.add_argument("--chandelier", type=float, default=4.0)
    ap.add_argument("--capital", type=float, default=200000.0)
    ap.add_argument("--entry-allocation", choices=ENTRY_ALLOCATIONS, default="rank_order",
                    help="rank_order (default: size_positions formula, funded in rank order), "
                         "pro_rata_leftover (original backtest rule) or shared (shared allocator).")
    ap.add_argument("--risk-config", default=None,
                    help="shared only: risk config TOML (default: config/risk_config.toml, else the unconfigured "
                         "example, which opens nothing).")
    ap.add_argument("--sectors", default=None,
                    help="shared only: CSV with Symbol and Industry columns (e.g. nifty500.csv). A pick with no "
                         "sector is rejected.")
    args = ap.parse_args()
    policy, sectors, gap_atr = None, None, None
    if args.entry_allocation == "shared":
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from research_system import config as cfgmod
        cfg = cfgmod.load(args.risk_config)
        policy = allocator.policy_from_risk_config(cfg)
        gap_atr = cfg.get("engines", {}).get("swing", {}).get("gap_allowance_atr")
        if args.sectors:
            s = pd.read_csv(args.sectors)
            sectors = dict(zip(s["Symbol"].astype(str).str.strip().str.upper(), s["Industry"].astype(str)))
        if policy.problems():
            print(f"[WARN] shared allocation: policy unset ({'; '.join(policy.problems())}); no position will open")

    outdir = Path(args.output_dir); outdir.mkdir(parents=True, exist_ok=True)
    ev_path = Path(args.corporate_actions) if args.corporate_actions else None
    W = load_wide(Path(args.master), args.start,
                  history_dir=Path(args.history_dir) if args.history_dir else None, events_path=ev_path)
    F = build_factors(W)
    bench = benchmark_series(W, args.start)

    results = {}
    for variant in args.variants.split(","):
        print(f"\n[INFO] === regime variant: {variant} ===")
        reg = regime_series(W, F, variant)
        sim = simulate(W, F, reg, args.start, top_n=args.top_n, max_pos=args.max_pos,
                       cost_bps=args.cost_bps, capital=args.capital,
                       time_stop=(args.time_stop or None), chandelier=args.chandelier, events_path=ev_path,
                       entry_allocation=args.entry_allocation, policy=policy, sectors=sectors,
                       gap_allowance_atr=gap_atr)
        st = stats(sim["equity"], bench)
        st["stats_with_stale_at_zero"] = stats(sim["components"]["equity_stale_at_zero"], bench)
        st["regime_days"] = sim["exposure"]["state"].value_counts().to_dict()
        st["avg_exposure_pct"] = round(float(sim["exposure"]["exposure"].mean() * 100), 1)
        st["n_trades"] = int(len(sim["trades"]))
        if len(sim["trades"]):
            st["win_rate_pct"] = round(float((sim["trades"]["pnl"] > 0).mean() * 100), 1)
            st["exit_reasons"] = sim["trades"]["reason"].value_counts().to_dict()
        st["unresolved_impact"] = unresolved_impact(sim)
        results[variant] = st
        sim["equity"].to_csv(outdir / f"equity_{variant}.csv", header=["EQUITY"])
        sim["components"].to_csv(outdir / f"components_{variant}.csv")
        sim["trades"].to_csv(outdir / f"trades_{variant}.csv", index=False)
        sim["holdings_end"].to_csv(outdir / f"holdings_end_{variant}.csv", index=False)
        sim["log"].to_csv(outdir / f"events_log_{variant}.csv", index=False)
        sim["allocations"].to_csv(outdir / f"allocations_{variant}.csv", index=False)
        reg.to_csv(outdir / f"regime_{variant}.csv")
        print(json.dumps(st, indent=2, default=str))

    bench.to_csv(outdir / "benchmark_proxy.csv", header=["LEVEL"])
    with open(outdir / "summary.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\n[DONE] outputs written to", outdir)


if __name__ == "__main__":
    main()
