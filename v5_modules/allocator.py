"""
allocator.py
============
Shared, deterministic sizing and exposure allocation for new entries.

One set of rules decides how many units a new idea may take, given a risk policy and the book it would
join. It is pure (no files, no prices, no clock) so the research risk engine, the backtest and, once parity
and portfolio-state checks pass, the paper ledger can all call the same code.

Connected today:   research_system/risk_engine.py (steps 10-12), backtest_regime.py (--entry-allocation shared)
Not connected:     paper_trade_manager.py (still sizes from the signal file's SIZE_INR)

Shared rules (docs/ARCHITECTURE.md, "Central risk engine" 10-12), checked in this order:
  risk_budget         the policy must be explicitly configured and every limit a positive number; the default
                      Policy() is unset, so nothing is approved until a budget is chosen
  book_view           the book must be complete: no listed problems, every exposure valued
  regime              the market regime must be known (it sets the gross cap)
  allocation_inputs   price, loss per unit (stop + gap allowance + costs, each stated), capital and exposure per
                      unit, sector, and 20-day traded value (cash) must all be present and valid
  duplicate           the symbol (or its underlying) is not already held or approved
  position_count      total and per-engine slots
  sizing              floor(loss budget x size multiplier / loss per unit), capped by max position %, ADV
                      participation, free capital, verified cash and free margin (derivatives)
  gross_exposure      gross cap = max gross % x the regime's share x capital; the size is cut to the room left.
                      This is the only place the market regime changes a size.
  net_exposure        net cap (reject)
  sector              sector concentration cap (reject)
Missing or invalid data rejects the request; it never loosens a limit or defaults to a value.

Requests are taken in a fixed order: higher priority first, then engine, symbol and key, so the result does
not depend on the order the engines produced them. Each approval is booked before the next request is sized.

Legacy rules (comparison only): legacy_allocate() is the V5 backtest's weekly allocation, unchanged:
  rank_order          the v5_signal_generator.size_positions formula (risk-parity on a 2 ATR stop, 10% cap, the
                      top-N list renormalised so a full book fits equity x regime gross), funded in rank order
                      within equity x regime gross - exposure held
  pro_rata_leftover   the original backtest rule (every pick scaled to share the leftover budget)
They apply the regime twice (the renormalisation and the budget), ignore gaps, costs, sectors, liquidity and
cash, and are not fail-closed. They are kept so the two behaviours can be compared, not to choose between them
on historical return.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

LEGACY_METHODS = ("rank_order", "pro_rata_leftover")
REGIME_GROSS = {"RISK_ON": 1.0, "RISK_NEU": 0.7, "RISK_OFF": 0.4}   # V5 backtested schedule (not a budget value)
UNKNOWN_SECTORS = ("", "UNKNOWN", "NAN", "NONE")
LOSS_TOLERANCE_INR = 0.01          # a stated loss per unit must equal its parts to the paisa


def _num(x) -> float | None:
    """A finite float, or None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pos(x) -> bool:
    v = _num(x)
    return v is not None and v > 0


# ---------------------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class Policy:
    """Risk limits. Every value defaults to unset: an unset policy approves nothing."""
    configured: bool = False
    capital_inr: float | None = None
    per_trade_max_loss_inr: float | None = None
    max_gross_exposure_pct: float | None = None
    max_net_exposure_pct: float | None = None
    max_position_pct: float | None = None
    max_sector_pct: float | None = None
    max_open_positions: int | None = None
    max_positions_by_engine: dict = field(default_factory=dict)
    max_participation_adv: float | None = None
    margin_inr: float | None = None               # derivatives only; unset or 0 = no derivative approvals
    regime_gross: dict = field(default_factory=lambda: dict(REGIME_GROSS))
    source: str = ""

    REQUIRED = ("capital_inr", "per_trade_max_loss_inr", "max_gross_exposure_pct", "max_net_exposure_pct",
                "max_position_pct", "max_sector_pct", "max_open_positions", "max_participation_adv")

    def problems(self) -> list[str]:
        out = [] if self.configured else ["policy not marked configured"]
        out += [f"{k} not set" for k in self.REQUIRED if not _pos(getattr(self, k))]
        if _pos(self.per_trade_max_loss_inr) and _pos(self.capital_inr) and \
                self.per_trade_max_loss_inr > self.capital_inr:
            out.append("per_trade_max_loss_inr exceeds capital_inr")
        return out

    def at_capital(self, capital: float) -> "Policy":
        """The same policy on another capital base: the per-trade loss budget keeps its ratio to capital and the
        percentage limits are unchanged (used by the backtest, which sizes from equity at the previous close)."""
        if not (_pos(capital) and _pos(self.capital_inr) and _pos(self.per_trade_max_loss_inr)):
            return replace(self, capital_inr=None)
        return replace(self, capital_inr=float(capital),
                       per_trade_max_loss_inr=self.per_trade_max_loss_inr * float(capital) / self.capital_inr)


