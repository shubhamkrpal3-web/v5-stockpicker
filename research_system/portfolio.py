"""Current book seen by the risk engine.

Source, in order:
  1. data/research/portfolio.csv  - positions you actually hold / track
     columns: engine,symbol,segment,direction,quantity,entry_price,stop,sector,underlying,
              last_price(optional),overnight(optional)
     plus data/research/equity_log.csv (DATE,EQUITY) for the kill switch.
  2. otherwise the V5 paper-trade ledger (every active row + equity log), so the
     risk engine always sees the book the existing pipeline is running.

From the V5 ledger (see v5_modules/ledger_view.py for the exact rules):
  positions        share holdings with a valid mark - open, stale, exit-offer locked and
                   pending entitlements - valued at the latest close (open) or the last valid
                   mark (others): gross / net / sector / correlation / duplicate checks see them
  others           active rows that are not share exposure: cash owed (dividends, exit or
                   merger consideration), fractions, entitlements with no price yet. They count
                   toward slots and duplicates exactly as the ledger does (Book.slots_used /
                   Book.holds), and cash owed is committed capital that cannot be spent yet
  owed             cash owed but not yet spendable
  available_cash   the ledger's spendable cash at its last run, only when ledger_view.verify_cash
                   recomputes the same amount from the ledger's own records; otherwise None
  problems         fail-closed reasons - the risk engine approves nothing while any exist:
                     * an active holding or entitlement with no defensible value (e.g. demerged
                       shares with no price yet, shares with no valid mark, cash owed with no amount)
                     * ledger cash that is unverifiable (e.g. no equity log, or no pre-trading
                       starting row) or that does not match the recomputed amount
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

import pandas as pd

from research_system import paths

sys.path.insert(0, str(paths.ROOT / "v5_modules"))
import ledger_view  # noqa: E402


@dataclass
class Position:
    engine: str
    symbol: str
    segment: str
    direction: str
    quantity: float
    entry_price: float
    stop: float
    last_price: float
    sector: str = "Unknown"
    underlying: str = ""
    overnight: bool = True
    state: str = "open"               # open | stale | locked | pending entitlement (ledger_view states)

    @property
    def notional(self) -> float:
        sign = 1 if self.direction == "long" else -1
        return sign * self.quantity * self.last_price


@dataclass
class Other:
    """An active ledger row that is not share exposure (cash owed, fraction, unpriced entitlement)."""
    engine: str
    symbol: str
    state: str
    owed: float = 0.0
    slot: bool = True
    held: bool = True


@dataclass
class Portfolio:
    capital: float
    positions: list = field(default_factory=list)
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    source: str = ""
    others: list = field(default_factory=list)
    available_cash: float | None = None
    problems: list = field(default_factory=list)
    view: dict = field(default_factory=dict)

    # ----- exposure
    def gross(self) -> float:
        return sum(abs(p.notional) for p in self.positions)

    def net(self) -> float:
        return sum(p.notional for p in self.positions)

    def owed(self) -> float:
        return sum(o.owed for o in self.others)

    def sector_notional(self) -> dict:
        out: dict = {}
        for p in self.positions:
            out[p.sector] = out.get(p.sector, 0.0) + abs(p.notional)
        return out

    def count(self, engine: str | None = None) -> int:
        """Position slots in use (the ledger's rule: cash owed as consideration and unpriced
        entitlements take a slot; dividends owed and fractions do not)."""
        return (sum(1 for p in self.positions if engine is None or p.engine == engine)
                + sum(1 for o in self.others if o.slot and (engine is None or o.engine == engine)))

    def holds(self, symbol: str) -> bool:
        return (any(p.symbol == symbol or p.underlying == symbol for p in self.positions)
                or any(o.held and o.symbol == symbol for o in self.others))

    # ----- kill switch inputs
    def drawdown(self) -> float | None:
        if self.equity.empty:
            return None
        return float(self.equity.iloc[-1] / self.equity.cummax().iloc[-1] - 1)

    def today_pnl_pct(self) -> float | None:
        if len(self.equity) < 2:
            return None
        return float(self.equity.iloc[-1] / self.equity.iloc[-2] - 1)

    def add(self, p: Position) -> None:
        self.positions.append(p)


def _sector_map() -> dict:
    if not paths.NIFTY500_LIST.exists():
        return {}
    df = pd.read_csv(paths.NIFTY500_LIST)
    return dict(zip(df["Symbol"].astype(str).str.upper(), df["Industry"].astype(str)))


def sector_of(symbol: str) -> str:
    return _SECTORS.get(symbol.upper(), "Unknown")


_SECTORS = _sector_map()


def load(capital: float, last_prices: dict | None = None) -> Portfolio:
    last_prices = last_prices or {}
    if paths.PORTFOLIO.exists():
        df = pd.read_csv(paths.PORTFOLIO)
        pos = []
        for r in df.itertuples(index=False):
            sym = str(r.symbol).upper()
            lp = float(getattr(r, "last_price", float("nan")))
            if lp != lp:
                lp = float(last_prices.get(sym, r.entry_price))
            pos.append(Position(engine=r.engine, symbol=sym, segment=r.segment, direction=r.direction,
                                quantity=float(r.quantity), entry_price=float(r.entry_price),
                                stop=float(r.stop), last_price=lp,
                                sector=getattr(r, "sector", None) or sector_of(sym),
                                underlying=str(getattr(r, "underlying", "") or sym),
                                overnight=bool(getattr(r, "overnight", True))))
        eq_path = paths.RESEARCH / "equity_log.csv"
        eq = (pd.read_csv(eq_path).set_index("DATE")["EQUITY"] if eq_path.exists()
              else pd.Series(dtype=float))
        return Portfolio(capital=capital, positions=pos, equity=eq, source=str(paths.PORTFOLIO))

    return load_v5(capital, paths.V5_POSITIONS, paths.V5_EQUITY_LOG, last_prices)


def load_v5(capital: float, positions_path, equity_log_path, last_prices: dict | None = None) -> Portfolio:
    """Every active row of the V5 paper ledger (new or older format), split as described above."""
    v5 = pd.read_csv(positions_path) if positions_path.exists() else pd.DataFrame()
    e = pd.read_csv(equity_log_path) if equity_log_path.exists() else pd.DataFrame()
    rows = ledger_view.classify(v5, last_prices)
    rt_path = positions_path.parent / "realized_trades.csv"
    rt = pd.read_csv(rt_path) if rt_path.exists() else None
    view = ledger_view.summary(v5, e, last_prices, rt)
    pos, others, problems = [], [], []
    for r in rows.itertuples(index=False):
        if r.state in ledger_view.SHARE_STATES and r.mark == r.mark:
            pos.append(Position(engine="swing", symbol=r.symbol, segment="cash", direction="long",
                                quantity=float(r.qty), entry_price=float(r.entry_price),
                                stop=float(r.current_stop) if r.current_stop == r.current_stop else 0.0,
                                last_price=float(r.mark), sector=sector_of(r.symbol), underlying=r.symbol,
                                state=r.state))
        else:
            others.append(Other(engine="swing", symbol=r.symbol, state=r.state, owed=float(r.owed),
                                slot=bool(r.slot), held=bool(r.held)))
    if view["undefended"]:
        problems.append("no defensible value - " + "; ".join(view["undefended"]))
    if not view["cash_verified"]:
        what = "no paper ledger found" if not positions_path.exists() and not equity_log_path.exists() else "ledger cash"
        problems.append(f"{what}: {view['cash_check']}")
    eq = e.set_index("DATE")["EQUITY"].astype(float) if len(e) and "EQUITY" in e.columns else pd.Series(dtype=float)
    return Portfolio(capital=capital, positions=pos, equity=eq, source="v5 paper ledger", others=others,
                     available_cash=view["available_cash"], problems=problems, view=view)
