"""
ledger_view.py
==============
Read-only view of the paper ledger (positions.csv + equity_log.csv) for reports and the
research risk engine. Nothing here writes files or changes the ledger.

Every ACTIVE row (status open / stale / pending) falls in exactly one bucket; closed and
converted rows are ignored:

  row (kind, status)                         bucket                 value
  shares, open                               share exposure         qty x latest close if supplied,
                                                                    else the ledger's last mark
  shares, stale                              share exposure         qty x last valid mark
  shares, pending + exit offer locked        share exposure         qty x last traded price
  shares, pending entitlement with a mark    share exposure         qty x its reference mark
  shares, pending entitlement without mark   UNPRICED               none: no defensible value
  cash, pending, dividend                    owed, not spendable    cash_due
  cash, pending, exit/merger consideration   owed, not spendable    cash_due
  fraction, pending                          fraction (reported)    outside cash and equity; with a market
                                                                    price it is bounded by one share,
                                                                    without one it has no defensible value
  fraction, pending, sale proceeds documented owed, not spendable   cash_due (credited only on its pay date)

  undefended      active rows with no defensible value: an entitlement with no price yet (e.g.
                  demerged shares before they trade), shares with no valid mark, an open holding
                  with neither a latest close nor a stored mark (an entry price is not a valuation),
                  cash owed with no amount, a fraction with no price. The risk view fails closed on any.
  available cash  the last logged CASH (equity_log.csv, written by the same run as positions.csv),
                  used only when verify_cash() recomputes the same amount from the ledger's own
                  records; otherwise None and the reason ("unverifiable" or the mismatch)
  marked equity   verified cash + share exposure + owed (None if cash is unverified or any row is
                  undefended)
  slots           the ledger's rule (portfolio_accounting.Book.slots_used): every active row except
                  fractions and dividend receivables
  entry fees      entry_cost_inr is the fee the ledger charged at entry; a 0 counts only with its basis
                  (entry_fee_source). Fees estimated with today's cost model are a separate, labelled
                  display (entry_fee_estimates) and are never read into cash, cost or P&L.
  provisional     rows a back-dated replay produced (an exit or a change in slot use dated before
                  sessions already recorded, which were produced without it); cash from a provisional
                  exit is not verified until the history is rebuilt (corrected_replay.py)
  held symbols    the ledger's rule (Book.holds): every active row except dividend receivables

Older ledgers (no kind / last_mark / cash_due columns; status open or closed) are read as open
shares; with no last mark, the latest close is used if supplied, else the entry price (flagged as
undefended).
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

ACTIVE = ("open", "stale", "pending")
OPEN, STALE, LOCKED, PENDING, UNPRICED = "open", "stale", "locked", "pending entitlement", "pending (unpriced)"
DIV_OWED, CONSIDERATION_OWED, FRACTION = "dividend owed", "consideration owed", "fraction"
FRACTION_OWED = "fraction proceeds owed"
OWED_STATES = (DIV_OWED, CONSIDERATION_OWED, FRACTION_OWED)
SHARE_STATES = (OPEN, STALE, LOCKED, PENDING)
LABEL = {OPEN: "open", STALE: "stale (untradeable)", LOCKED: "locked (exit offer)", PENDING: "pending entitlement",
         UNPRICED: "pending entitlement, no price yet", DIV_OWED: "dividend owed",
         CONSIDERATION_OWED: "cash consideration owed", FRACTION: "fraction (unconfirmed)",
         FRACTION_OWED: "fraction sale proceeds owed"}
FEE_ESTIMATES = Path(__file__).resolve().parent.parent / "data" / "ledger_evidence" / "entry_fee_estimates.csv"
ESTIMATE_LABEL = "ESTIMATE with today's cost model: never charged, not cash, not in cost or P&L"


def _s(v) -> str:
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip().lower() in ("", "nan", "none") else str(v).strip()


def _f(v) -> float:
    x = pd.to_numeric(v, errors="coerce")
    return float(x) if pd.notna(x) else float("nan")


def is_new_format(pos: pd.DataFrame) -> bool:
    return "kind" in pos.columns and pos["kind"].map(_s).ne("").any()


def classify(pos: pd.DataFrame, last_prices: dict | None = None) -> pd.DataFrame:
    """One row per active ledger row: state, value and how it counts (exposure / owed / slot / held)."""
    last_prices = {str(k).upper(): v for k, v in (last_prices or {}).items()}
    cols = ["symbol", "state", "label", "qty", "mark", "mark_date", "mark_basis", "exposure", "owed",
            "slot", "held", "entry_price", "current_stop", "cash_date", "detail", "hid"]
    if pos is None or pos.empty or "status" not in pos.columns:
        return pd.DataFrame(columns=cols)
    new = is_new_format(pos)
    out = []
    for r in pos.to_dict("records"):
        status = _s(r.get("status")).lower()
        if status not in ACTIVE:
            continue
        sym = _s(r.get("symbol")).upper()
        kind = (_s(r.get("kind")) or "shares") if new else "shares"
        qty, entry = _f(r.get("qty")), _f(r.get("entry_price"))
        mark, mark_date = _f(r.get("last_mark")), _s(r.get("last_mark_date")).split(".")[0]
        has_mark = mark == mark and mark > 0 and (mark_date != "" or not new)
        applied = _s(r.get("ca_applied"))
        origin = _s(r.get("origin"))
        row = {"symbol": sym, "qty": qty, "entry_price": entry, "current_stop": _f(r.get("current_stop")),
               "mark": float("nan"), "mark_date": "", "mark_basis": "", "exposure": 0.0, "owed": 0.0,
               "cash_date": "", "detail": "", "hid": _s(r.get("hid"))}
        if kind == "cash":
            due = _f(r.get("cash_due"))
            state = DIV_OWED if origin.startswith("dividend:") else CONSIDERATION_OWED
            row.update(owed=due if due == due else 0.0, cash_date=_s(r.get("cash_date")).split(".")[0],
                       detail="pay date " + (_s(r.get("cash_date")).split(".")[0] or "not established"))
            if due != due:
                row["detail"] += "; cash_due missing"
        elif kind == "fraction":
            due = _f(r.get("cash_due"))
            if due == due and due > 0:              # sale proceeds documented: owed, still not cash
                state = FRACTION_OWED
                pay = _s(r.get("cash_date")).split(".")[0]
                row.update(owed=due, cash_date=pay, detail="fraction sale proceeds; pay date " + (pay or "not documented"))
            else:
                state = FRACTION
                row["detail"] = "unconfirmed fractional entitlement; outside cash and equity"
                if has_mark:                        # successor's market price: value bounded by one share
                    row.update(mark=mark, mark_date=mark_date, mark_basis="successor last mark (upper bound)")
        else:
            if status == "open":
                state = OPEN
                px = _f(last_prices.get(sym))
                if px == px and px > 0:
                    row.update(mark=px, mark_basis="latest close")
                elif has_mark:
                    row.update(mark=mark, mark_date=mark_date, mark_basis="ledger last mark")
                elif entry == entry and entry > 0:
                    row.update(mark=entry, mark_basis="entry price (no mark in ledger)")
            else:
                if status == "stale":
                    state = STALE
                    row["detail"] = _s(r.get("stale_reason"))
                elif any(k.endswith(":locked") for k in applied.split(";")):
                    state = LOCKED
                    row["detail"] = "exit offer: shares locked until tender"
                else:
                    state = PENDING
                    tf = _s(r.get("tradable_from")).split(".")[0]
                    row["detail"] = f"{origin or 'entitlement'}; tradable from {tf or 'unknown'}"
                if has_mark:                       # last valid mark only: never a fresh price for untradeable shares
                    row.update(mark=mark, mark_date=mark_date, mark_basis="last valid mark")
                elif state == PENDING:
                    state = UNPRICED
                    row["detail"] += "; no price yet: outside exposure and equity"
                else:
                    row["detail"] += "; no valid mark in the ledger"
            if row["mark"] == row["mark"] and qty == qty:
                row["exposure"] = qty * row["mark"]
        row["state"], row["label"] = state, LABEL[state]
        row["slot"] = state not in (FRACTION, FRACTION_OWED, DIV_OWED)
        row["held"] = state != DIV_OWED
        out.append(row)
    return pd.DataFrame(out, columns=cols)


def undefended(rows: pd.DataFrame) -> list[str]:
    """Active rows with no defensible value, as 'SYMBOL: reason'."""
    out = []
    for r in rows.itertuples(index=False):
        if r.state == UNPRICED:
            out.append(f"{r.symbol}: pending entitlement with no price yet ({r.detail.split('; no price')[0]})")
        elif r.state in SHARE_STATES and r.mark != r.mark:
            out.append(f"{r.symbol}: {LABEL[r.state]} shares with no valid mark")
        elif r.state == OPEN and r.mark_basis.startswith("entry price"):
            out.append(f"{r.symbol}: open holding with no latest close and no stored mark (an entry price is not a valuation)")
        elif r.state in OWED_STATES and "cash_due missing" in r.detail:
            out.append(f"{r.symbol}: {LABEL[r.state]} with no amount recorded")
        elif r.state == FRACTION and r.mark != r.mark:
            out.append(f"{r.symbol}: fractional entitlement with no price and no documented settlement")
    return out


CASH_TOL = 1.0      # rupees: every figure is rounded to paise when the ledger writes it


def verify_cash(pos: pd.DataFrame | None, equity_log: pd.DataFrame | None,
                realized: pd.DataFrame | None = None) -> dict:
    """Recompute the ledger's cash from its own records and compare it with the last logged CASH.

    Both ledger versions keep cash by the same identity:
        cash = starting capital - cost of every active holding + realised P&L of every closed row
      older ledger   cost = size_inr + entry_cost_inr of open rows  (its _compute_equity)
      current ledger cost = cost_basis of every active row, incl. cash owed and fractions
                     (paper_trade_manager.run rebuilds cash this way on every load)
    How each cash movement the system represents enters the identity:
      buy                cash - (notional + entry fee)       the holding's cost (active); the entry fee is the
                         one the ledger charged (entry_cost_inr), never a fee estimated with today's model
      sale / stop exit   cash + (proceeds - exit fees)       realised P&L = net proceeds - cost (closed row)
      fees               entry fees in the cost, exit fees in the net proceeds
      dividend, ex-date  no cash: a receivable row (active) holding its share of cost (0 for an
                         ordinary dividend) - the amount owed is NOT counted as cash
      dividend, paid     cash + amount                       realised P&L = amount - receivable cost
      exit offer         locked shares (active) -> cash owed (same row, active) -> paid: realised P&L
      merger in shares   parent row 'converted' (in neither sum); successor + fraction rows carry its cost
      fraction           no cash: carries its share of the cost (active) - neither the fraction nor any
                         trustee-sale proceeds are cash; documented proceeds are owed, then paid: realised P&L
      merger in cash     cash owed (active) -> paid: realised P&L
      demerger           child at cost 0 (active); its sale: realised P&L = proceeds
    Every amount must be recorded: a cost note (cost not fully recorded), an entry fee of 0 with no stated
    basis, a closed row without realised P&L, a provisional exit (from a back-dated replay) or a
    non-empty CASH_NOTE in the equity log makes the cash unverifiable - a blank is never read as zero. Starting capital = the first equity-log row, which must be a pre-trading row
    (nothing open, invested or realised; CASH = EQUITY). The logged INVESTED, REALIZED_PNL and N_OPEN
    are cross-checked, and so is realized_trades.csv when supplied, and the logged cash of EVERY
    equity-log date is rebuilt from the dated movements (cash_history).
    Returns {'verified', 'cash', 'as_of', 'check', 'history'}; 'cash' is None unless verified."""
    res = {"verified": False, "cash": None, "as_of": "", "check": "", "history": None}
    if equity_log is None or equity_log.empty:
        res["check"] = "unverifiable: no equity log, so neither the starting capital nor the cash is recorded"
        return res
    miss = [c for c in ("DATE", "CASH", "EQUITY", "N_OPEN") if c not in equity_log.columns]
    if miss:
        res["check"] = f"unverifiable: equity log lacks {', '.join(miss)}"
        return res
    el = equity_log.assign(_d=equity_log["DATE"].astype(str).str.replace("-", "")).sort_values("_d")
    first, last = el.iloc[0], el.iloc[-1]
    res["as_of"] = str(last["DATE"])
    logged = _f(last["CASH"])
    zero = lambda c: c not in el.columns or abs(_f(first[c]) if _f(first[c]) == _f(first[c]) else 1.0) < 0.005
    pre = (_f(first["N_OPEN"]) == 0 and zero("INVESTED") and zero("REALIZED_PNL")
           and abs(_f(first["CASH"]) - _f(first["EQUITY"])) <= CASH_TOL)
    if not pre:
        res["check"] = (f"unverifiable: the first equity-log row ({first['DATE']}) is not a pre-trading row, "
                        "so the starting capital is not recorded")
        return res
    if logged != logged:
        res["check"] = f"unverifiable: CASH missing in the last equity-log row ({last['DATE']})"
        return res
    capital = _f(first["CASH"])
    pos = pos if pos is not None else pd.DataFrame()
    status = pos["status"].map(_s).str.lower() if "status" in pos.columns else pd.Series(dtype=str)
    new = is_new_format(pos) if len(pos) else False
    active, closed = pos[status.isin(ACTIVE)], pos[status == "closed"]
    gaps, cost, cost_if_zero = [], 0.0, 0.0       # cost_if_zero: unrecorded parts taken as 0 (informational)
    only_costs = True                             # every gap is an unrecorded cost (so the zero comparison applies)
    for r in active.to_dict("records"):
        note = _s(r.get("cost_note"))
        size, ec = _f(r.get("size_inr")), _f(r.get("entry_cost_inr"))
        if ec == 0 and not _s(r.get("entry_fee_source")) and not note:
            note = "entry fee recorded as 0 with no stated basis"
        c = _f(r.get("cost_basis")) if new and not note else float("nan")
        if c != c and not note:
            c = size + ec if size == size and ec == ec else float("nan")
        if c == c:
            cost += c
            cost_if_zero += c
            continue
        known = _f(r.get("cost_known_part"))
        known = known if known == known else (size if size == size else float("nan"))
        what = note or ("no entry cost recorded" if size == size else "no cost recorded")
        gaps.append(f"{_s(r.get('symbol'))} entered {_s(r.get('entry_date'))} ({what})")
        cost_if_zero += known
    realised = 0.0
    for r in closed.to_dict("records"):
        p = _f(r.get("realized_pnl_inr"))
        if p != p:
            gaps.append(f"{_s(r.get('symbol'))} closed {_s(r.get('exit_date'))} (no realised P&L recorded)")
            only_costs = False
            continue
        realised += p
        if _s(r.get("cost_note")):
            gaps.append(f"{_s(r.get('symbol'))} closed {_s(r.get('exit_date'))} (realised P&L excludes an "
                        f"unrecorded cost: {_s(r.get('cost_note'))})")
        if _s(r.get("provisional")):
            amt = _row_cost(r)[0] + p
            gaps.append(f"{_s(r.get('symbol'))} closed {_s(r.get('exit_date'))} (cash Rs{amt:,.2f} from a provisional "
                        f"exit: {_s(r.get('provisional'))})")
            only_costs = False
    log_note = _s(last.get("CASH_NOTE")) if "CASH_NOTE" in el.columns else ""
    if log_note and not gaps:
        gaps.append(f"the equity log says the cash leaves out unrecorded amounts: {log_note}")
        only_costs = False
    if realized is not None and len(realized) and "cost_note" in realized.columns:
        for r in realized.to_dict("records"):
            if _s(r.get("cost_note")) and not any(_s(r.get("symbol")) in g for g in gaps):
                gaps.append(f"{_s(r.get('symbol'))} trade {_s(r.get('exit_date'))} in realized_trades.csv "
                            f"(P&L excludes an unrecorded cost)")
    if realized is not None and len(realized) and "provisional" in realized.columns:
        for r in realized.to_dict("records"):
            if _s(r.get("provisional")) and not any(_s(r.get("symbol")) in g and "provisional" in g for g in gaps):
                gaps.append(f"{_s(r.get('symbol'))} trade {_s(r.get('exit_date'))} in realized_trades.csv "
                            f"(provisional exit)")
                only_costs = False
    res["history"] = hist = cash_history(pos, el)
    if gaps:
        res["check"] = "unverifiable: " + "; ".join(gaps[:6]) + (f" (+{len(gaps) - 6} more)" if len(gaps) > 6 else "")
        alt = capital - cost_if_zero + realised
        if alt == alt and only_costs:
            same = abs(alt - logged) <= max(CASH_TOL, 0.01 * (len(active) + len(closed)))
            res["check"] += (f". Taking the unrecorded costs as zero would give Rs{alt:,.2f}, which "
                             + ("equals" if same else "does not equal") + f" the logged cash Rs{logged:,.2f}; "
                             "the records do not say whether zero is correct")
        return res
    tol = max(CASH_TOL, 0.01 * (len(active) + len(closed)))
    expected = capital - cost + realised
    bad = []
    if abs(expected - logged) > tol:
        bad.append(f"logged cash Rs{logged:,.2f} != starting capital Rs{capital:,.2f} - cost of holdings "
                   f"Rs{cost:,.2f} + realised P&L Rs{realised:,.2f} = Rs{expected:,.2f}")
    if "REALIZED_PNL" in el.columns and abs(_f(last["REALIZED_PNL"]) - realised) > tol:
        bad.append(f"logged REALIZED_PNL Rs{_f(last['REALIZED_PNL']):,.2f} != closed rows Rs{realised:,.2f}")
    opens = pos[status == "open"]
    if int(_f(last["N_OPEN"])) != len(opens):
        bad.append(f"logged N_OPEN {int(_f(last['N_OPEN']))} != {len(opens)} open rows")
    if "INVESTED" in el.columns:
        inv = (pd.to_numeric(opens.get("cost_basis"), errors="coerce").sum() if new
               else pd.to_numeric(opens.get("size_inr"), errors="coerce").sum())
        if abs(_f(last["INVESTED"]) - inv) > tol:
            bad.append(f"logged INVESTED Rs{_f(last['INVESTED']):,.2f} != open rows Rs{inv:,.2f}")
    if realized is not None and len(realized) and "pnl_inr" in realized.columns:
        rt = pd.to_numeric(realized["pnl_inr"], errors="coerce")
        if rt.isna().any():
            bad.append(f"{int(rt.isna().sum())} realized_trades.csv rows have no P&L")
        elif abs(rt.sum() - realised) > tol:
            bad.append(f"realized_trades.csv P&L Rs{rt.sum():,.2f} != closed rows Rs{realised:,.2f}")
    if hist["mismatches"]:
        bad.append(hist["text"])
    if bad:
        res["check"] = "mismatch: " + "; ".join(bad)
        return res
    res.update(verified=True, cash=logged,
               check=(f"verified: logged cash Rs{logged:,.2f} = starting capital Rs{capital:,.2f} "
                      f"({first['DATE']}) - cost of {len(active)} active holdings Rs{cost:,.2f} "
                      f"+ realised P&L of {len(closed)} closed rows Rs{realised:,.2f}"
                      + ("; realized_trades.csv agrees" if realized is not None and len(realized) else "")
                      + f"; {hist['text']}"))
    return res


def _row_cost(r: dict) -> tuple[float, bool]:
    """(cost carried by a ledger row, fully recorded?)  New-format rows hold it in cost_basis (or only the
    recorded part in cost_known_part, with a cost_note); older rows hold notional + entry fee."""
    note = _s(r.get("cost_note"))
    cb, known = _f(r.get("cost_basis")), _f(r.get("cost_known_part"))
    size, ec = _f(r.get("size_inr")), _f(r.get("entry_cost_inr"))
    if cb == cb and not note:
        return cb, True
    if known == known:
        return known, False
    if cb == cb:
        return cb, not note
    if size == size:
        return size + (ec if ec == ec else 0.0), ec == ec and not note
    return 0.0, False


def cash_movements(pos: pd.DataFrame | None) -> pd.DataFrame:
    """Every cash movement the ledger rows record, dated:
      purchase    each original entry (open, stale, pending, closed or converted) on its entry date:
                  - notional; its entry fee as charged (entry_cost_inr with its basis). A fee that was never
                  written is listed as 'not recorded' (no amount) unless the row is closed, whose realised
                  P&L is the recorded net of the whole trade.
      settlement  each closed row on its exit date: + (cost carried + realised P&L) = sale proceeds net of
                  fees, a dividend or consideration paid, fraction sale proceeds.
    Conversions, receivables and fractions move no cash until paid; fee estimates are never movements.
    Columns: date (YYYYMMDD), symbol, hid, movement, amount, status ('recorded' / 'not recorded' /
    'provisional'), basis."""
    cols = ["date", "symbol", "hid", "movement", "amount", "status", "basis"]
    out = []
    if pos is None or pos.empty or "status" not in pos.columns:
        return pd.DataFrame(columns=cols)
    for r in pos.to_dict("records"):
        status = _s(r.get("status")).lower()
        sym, hid = _s(r.get("symbol")).upper(), _s(r.get("hid"))
        origin = _s(r.get("origin")) or "entry"
        size, ec, src = _f(r.get("size_inr")), _f(r.get("entry_cost_inr")), _s(r.get("entry_fee_source"))
        if origin == "entry" and status in ACTIVE + ("closed", "converted"):
            d = _s(r.get("entry_date")).split(".")[0].replace("-", "")[:8]
            out.append((d, sym, hid, "purchase (notional)", -size if size == size else float("nan"),
                        "recorded" if size == size else "not recorded", "size_inr"))
            cb, note = _f(r.get("cost_basis")), _s(r.get("cost_note"))
            if ec == ec and (ec != 0 or src):
                out.append((d, sym, hid, "entry fee", -ec, "recorded", src or "entry_cost_inr"))
            elif ec == ec:
                out.append((d, sym, hid, "entry fee", float("nan"), "not recorded", "0 with no stated basis"))
            elif cb == cb and not note and size == size and _s(r.get("kind")):
                pass                     # opened by the current ledger before fees had a column: size_inr = cost
            elif status not in ("closed",):
                out.append((d, sym, hid, "entry fee", float("nan"), "not recorded", note or "entry_cost_inr blank"))
        if status == "closed":
            d = _s(r.get("exit_date")).split(".")[0].replace("-", "")[:8]
            pnl = _f(r.get("realized_pnl_inr"))
            # the cost here is the one its purchase leg used, so the pair nets to the recorded realised P&L
            cost = _row_cost(r)[0]
            prov, note = _s(r.get("provisional")), _s(r.get("cost_note"))
            st = ("provisional" if prov else "recorded") if pnl == pnl and not note else "not recorded"
            out.append((d, sym, hid, f"settlement ({_s(r.get('exit_reason')) or 'closed'})",
                        cost + pnl if pnl == pnl else float("nan"), st,
                        prov or note or ("cost + realised P&L" if pnl == pnl else "realised P&L missing")))
    return pd.DataFrame(out, columns=cols)


def cash_history(pos: pd.DataFrame | None, equity_log: pd.DataFrame | None) -> dict:
    """Rebuild the logged CASH of every equity-log date from the dated movements (cash_movements) and the
    starting capital (first row). A movement with no recorded amount makes the dates from its date on
    inconclusive (they are not compared). Returns {'dates', 'compared', 'mismatches' [(date, logged, from
    movements)], 'inconclusive_from', 'text'}."""
    res = {"dates": 0, "compared": 0, "mismatches": [], "inconclusive_from": "", "text": ""}
    if equity_log is None or equity_log.empty or "CASH" not in equity_log.columns:
        res["text"] = "cash history: no equity log"
        return res
    el = equity_log.assign(_d=equity_log["DATE"].astype(str).str.replace("-", "").str[:8]).sort_values("_d")
    mv = cash_movements(pos)
    unknown = mv[mv["amount"].isna()]
    res["inconclusive_from"] = unknown["date"].min() if len(unknown) else ""
    capital = _f(el.iloc[0]["CASH"])
    res["dates"] = len(el)
    for d, logged in zip(el["_d"], el["CASH"].map(_f)):
        if res["inconclusive_from"] and d >= res["inconclusive_from"]:
            continue
        upto = mv[mv["date"] <= d]
        expected = capital + upto["amount"].sum()
        res["compared"] += 1
        if abs(expected - logged) > max(CASH_TOL, 0.01 * len(upto)):
            res["mismatches"].append((d, logged, round(expected, 2)))
    if res["mismatches"]:
        d, logged, exp = res["mismatches"][0]
        res["text"] = (f"cash history: the logged cash differs from the recorded movements on {len(res['mismatches'])} "
                       f"of {res['compared']} dates (first {d}: logged Rs{logged:,.2f}, movements Rs{exp:,.2f})")
    elif res["inconclusive_from"]:
        res["text"] = (f"cash history: matches the recorded movements on all {res['compared']} dates before "
                       f"{res['inconclusive_from']}; from then on a movement has no recorded amount")
    else:
        res["text"] = f"cash history: the logged cash matches the recorded movements on all {res['compared']} dates"
    return res


def entry_fee_estimates(pos: pd.DataFrame | None, path=FEE_ESTIMATES) -> pd.DataFrame:
    """Fees estimated with today's cost model for entries the paper ledger charged no fee, next to the fee
    actually charged, for display only. Nothing reads these estimates into cash, cost or P&L."""
    cols = ["symbol", "entry_date", "hid", "status", "fee_charged", "fee_charged_basis", "estimate", "label"]
    p = Path(path) if path else None
    if p is None or not p.exists() or pos is None or pos.empty:
        return pd.DataFrame(columns=cols)
    est = pd.read_csv(p, dtype=str).fillna("")
    origin = pos["origin"].map(_s) if "origin" in pos.columns else pd.Series("", index=pos.index)
    out = []
    for e in est.to_dict("records"):
        ed = e["ENTRY_DATE"].replace("-", "")[:8]
        m = pos[(pos["symbol"].astype(str).str.upper() == e["SYMBOL"].upper())
                & (pos["entry_date"].astype(str).str.replace("-", "").str[:8] == ed) & origin.isin(["", "entry"])]
        for r in m.to_dict("records"):
            out.append((e["SYMBOL"], ed, _s(r.get("hid")), _s(r.get("status")), _f(r.get("entry_cost_inr")),
                        _s(r.get("entry_fee_source")), _f(e["ESTIMATED_ENTRY_FEE_INR"]), ESTIMATE_LABEL))
    return pd.DataFrame(out, columns=cols)


def summary(pos: pd.DataFrame, equity_log: pd.DataFrame | None = None, last_prices: dict | None = None,
            realized: pd.DataFrame | None = None) -> dict:
    """Separately: share exposure, owed amounts, verified cash, marked equity and slots."""
    rows = classify(pos, last_prices)
    new = is_new_format(pos) if pos is not None and not pos.empty else False
    share = rows[rows.state.isin(SHARE_STATES)]
    no_mark = share[share["mark"].isna()]
    out = {"format": "new" if new else "old", "n_active": len(rows),
           "counts": rows.state.value_counts().to_dict(),
           "share_exposure": float(share.exposure.sum()),
           "share_exposure_by_state": share.groupby("state").exposure.sum().round(2).to_dict(),
           "owed": float(rows.owed.sum()), "owed_by_state": rows[rows.owed > 0].groupby("state").owed.sum().round(2).to_dict(),
           "slots": int(rows.slot.astype(bool).sum()),
           "held_symbols": sorted(set(rows.loc[rows.held.astype(bool), "symbol"])),
           "unpriced": rows[rows.state == UNPRICED].symbol.tolist(), "no_valid_mark": no_mark.symbol.tolist(),
           "fractions": rows[rows.state.isin([FRACTION, FRACTION_OWED])].symbol.tolist(), "undefended": undefended(rows),
           "provisional": ([f"{_s(r['symbol'])} ({_s(r['status'])})" for r in pos.to_dict("records")
                            if _s(r.get("provisional")) and _s(r.get("status")).lower() in ACTIVE]
                           if pos is not None and "provisional" in pos.columns else []),
           "available_cash": None, "cash_verified": False, "cash_as_of": "", "cash_check": "", "marked_equity": None,
           "ledger_equity": None, "cash_history": None}
    v = verify_cash(pos, equity_log, realized)
    out.update(cash_as_of=v["as_of"], cash_check=v["check"], cash_history=v["history"])
    if equity_log is not None and len(equity_log) and "EQUITY" in equity_log.columns:
        e = equity_log.assign(_d=equity_log["DATE"].astype(str).str.replace("-", "")).sort_values("_d").iloc[-1]
        out["ledger_equity"] = _f(e["EQUITY"]) if _f(e["EQUITY"]) == _f(e["EQUITY"]) else None
    if v["verified"] and new and out["ledger_equity"] is not None:
        # positions.csv must also carry the marks that produced the logged EQUITY (same run)
        own = classify(pos, None)
        own_share = own[own.state.isin(SHARE_STATES)].exposure.sum()
        recon = v["cash"] + own_share + own.owed.sum()
        if abs(recon - out["ledger_equity"]) > max(CASH_TOL, 1e-6 * abs(out["ledger_equity"])):
            out["cash_check"] = (f"mismatch: cash Rs{v['cash']:,.2f} + shares Rs{own_share:,.2f} + owed "
                                 f"Rs{own.owed.sum():,.2f} = Rs{recon:,.2f} != logged equity Rs{out['ledger_equity']:,.2f} "
                                 "(positions.csv and equity_log.csv are not from the same run)")
            return out
    if v["verified"]:
        out.update(available_cash=v["cash"], cash_verified=True)
        if no_mark.empty and not out["undefended"]:
            out["marked_equity"] = v["cash"] + out["share_exposure"] + out["owed"]
    return out


def describe(r) -> str:
    """One labelled line for a classified row (namedtuple or Series)."""
    if r.state in OWED_STATES:
        return f"{r.label}: Rs{r.owed:,.2f} ({r.detail})"
    if r.state == FRACTION or r.mark != r.mark:
        return f"{r.label}: {r.qty:g} sh" + (f"; {r.detail}" if r.detail else "")
    return (f"{r.label}: {r.qty:g} sh @ Rs{r.mark:,.2f} ({r.mark_basis}{', ' + r.mark_date if r.mark_date else ''})"
            + (f"; {r.detail}" if r.detail else ""))


def holdings_line(rows: pd.DataFrame, max_names: int = 8) -> list[str]:
    """Short text for messages: open names first, then every other active state, labelled."""
    if rows is None or rows.empty:
        return []
    lines = []
    op = rows[rows.state == OPEN]
    if len(op):
        names = op.symbol.tolist()
        lines.append(f"open ({len(op)}): " + ", ".join(names[:max_names]) + (f" + {len(names) - max_names} more" if len(names) > max_names else ""))
    other = rows[rows.state != OPEN]
    for st in (STALE, LOCKED, PENDING, UNPRICED, CONSIDERATION_OWED, DIV_OWED, FRACTION_OWED, FRACTION):
        g = other[other.state == st]
        if g.empty:
            continue
        if st in OWED_STATES:
            lines.append(f"{LABEL[st]} ({len(g)}): Rs{g.owed.sum():,.0f} - " + ", ".join(g.symbol.tolist()[:max_names]))
        else:
            lines.append(f"{LABEL[st]} ({len(g)}): " + ", ".join(g.symbol.tolist()[:max_names]))
    return lines