def policy_from_risk_config(cfg: dict) -> Policy:
    """The policy described by a risk config (config/risk_config.example.toml layout). The example file ships
    unconfigured with zero budget values, so the policy it gives is unset."""
    acct, lim = cfg.get("account", {}), cfg.get("limits", {})
    return Policy(
        configured=bool(acct.get("configured", False)),
        capital_inr=acct.get("capital_inr"), per_trade_max_loss_inr=lim.get("per_trade_max_loss_inr"),
        max_gross_exposure_pct=lim.get("max_gross_exposure_pct"), max_net_exposure_pct=lim.get("max_net_exposure_pct"),
        max_position_pct=lim.get("max_position_pct"), max_sector_pct=lim.get("max_sector_pct"),
        max_open_positions=lim.get("max_open_positions"),
        max_positions_by_engine={e: v.get("max_positions") for e, v in cfg.get("engines", {}).items()},
        max_participation_adv=cfg.get("liquidity", {}).get("max_participation_adv"),
        margin_inr=acct.get("fno_margin_inr"), source=str(cfg.get("_source", "")))


@dataclass(frozen=True)
class Request:
    """One idea to size. Per-unit amounts are in rupees per unit of quantity (a share, or one lot/structure)."""
    key: str
    symbol: str
    engine: str
    priority: float                       # higher is taken first
    price: float | None                   # entry reference
    stop_loss: float | None               # loss per unit from the entry reference to the stop / invalidation
    gap_allowance: float | None           # extra adverse move per unit allowed beyond the stop (gaps, slippage)
    costs: float | None                   # round-trip charges per unit
    unit_capital: float | None = None     # cash needed per unit (default: price)
    unit_notional: float | None = None    # exposure per unit (default: price)
    unit_margin: float = 0.0              # margin per unit (derivatives)
    sector: str | None = None
    adv_inr: float | None = None          # 20-day average traded value (cash segment)
    direction: str = "long"
    segment: str = "cash"
    underlying: str = ""
    size_multiplier: float = 1.0          # reductions decided upstream (event risk, macro overlay); never > 1
    max_loss_inr: float | None = None     # a lower per-trade loss cap for this engine, if any
    stated_unit_loss: float | None = None  # the engine's own total, checked against the parts

    def unit_loss(self) -> float | None:
        parts = [_num(self.stop_loss), _num(self.gap_allowance), _num(self.costs)]
        return None if any(p is None for p in parts) else round(sum(parts), 6)

    def capital_per_unit(self) -> float | None:
        return _num(self.price if self.unit_capital is None else self.unit_capital)

    def notional_per_unit(self) -> float | None:
        return _num(self.price if self.unit_notional is None else self.unit_notional)


@dataclass(frozen=True)
class Exposure:
    """A holding that carries market exposure, valued at its last valid mark."""
    symbol: str
    engine: str
    notional: float | None                # signed rupees (long +, short -); None = no valid mark
    sector: str | None = None
    segment: str = "cash"
    underlying: str = ""
    slot: bool = True


@dataclass(frozen=True)
class Claim:
    """An active row with no market exposure: cash owed, a fraction, an entitlement without a price."""
    symbol: str
    engine: str
    owed: float = 0.0
    slot: bool = True
    held: bool = True


