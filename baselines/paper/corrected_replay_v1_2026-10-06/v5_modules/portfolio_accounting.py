"""
portfolio_accounting.py
=======================
Share-based portfolio accounting shared by the backtest (backtest_regime.py)
and the paper ledger (paper_trade_manager.py), so both apply identical rules.

Holdings are kept in real share counts at real (unadjusted) prices.

States
  open     tradable; stop orders active
  pending  an entitlement that cannot be traded yet: successor shares before
           their first tradable session, cash not yet paid, or a fractional
           entitlement whose treatment is not documented. No stops, no sales.
  stale    no print and no verified explanation (or verified type but
           unverified terms). Untradeable; capital and the portfolio slot stay
           occupied. Valued at its last traded price, reported separately so
           recovery scenarios can be shown.
  closed   sold, converted or paid out

Per session d, in this order
  1. events:   official split/bonus ratios change share counts (stops follow);
               price-derived factors are NEVER used for share counts. A held name
               with an unverified corporate action becomes stale.
               merger_stock (verified)  -> pending successor shares (+ pending fraction)
               merger_cash/delisting_cash (verified) -> pending cash. An exit offer
                                           with a TENDER_DATE runs: shares locked (pending,
                                           valued at their last traded price) from the day
                                           after the last trade -> cash receivable from
                                           the tender date -> spendable cash on the pay
                                           date; each date's basis is logged
               demerger (verified)      -> parent continues; pending successor shares
               cash_dividend (verified) -> shares continue, tradable; stops rescaled by
                                           the dividend factor; a separate pending cash
                                           receivable (gross) that is never spendable
                                           before its documented pay date
  2. pending:  entitlements whose TRADABLE_DATE has arrived AND that print on d
               become open (merger successor, managed by its converted stops) or
               are sold at that first actually tradable OPEN with costs
               (demerger successor, policy "sell"). Cash is credited on its pay date.
               A fraction stays outside cash and equity until its event records the
               sale proceeds (then an amount owed) and is credited on its pay date.
  3. exits:    stops are checked against d's OPEN and LOW using the stop set at
               the previous close. OPEN <= stop -> filled at the OPEN (gap);
               LOW <= stop -> filled at the stop.
  -- caller may open new positions at d's OPEN here --
  4. close:    new positions are checked against their initial stop on the
               entry day; then the trailing stop is raised using d's close and
               ATR - and, where the rule in force on d has one, the lowest low of
               the last N sessions - to apply from the next session. A stop is
               never lowered, including when the rule changes. Valuation at the close.
               ATR, the rolling low and every stop are in d's as-traded (raw) prices.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import pandas as pd

OPEN, PENDING, STALE, CLOSED = "open", "pending", "stale", "closed"
# Life-cycle events after which the old security no longer trades. Only the
# verified, fully specified types (merger_stock / merger_cash / delisting_cash)
# convert a holding; the generic ones mark it stale until their terms are verified.
TERMINAL_TYPES = ("merger", "merger_stock", "merger_cash", "delisting", "delisting_cash", "capital_reduction")


@dataclass
class Holding:
    hid: str
    symbol: str
    qty: float
    state: str = OPEN
    entry_date: str = ""          # YYYYMMDD
    entry_price: float = 0.0      # per share, current share basis
    cost_basis: float = 0.0       # rupees paid incl. entry costs (carried through conversions)
    cost_note: str = ""           # non-empty: cost_basis is only the recorded part (e.g. an entry cost that was
                                  # never recorded); carried to every row derived from this holding
    provisional: str = ""         # non-empty: stop levels (and so any exit) come from a replay whose rules are
                                  # undecided; carried to the successor shares and to the exit trade
    stop: float = 0.0             # stop that applies from the NEXT session (current share basis)
    stop0: float = 0.0
    hi: float = 0.0               # highest close since entry
    last_mark: float = 0.0
    last_mark_date: str = ""
    kind: str = "shares"          # shares | cash | fraction
    tradable_from: str = ""
    cash_due: float = 0.0
    cash_date: str = ""
    origin: str = "entry"
    on_tradable: str = "hold"     # hold | sell
    missing: int = 0
    stale_reason: str = ""
    days: int = 0
    regime: str = ""
    indicative_value: float = 0.0  # price-derived estimate for unpriced pending shares (reporting only)
    applied: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["applied"] = ";".join(self.applied)
        d["notes"] = " | ".join(self.notes)
        return d


class Book:
    """A portfolio of Holdings plus cash, advanced one session at a time.

    bar(symbol, d)  -> dict(open, high, low, close) or None  (raw prices, any tradable series)
    atr(symbol, d)  -> ATR(14) in d's raw price terms, or None
    cost(notional, side, symbol, d) -> rupees of charges + slippage for one side
    stop_rules      optional ((from_date YYYYMMDD, chandelier multiple, rolling-low sessions or None,
                    time stop in calendar days since entry or None), ...): the exit rules in force on each
                    session (the latest rule whose from_date <= d). Without it every session uses
                    `chandelier`, no rolling low and the session-count `time_stop` (the backtest).
    low(symbol, d, n) -> lowest LOW of the n sessions up to and including d, in d's as-traded prices,
                    or None (needed only for a rule with a rolling low)
    """

    def __init__(self, cash: float, bar, atr, cost, share_events: pd.DataFrame | None = None,
                 other_events: pd.DataFrame | None = None, security_events: pd.DataFrame | None = None,
                 chandelier: float = 4.0, stale_after: int = 3, time_stop: int | None = None,
                 stop_rules: tuple | None = None, low=None):
        self.cash = float(cash)
        self.bar, self.atr, self.cost = bar, atr, cost
        self.k = chandelier
        self.stop_rules = tuple(sorted(stop_rules)) if stop_rules else None
        self.low = low
        self.stale_after = stale_after
        self.time_stop = time_stop
        self.holdings: list[Holding] = []
        self.trades: list[dict] = []
        self.log: list[dict] = []
        self._seq = 0
        self.share_ev = _index(share_events, "EX_DATE")
        self.other_ev = _index(other_events, "EX_DATE")
        self.sec = security_events if security_events is not None else pd.DataFrame()
        self.sec_by_symbol = {s: g for s, g in self.sec.groupby("SYMBOL")} if len(self.sec) else {}
        self.sec_by_id = ({e.EVENT_ID: e for e in self.sec.itertuples(index=False)}
                          if len(self.sec) and "EVENT_ID" in self.sec.columns else {})

    # ------------------------------------------------------------ helpers
    tag: str = ""          # e.g. the run date, so ids stay unique across ledger runs

    def new_id(self, prefix: str = "H") -> str:
        self._seq += 1
        return f"{prefix}{self.tag}{self._seq}"

    def active(self) -> list[Holding]:
        return [h for h in self.holdings if h.state != CLOSED]

    def stop_rule(self, d: str) -> tuple[float, int | None, int | None]:
        """(chandelier multiple, rolling-low sessions or None, calendar-day time stop or None) in force on d."""
        if not self.stop_rules:
            return self.k, None, None
        rule = self.stop_rules[0]
        for r in self.stop_rules:
            if r[0] <= d:
                rule = r
        return float(rule[1]), rule[2], (rule[3] if len(rule) > 3 else None)

    @staticmethod
    def is_dividend_receivable(h: Holding) -> bool:
        """A pending cash dividend: money owed on shares held at the ex-date, not ownership of shares."""
        return h.kind == "cash" and h.origin.startswith("dividend:")

    def slots_used(self) -> int:
        """open + stale + pending shares/cash occupy capacity. Fractions and dividend
        receivables do not: the shares a dividend was paid on hold (or held) that slot."""
        return sum(1 for h in self.active() if h.kind != "fraction" and not self.is_dividend_receivable(h))

    def holds(self, symbol: str) -> bool:
        """True while shares of `symbol` are open, stale, pending or locked, or their cash consideration
        is pending. A dividend receivable is not a holding: it never blocks buying the stock again."""
        return any(h.symbol == symbol and not self.is_dividend_receivable(h) for h in self.active())

    def _note(self, h: Holding, d: str, text: str) -> None:
        h.notes.append(f"{d}: {text}")
        self.log.append({"date": d, "hid": h.hid, "symbol": h.symbol, "event": text})

    def _close(self, h: Holding, d: str, px: float, reason: str, regime: str = "") -> None:
        gross = px * h.qty
        c = self.cost(gross, "sell", h.symbol, d)
        proceeds = gross - c
        self.cash += proceeds
        self.trades.append({
            "hid": h.hid, "symbol": h.symbol, "origin": h.origin, "entry_date": h.entry_date, "exit_date": d,
            "regime": h.regime, "entry": h.entry_price, "exit": float(px), "stop0": h.stop0, "qty": h.qty,
            "reason": reason, "days": h.days, "cost_basis": h.cost_basis, "proceeds": proceeds,
            "pnl": proceeds - h.cost_basis, "cost_note": h.cost_note, "provisional": h.provisional,
            "ret": (px / h.entry_price - 1) if h.entry_price else float("nan"),
            "r_mult": ((px - h.entry_price) / (h.entry_price - h.stop0)) if h.entry_price > h.stop0 > 0 else float("nan"),
        })
        h.state = CLOSED
        self._note(h, d, f"closed {reason} at {px:.4f} x {h.qty:g}")

    # ------------------------------------------------------------ entry
    def open_position(self, symbol: str, d: str, qty: int, fill: float, atr_prev: float | None,
                      regime: str = "", stop_mult: float = 2.0, stop: float | None = None) -> Holding | None:
        if qty < 1 or not fill or fill <= 0:
            return None
        outlay = qty * fill
        c = self.cost(outlay, "buy", symbol, d)
        if outlay + c > self.cash:
            qty = int(self.cash / (fill * (1 + c / outlay)))
            if qty < 1:
                return None
            outlay = qty * fill
            c = self.cost(outlay, "buy", symbol, d)
        self.cash -= outlay + c
        if stop is None:
            stop = fill - stop_mult * (atr_prev or 0.0)
        h = Holding(hid=self.new_id(), symbol=symbol, qty=float(qty), entry_date=d, entry_price=float(fill),
                    cost_basis=outlay + c, stop=stop, stop0=stop, hi=float(fill), last_mark=float(fill),
                    last_mark_date=d, regime=regime)
        self.holdings.append(h)
        return h

    # ------------------------------------------------------------ 1. events
    def apply_events(self, d: str) -> None:
        for h in list(self.active()):
            if h.kind != "shares":
                continue
            # official share-count changes
            for e in self.share_ev.get(d, {}).get(h.symbol, []):
                key = f"share:{e['EX_DATE']}:{e['KIND']}"
                if key in h.applied:
                    continue
                f = float(e["FACTOR"])
                h.qty = h.qty / f
                for a in ("entry_price", "stop", "stop0", "hi", "last_mark"):
                    setattr(h, a, getattr(h, a) * f)
                h.applied.append(key)
                self._note(h, d, f"{e['KIND']} (official ratio, factor {f:.6f}): qty -> {h.qty:g}")
            sev = self.sec_by_symbol.get(h.symbol)
            covered = set()
            if sev is not None:
                for e in sev.itertuples(index=False):
                    if h.state == CLOSED:      # converted/paid out by an earlier event: nothing more applies
                        break
                    key = f"sec:{e.EVENT_ID}"
                    if key in h.applied:
                        continue
                    if e.TYPE == "demerger" and e.EX_DATE == d:
                        covered.add(d)
                        self._demerger(h, e, d)
                    elif e.TYPE == "cash_dividend" and e.EX_DATE == d and e.STATUS == "verified":
                        covered.add(d)
                        self._dividend(h, e, d)
                    elif e.TYPE in TERMINAL_TYPES and e.LAST_TRADE_DATE and d > e.LAST_TRADE_DATE:
                        self._terminal(h, e, d)
            # price-derived corporate action on a held name with no verified security record
            for e in self.other_ev.get(d, {}).get(h.symbol, []):
                if d in covered or h.state != OPEN:
                    continue
                key = f"unverified:{e['EX_DATE']}:{e['KIND']}"
                if key in h.applied:
                    continue
                h.applied.append(key)
                h.state = STALE
                h.stale_reason = (f"unverified corporate action ({e['KIND']}, price-derived factor "
                                  f"{float(e['FACTOR']):.4f}) on {d}")
                self._note(h, d, f"stale: {h.stale_reason}")

    def _demerger(self, h: Holding, e, d: str) -> None:
        h.applied.append(f"sec:{e.EVENT_ID}")
        f = float(e.PRICE_FACTOR) if pd.notna(e.PRICE_FACTOR) else None
        if f:
            for a in ("stop", "stop0", "hi"):
                setattr(h, a, getattr(h, a) * f)
            self._note(h, d, f"demerger {e.EVENT_ID}: parent shares unchanged; stops rescaled x{f:.6f} "
                             f"(price-derived estimate, for stop continuity only)")
        r = float(e.RATIO_NUM) / float(e.RATIO_DEN)
        whole = math.floor(h.qty * r + 1e-9)
        frac = h.qty * r - whole
        pre = h.last_mark
        if whole > 0:
            child = Holding(hid=self.new_id("D"), symbol=e.SUCCESSOR, qty=float(whole), state=PENDING,
                            entry_date=d, entry_price=0.0, cost_basis=0.0, kind="shares",
                            tradable_from=e.TRADABLE_DATE, origin=f"demerger:{e.EVENT_ID}", on_tradable="sell",
                            regime=h.regime, provisional=h.provisional,
                            indicative_value=(whole / r) * pre * (1 - f) if f else 0.0)
            child.applied.append(f"sec:{e.EVENT_ID}")
            self.holdings.append(child)
            self._note(child, d, f"pending entitlement {whole:g} {e.SUCCESSOR} from {h.symbol} "
                                 f"(tradable from {e.TRADABLE_DATE})")
        if frac > 1e-9:
            self._fraction(h, e, d, frac, e.SUCCESSOR, 0.0)

    def _dividend(self, h: Holding, e, d: str) -> None:
        """Verified cash dividend: the shares stay open and tradable; the gross amount
        becomes a separate pending receivable, credited to cash only on a documented
        pay date. Stop levels, the trailing high and the entry price are rescaled by
        the dividend factor (prev close - dividend) / prev close so the stop keeps its
        distance from the ex-dividend price; the cost basis is split between the
        shares (factor) and the receivable (1 - factor), so each leg's P&L is on the
        same basis."""
        h.applied.append(f"sec:{e.EVENT_ID}")
        per = float(e.CASH_PER_SHARE)
        due = h.qty * per
        f = float(e.PRICE_FACTOR) if pd.notna(e.PRICE_FACTOR) and 0 < float(e.PRICE_FACTOR) < 1 else None
        pay = e.CASH_PAY_DATE or ""
        basis = getattr(e, "PAY_DATE_BASIS", "") or ("" if pay else "pay date not established")
        rc = Holding(hid=self.new_id("V"), symbol=h.symbol, qty=h.qty, state=PENDING, entry_date=d, kind="cash",
                     cash_due=due, cash_date=pay, origin=f"dividend:{e.EVENT_ID}", regime=h.regime,
                     cost_basis=h.cost_basis * (1 - f) if f else 0.0,
                     cost_note=h.cost_note if f else "",     # a share of an incomplete basis is incomplete too
                     provisional=h.provisional)
        rc.applied.append(f"sec:{e.EVENT_ID}")
        self.holdings.append(rc)
        if f:
            h.cost_basis -= rc.cost_basis
            for a in ("entry_price", "stop", "stop0", "hi"):
                setattr(h, a, getattr(h, a) * f)
        self._note(h, d, f"cash dividend {e.EVENT_ID}: Rs{per:g}/share gross on {h.qty:g} shares; shares continue"
                         + (f"; stops rescaled x{f:.6f} (dividend factor)" if f else ""))
        self._note(rc, d, f"pending dividend Rs{due:,.2f} gross ({e.EVENT_ID}); "
                          + (f"pay date {pay} [{basis}]" if pay else f"kept out of spendable cash: {basis}"))

    def _terminal(self, h: Holding, e, d: str) -> None:
        tender = getattr(e, "TENDER_DATE", "") or ""
        if (e.STATUS == "verified" and e.TYPE in ("merger_cash", "delisting_cash") and tender and d < tender
                and pd.notna(e.CASH_PER_SHARE)):
            # exit offer: trading has stopped but the shares cannot be tendered yet
            lock = f"sec:{e.EVENT_ID}:locked"
            if lock not in h.applied:
                h.applied.append(lock)
                h.state, h.missing = PENDING, 0
                basis = getattr(e, "TENDER_DATE_BASIS", "") or ""
                self._note(h, d, f"locked: no trading after {e.LAST_TRADE_DATE}; shares awaiting tender on {tender}"
                                 + (f" [{basis}]" if basis else "") + f"; reference value = last traded price "
                                 f"{h.last_mark:.4f} ({e.EVENT_ID})")
            return
        h.applied.append(f"sec:{e.EVENT_ID}")
        if e.STATUS != "verified" or e.TYPE not in ("merger_stock", "merger_cash", "delisting_cash"):
            h.state = STALE
            h.stale_reason = f"{e.TYPE} {e.EVENT_ID}: terms not verified"
            self._note(h, d, f"stale: {h.stale_reason}")
            return
        if e.TYPE == "merger_stock":
            r = float(e.RATIO_NUM) / float(e.RATIO_DEN)
            entitled = h.qty * r
            whole = math.floor(entitled + 1e-9)
            frac = entitled - whole
            if whole > 0:
                share = whole / entitled
                succ = Holding(hid=self.new_id("M"), symbol=e.SUCCESSOR, qty=float(whole), state=PENDING,
                               entry_date=h.entry_date, entry_price=h.entry_price / r,
                               cost_basis=h.cost_basis * share, stop=h.stop / r, stop0=h.stop0 / r, hi=h.hi / r,
                               last_mark=h.last_mark / r, last_mark_date=h.last_mark_date, kind="shares",
                               tradable_from=e.TRADABLE_DATE, origin=f"merger:{e.EVENT_ID}", on_tradable="hold",
                               days=h.days, regime=h.regime, cost_note=h.cost_note, provisional=h.provisional)
                succ.applied.append(f"sec:{e.EVENT_ID}")
                self.holdings.append(succ)
                self._note(succ, d, f"pending: {whole:g} {e.SUCCESSOR} for {h.qty:g} {h.symbol} at "
                                    f"{int(e.RATIO_NUM)}:{int(e.RATIO_DEN)} (tradable from {e.TRADABLE_DATE}); "
                                    f"stops converted by the official ratio")
            if frac > 1e-9:
                self._fraction(h, e, d, frac, e.SUCCESSOR, h.cost_basis * (frac / entitled))
            h.state = CLOSED
            self._note(h, d, f"converted under {e.EVENT_ID} (not a sale; cost basis carried)")
        else:   # merger_cash / delisting_cash with documented consideration
            if pd.isna(e.CASH_PER_SHARE):
                h.state = STALE
                h.stale_reason = f"{e.TYPE} {e.EVENT_ID}: cash consideration not documented"
                self._note(h, d, f"stale: {h.stale_reason}")
                return
            due = h.qty * float(e.CASH_PER_SHARE)
            h.kind, h.state, h.cash_due, h.cash_date = "cash", PENDING, due, e.CASH_PAY_DATE
            basis = getattr(e, "PAY_DATE_BASIS", "") or ""
            what = (f"tendered {tender} [{getattr(e, 'TENDER_DATE_BASIS', '') or 'tender date'}]; receivable"
                    if tender else "pending cash")
            self._note(h, d, f"{what} Rs{due:,.2f} ({e.TYPE} {e.EVENT_ID}, Rs{float(e.CASH_PER_SHARE):g}/share); "
                             f"spendable from {e.CASH_PAY_DATE or 'an undocumented date'}"
                             + (f" [{basis}]" if basis else ""))

    def _fraction(self, h: Holding, e, d: str, frac: float, successor: str, cost: float) -> None:
        fh = Holding(hid=self.new_id("F"), symbol=successor, qty=frac, state=PENDING, entry_date=d,
                     cost_basis=cost, kind="fraction", tradable_from="", origin=f"fraction:{e.EVENT_ID}",
                     regime=h.regime, cost_note=h.cost_note if cost else "", provisional=h.provisional)
        fh.applied.append(f"sec:{e.EVENT_ID}")
        self.holdings.append(fh)
        self._note(fh, d, f"fractional entitlement {frac:.4f} {successor} kept pending ({e.FRACTION_TREATMENT})")

    def _settle_fraction(self, h: Holding, d: str) -> None:
        """A fractional entitlement is never cash on its own. Only when its event records the sale proceeds
        (FRACTION_CASH_PER_SHARE: net rupees per whole successor share; FRACTION_ROUNDING 'ceil_rupee' when the
        scheme rounds each holder's amount up to the next rupee) does it become an amount owed - still not
        cash - and only on the documented FRACTION_PAY_DATE is that amount credited to cash."""
        e = self.sec_by_id.get(h.origin.split(":", 1)[1]) if ":" in h.origin else None
        raw = getattr(e, "FRACTION_CASH_PER_SHARE", None) if e is not None else None
        per = pd.to_numeric(raw, errors="coerce") if raw is not None else float("nan")
        if pd.isna(per) or float(per) < 0:
            return                                  # proceeds not disclosed: stays a pending entitlement
        due = h.qty * float(per)
        if str(getattr(e, "FRACTION_ROUNDING", "") or "").strip() == "ceil_rupee":
            due = float(math.ceil(due - 1e-9))
        pay = str(getattr(e, "FRACTION_PAY_DATE", "") or "").strip()
        if (due, pay) != (h.cash_due, h.cash_date):
            h.cash_due, h.cash_date = due, pay
            self._note(h, d, f"fraction sale proceeds documented: Rs{due:,.2f} owed for {h.qty:g} {h.symbol} "
                             f"({e.EVENT_ID}); " + (f"pay date {pay}" if pay else "not cash until a pay date is documented"))
        if pay and d >= pay:
            self.cash += due
            self.trades.append({"hid": h.hid, "symbol": h.symbol, "origin": h.origin, "entry_date": h.entry_date,
                                "exit_date": d, "regime": h.regime, "entry": h.entry_price, "exit": float("nan"),
                                "stop0": h.stop0, "qty": h.qty, "days": h.days, "reason": "FRACTION_CASH",
                                "cost_basis": h.cost_basis, "proceeds": due, "pnl": due - h.cost_basis,
                                "ret": float("nan"), "r_mult": float("nan"), "cost_note": h.cost_note,
                                "provisional": h.provisional})
            h.state = CLOSED
            self._note(h, d, f"fraction sale proceeds Rs{due:,.2f} received")

    # ------------------------------------------------------------ 2. pending
    def settle_pending(self, d: str) -> None:
        for h in list(self.active()):
            if h.state != PENDING:
                continue
            if h.kind == "cash":
                if h.cash_date and d >= h.cash_date:
                    self.cash += h.cash_due
                    self.trades.append({"hid": h.hid, "symbol": h.symbol, "origin": h.origin,
                                        "entry_date": h.entry_date, "exit_date": d, "regime": h.regime,
                                        "entry": h.entry_price, "exit": float("nan"), "stop0": h.stop0,
                                        "qty": h.qty, "days": h.days,
                                        "reason": "CASH_DIVIDEND" if h.origin.startswith("dividend:") else "CASH_CONSIDERATION",
                                        "cost_basis": h.cost_basis, "proceeds": h.cash_due,
                                        "pnl": h.cash_due - h.cost_basis, "ret": float("nan"), "r_mult": float("nan"),
                                        "cost_note": h.cost_note, "provisional": h.provisional})
                    h.state = CLOSED
                    self._note(h, d, f"cash Rs{h.cash_due:,.2f} received")
                continue
            if h.kind == "fraction":
                self._settle_fraction(h, d)
                continue
            if h.kind != "shares" or not h.tradable_from or d < h.tradable_from:
                continue
            b = self.bar(h.symbol, d)
            if b is None or not b.get("open"):
                continue      # tradable in principle, but no print yet
            if h.on_tradable == "sell":
                self._close(h, d, float(b["open"]), "ENTITLEMENT_SALE_FIRST_TRADABLE_OPEN")
            else:
                h.state = OPEN
                self._note(h, d, f"entitlement now tradable; first print open {float(b['open']):.4f}")

    # ------------------------------------------------------------ 3. exits at/after the open
    def check_exits(self, d: str) -> None:
        for h in list(self.active()):
            if h.state not in (OPEN, STALE) or h.kind != "shares" or h.entry_date == d:
                continue
            b = self.bar(h.symbol, d)
            if b is None:
                h.missing += 1
                if h.state == OPEN and h.missing >= self.stale_after:
                    h.state = STALE
                    h.stale_reason = h.stale_reason or self._explain(h.symbol) or f"no print since {h.last_mark_date}"
                    self._note(h, d, f"stale: {h.stale_reason}")
                continue
            if h.state == STALE:
                if h.stale_reason.startswith("unverified corporate action") or ": terms not verified" in h.stale_reason:
                    continue  # stays stale until the event is verified
                h.state = OPEN
                self._note(h, d, f"prints again after {h.missing} missing sessions; open {float(b['open']):.4f}")
            h.missing = 0
            h.days += 1
            o, lo = float(b["open"]), float(b["low"])
            if o <= h.stop:
                self._close(h, d, o, "STOP_GAP_AT_OPEN")
            elif lo <= h.stop:
                self._close(h, d, h.stop, "STOP_LOSS")
            elif self.time_stop and h.days >= self.time_stop:
                self._close(h, d, float(b["close"]), "TIME_STOP")
            elif (cal := self.stop_rule(d)[2]) and (pd.Timestamp(d) - pd.Timestamp(h.entry_date)).days >= cal:
                self._close(h, d, float(b["close"]), "TIME_STOP")      # calendar days since entry, at the close

    def _explain(self, symbol: str) -> str:
        sev = self.sec_by_symbol.get(symbol)
        if sev is not None:      # only records that end or suspend trading can explain missing prints
            sev = sev[~sev["TYPE"].isin(["cash_dividend", "demerger"])]
        if sev is None or sev.empty:
            return ""
        e = sev.iloc[-1]
        return f"{e['TYPE']} ({e['STATUS']}) {e['EVENT_ID']}"

    # ------------------------------------------------------------ 4. close
    def end_of_day(self, d: str) -> None:
        for h in list(self.active()):
            if h.state != OPEN or h.kind != "shares":
                continue
            b = self.bar(h.symbol, d)
            if b is None:
                continue
            if h.entry_date == d:
                h.missing = 0
                if float(b["low"]) <= h.stop:          # initial stop is live from the entry fill
                    self._close(h, d, h.stop, "STOP_LOSS_ENTRY_DAY")
                    continue
            c = float(b["close"])
            h.hi = max(h.hi, c)
            k, lookback, _ = self.stop_rule(d)
            a = self.atr(h.symbol, d)
            if a and a > 0:
                h.stop = max(h.stop, h.hi - k * a)          # never lowered, whatever rule applies
            if lookback and self.low is not None:
                lo = self.low(h.symbol, d, lookback)
                if lo and lo > 0:
                    h.stop = max(h.stop, lo)
            h.last_mark, h.last_mark_date = c, d
        for h in self.active():
            if h.state in (PENDING, STALE) and h.kind in ("shares", "fraction"):
                b = self.bar(h.symbol, d)
                if b is not None and h.state == PENDING:
                    h.last_mark, h.last_mark_date = float(b["close"]), d

    def value(self, d: str) -> dict:
        """Valuation components at d's close."""
        comp = {"date": d, "cash": self.cash, "open_mtm": 0.0, "pending_ref": 0.0, "pending_unpriced_indicative": 0.0,
                "fraction_ref_unconfirmed": 0.0, "stale_mark": 0.0, "n_open": 0, "n_pending": 0, "n_stale": 0,
                "n_fraction": 0}
        for h in self.active():
            if h.kind == "fraction":
                comp["n_fraction"] += 1
                if h.cash_due > 0:                    # documented proceeds: owed (not cash) until paid
                    comp["pending_ref"] += h.cash_due
                elif h.last_mark_date:
                    comp["fraction_ref_unconfirmed"] += h.qty * h.last_mark
                continue
            if h.kind == "cash":
                comp["n_pending"] += 1
                comp["pending_ref"] += h.cash_due
                continue
            if h.state == OPEN:
                comp["n_open"] += 1
                comp["open_mtm"] += h.qty * h.last_mark
            elif h.state == PENDING:
                comp["n_pending"] += 1
                if h.last_mark_date:          # successor has a market print (these shares not yet tradable)
                    comp["pending_ref"] += h.qty * h.last_mark
                else:
                    comp["pending_unpriced_indicative"] += h.indicative_value
            elif h.state == STALE:
                comp["n_stale"] += 1
                comp["stale_mark"] += h.qty * h.last_mark
        comp["equity"] = comp["cash"] + comp["open_mtm"] + comp["pending_ref"] + comp["stale_mark"]
        comp["equity_stale_at_zero"] = comp["equity"] - comp["stale_mark"]
        comp["equity_tradable"] = comp["cash"] + comp["open_mtm"]
        return comp


def _index(ev: pd.DataFrame | None, col: str) -> dict:
    """{date: {symbol: [event dicts]}}"""
    out: dict = {}
    if ev is None or len(ev) == 0:
        return out
    for r in ev.to_dict("records"):
        out.setdefault(str(r[col]), {}).setdefault(r["SYMBOL"], []).append(r)
    return out