@dataclass(frozen=True)
class BookState:
    exposures: tuple = ()
    claims: tuple = ()
    available_cash: float | None = None   # spendable cash, only when the source verifies it; None = not known
    problems: tuple = ()
    margin_used: float = 0.0

    def gross(self) -> float:
        return sum(abs(e.notional) for e in self.exposures)

    def net(self) -> float:
        return sum(e.notional for e in self.exposures)

    def sector_notional(self, sector: str) -> float:
        return sum(abs(e.notional) for e in self.exposures if _sector(e.sector) == sector)

    def committed_cash(self) -> float:
        """Capital tied up: cash-segment holdings at their mark and cash owed but not yet spendable."""
        return sum(abs(e.notional) for e in self.exposures if e.segment == "cash") + sum(c.owed for c in self.claims)

    def slots(self, engine: str | None = None) -> int:
        return (sum(1 for e in self.exposures if e.slot and (engine is None or e.engine == engine))
                + sum(1 for c in self.claims if c.slot and (engine is None or c.engine == engine)))

    def holds(self, symbol: str) -> bool:
        return (any(symbol in (e.symbol, e.underlying) for e in self.exposures)
                or any(c.held and c.symbol == symbol for c in self.claims))

    def all_problems(self) -> list[str]:
        out = list(self.problems)
        out += [f"{e.symbol}: exposure has no valid value" for e in self.exposures if _num(e.notional) is None]
        out += [f"{c.symbol}: amount owed is not a number" for c in self.claims if _num(c.owed) is None]
        if self.available_cash is not None and _num(self.available_cash) is None:
            out.append("available cash is not a number")
        return out

    def plus(self, r: Request, d: "Decision") -> "BookState":
        """This book with an approved request booked at its reference price."""
        sign = 1 if r.direction == "long" else -1
        e = Exposure(r.symbol, r.engine, sign * d.notional_inr, r.sector, r.segment, r.underlying or r.symbol)
        cash = self.available_cash
        if cash is not None and r.segment == "cash":
            cash = cash - d.capital_inr
        return replace(self, exposures=self.exposures + (e,), available_cash=cash,
                       margin_used=self.margin_used + d.margin_inr)


@dataclass
class Decision:
    key: str
    symbol: str
    engine: str
    status: str = "rejected"              # approved | rejected
    quantity: int = 0
    unit_loss_inr: float = 0.0
    capital_inr: float = 0.0
    notional_inr: float = 0.0
    margin_inr: float = 0.0
    planned_loss_inr: float = 0.0
    exposure_after: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)     # [(check, PASS | FAIL | ADJUST | INFO, detail)]
    notes: list = field(default_factory=list)      # sizing arithmetic

    @property
    def failed(self) -> tuple | None:
        return next((c for c in self.checks if c[1] == "FAIL"), None)


def _sector(s) -> str:
    s = "" if s is None else str(s).strip()
    return "Unknown" if s.upper() in UNKNOWN_SECTORS else s


# ---------------------------------------------------------------------------------------------- shared rules
def rank_key(priority, engine, symbol, key) -> tuple:
    """Higher priority first; ties by engine, symbol, key (never by the order requests arrive in).
    A missing or non-numeric priority goes last."""
    p = _num(priority)
    return (-(p if p is not None else -math.inf), str(engine), str(symbol), str(key))


def order_key(r: Request) -> tuple:
    return rank_key(r.priority, r.engine, r.symbol, r.key)


def _input_problems(r: Request, p: Policy) -> list[str]:
    out = []
    if not _pos(r.price):
        out.append("no valid entry reference price")
    for name in ("stop_loss", "gap_allowance", "costs"):
        v = _num(getattr(r, name))
        if v is None:
            out.append(f"{name.replace('_', ' ')} per unit not stated")
        elif v < 0 or (name == "stop_loss" and v == 0):
            out.append(f"{name.replace('_', ' ')} per unit {v:g} is not valid")
    ul = r.unit_loss()
    if ul is not None and r.stated_unit_loss is not None:
        st = _num(r.stated_unit_loss)
        if st is None or abs(st - ul) > LOSS_TOLERANCE_INR:
            out.append(f"stated loss per unit {r.stated_unit_loss} differs from stop + gap + costs {ul:.4f}")
    if not _pos(r.capital_per_unit()):
        out.append("no valid capital per unit")
    if not _pos(r.notional_per_unit()):
        out.append("no valid exposure per unit")
    if _sector(r.sector) == "Unknown":
        out.append("sector unknown (the sector cap cannot be checked)")
    if r.segment == "cash" and not _pos(r.adv_inr):
        out.append("no 20-day traded value (the liquidity cap cannot be checked)")
    m = _num(r.size_multiplier)
    if m is None or not 0 < m <= 1:
        out.append(f"size multiplier {r.size_multiplier} outside (0, 1]")
    if r.max_loss_inr is not None and not _pos(r.max_loss_inr):
        out.append("engine loss cap is not a positive number")
    if r.direction not in ("long", "short"):
        out.append(f"direction {r.direction!r} unknown")
    if r.segment == "fno":
        if not _pos(p.margin_inr):
            out.append("derivative margin not set")
        if _num(r.unit_margin) is None or r.unit_margin < 0:
            out.append("margin per unit not valid")
    return out


def size_one(r: Request, book: BookState, policy: Policy, regime: str | None) -> Decision:
    """Size one request against a book. Pure: neither argument is changed."""
    d = Decision(key=r.key, symbol=r.symbol, engine=r.engine)

    def fail(check: str, detail: str) -> Decision:
        d.checks.append((check, "FAIL", detail))
        d.status = "rejected"
        return d

    pp = policy.problems()
    if pp:
        return fail("risk_budget", "allocation policy unset: " + "; ".join(pp))
    bp = book.all_problems()
    if bp:
        return fail("book_view", "book incomplete: " + "; ".join(bp))
    share = _num(policy.regime_gross.get(regime)) if regime is not None else None
    if share is None or not 0 <= share <= 1:
        return fail("regime", f"market regime {regime!r} unknown; the gross cap cannot be set")
    ip = _input_problems(r, policy)
    if ip:
        return fail("allocation_inputs", "; ".join(ip))

    cap = float(policy.capital_inr)
    under = r.underlying or r.symbol
    if book.holds(r.symbol) or book.holds(under):
        return fail("duplicate", f"{r.symbol} already held")
    if book.slots() + 1 > policy.max_open_positions:
        return fail("position_count", f"{book.slots()} open; max {policy.max_open_positions}")
    emax = policy.max_positions_by_engine.get(r.engine)
    if not _pos(emax):
        return fail("position_count", f"{r.engine}: engine position limit not set")
    if book.slots(r.engine) + 1 > emax:
        return fail("position_count", f"{r.engine} has {book.slots(r.engine)}; max {emax}")

    # sizing from the loss budget and the full loss per unit
    unit_loss = r.unit_loss()
    unit_cap, unit_not = r.capital_per_unit(), r.notional_per_unit()
    budget = float(policy.per_trade_max_loss_inr)
    if r.max_loss_inr is not None:
        budget = min(budget, float(r.max_loss_inr))
    q = math.floor(budget * r.size_multiplier / unit_loss + 1e-9)
    d.unit_loss_inr = unit_loss
    notes = [f"risk budget Rs{budget:,.0f} x{r.size_multiplier:g} / unit loss Rs{unit_loss:,.2f} "
             f"(stop {r.stop_loss:,.2f} + gap {r.gap_allowance:,.2f} + costs {r.costs:,.2f}) -> {q}"]
    caps = [("max_position_pct", math.floor(policy.max_position_pct * cap / unit_not + 1e-9))]
    if r.segment == "cash":
        caps.append(("liquidity_participation", math.floor(policy.max_participation_adv * r.adv_inr / unit_not + 1e-9)))
        caps.append(("available_capital", math.floor(max(cap - book.committed_cash(), 0) / unit_cap + 1e-9)))
        if book.available_cash is not None:
            caps.append(("available_cash", math.floor(max(book.available_cash, 0) / unit_cap + 1e-9)))
    if r.segment == "fno":
        per = max(r.unit_margin, unit_cap)
        caps.append(("available_margin", math.floor(max(policy.margin_inr - book.margin_used, 0) / per + 1e-9)))
    for name, v in caps:
        if v < q:
            notes.append(f"capped by {name}: {q} -> {v}")
            q = v
    d.notes = notes
    if q < 1:
        return fail("sizing", "; ".join(notes) + " -> below one unit")
    d.checks.append(("sizing", "ADJUST" if len(notes) > 1 or r.size_multiplier < 1 else "PASS", "; ".join(notes)))

    # exposure: the regime scales the gross cap (once)
    g_cap = policy.max_gross_exposure_pct * share * cap
    sign = 1 if r.direction == "long" else -1
    gross_now = book.gross()
    if gross_now + q * unit_not > g_cap:
        q2 = math.floor(max(g_cap - gross_now, 0) / unit_not + 1e-9)
        if q2 < 1:
            return fail("gross_exposure", f"gross after Rs{gross_now + q * unit_not:,.0f} > cap Rs{g_cap:,.0f} "
                                          f"({regime} x{share:g})")
        d.checks.append(("gross_exposure", "ADJUST", f"{q} -> {q2} to stay within {regime} gross cap"))
        q = q2
    else:
        d.checks.append(("gross_exposure", "PASS", f"gross after {(gross_now + q * unit_not) / cap:.0%} "
                                                   f"(cap {g_cap / cap:.0%}, {regime})"))
    add = q * unit_not
    gross_after, net_after = gross_now + add, book.net() + sign * add
    if abs(net_after) > policy.max_net_exposure_pct * cap:
        return fail("net_exposure", f"net after {net_after / cap:.0%} > {policy.max_net_exposure_pct:.0%}")
    sector = _sector(r.sector)
    sec_after = book.sector_notional(sector) + add
    if sec_after > policy.max_sector_pct * cap:
        return fail("sector", f"{sector} after {sec_after / cap:.0%} > {policy.max_sector_pct:.0%}")
    d.checks.append(("sector", "PASS", f"{sector} after {sec_after / cap:.0%}"))

    d.status, d.quantity = "approved", int(q)
    d.capital_inr = round(q * unit_cap, 2)
    d.notional_inr = round(add, 2)
    d.margin_inr = round(q * (r.unit_margin or 0.0), 2)
    d.planned_loss_inr = round(q * unit_loss, 2)
    d.exposure_after = {"gross_pct": round(gross_after / cap, 4), "net_pct": round(net_after / cap, 4),
                        "sector_pct": round(sec_after / cap, 4), "open_positions": book.slots() + 1}
    return d


def allocate(requests, book: BookState, policy: Policy, regime: str | None) -> list[Decision]:
    """Size every request in order_key order; each approval is booked before the next request is sized."""
    out = []
    for r in sorted(requests, key=order_key):
        d = size_one(r, book, policy, regime)
        if d.status == "approved":
            book = book.plus(r, d)
        out.append(d)
    return out


# ---------------------------------------------------------------------------------------------- legacy rules
def legacy_allocate(intended: dict, picks: list, equity: float, gross: float, budget: float,
                    method: str = "rank_order") -> dict:
    """Rupee sizes for the V5 backtest's weekly new positions (comparison only; see the module docstring).

    intended  {symbol: risk-parity size, capped at max_w x equity} for the ranked top-N candidate list, best first
    picks     the candidates that fit in the free slots (the first ones of that list)
    budget    regime exposure still available: equity x gross - exposure already held

    rank_order         sizes use the formula of v5_signal_generator.size_positions on the current equity: the
                       whole top-N list is renormalised so a full book fits equity x gross, and the picks are
                       funded in rank order at that size; the last one funded takes what is left of the budget and
                       the rest are not opened, so their slots stay free for later weeks. Not a match for the
                       paper ledger (fixed --portfolio-inr base, confidence order, no regime cap on the book) or
                       the research risk engine (per-trade loss budget, configured capital and caps).
    pro_rata_leftover  the original backtest rule: if the picks' intended total exceeds the budget, every pick is
                       scaled by the same factor, so a small leftover is spread over every free slot.
    """
    if method == "pro_rata_leftover":
        sizes = {s: intended[s] for s in picks if s in intended}
        tot = sum(sizes.values())
        if tot > budget and tot > 0:
            sizes = {s: v * budget / tot for s, v in sizes.items()}
        return sizes
    if method != "rank_order":
        raise ValueError(f"entry_allocation must be one of {LEGACY_METHODS}, not {method!r}")
    tot = sum(intended.values())
    scale = min(1.0, equity * gross / tot) if tot > 0 else 0.0
    sizes, left = {}, max(budget, 0.0)
    for s in picks:
        if s not in intended:
            continue
        if left <= 0:
            break
        sizes[s] = min(intended[s] * scale, left)
        left -= sizes[s]
    return sizes
