"""Intraday short engine (minutes to hours).

Needs a LIVE intraday feed. Without one it returns NO TRADE: end-of-day data
cannot support an opening-range / VWAP short, and stale quotes are worse than
no signal. Connect a broker or data provider by implementing IntradayFeed
(see docs/ARCHITECTURE.md). Broker credentials stay in your own environment;
this package never handles them and never places orders.

Setup (opening-range breakdown with relative weakness):
  * universe: NSE F&O stocks (deep liquidity, no fixed circuit band),
    excluding names near the lower price band (a short there may be unable to
    cover) and names with results/ex-dates today
  * opening range = first 15 minutes; trigger when a 5-minute bar closes
    below the opening-range low AND below VWAP
  * relative weakness: stock return since open at least 0.5 pp worse than
    Nifty 50, and the sector weaker than the market
  * invalidation: back above max(VWAP, opening-range midpoint)
  * cover levels: 1R and 2R; time exit at square_off_by; mandatory flat
    before the broker's MIS auto square-off; no new entries after the cutoff

The setup has NOT been validated on recorded intraday data, so candidates are
capped at grade D and the default risk engine floor (C) rejects them. Record
intraday bars, validate the setup, then add it to the setup registry.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

import pandas as pd

from research_system import confidence as conf
from research_system.candidate import Candidate, Evidence, NoTrade, Plan, Scenario
from research_system.data.calendar import IST

SETUP = "orb_breakdown_relative_weakness"


class IntradayFeed(Protocol):
    def is_live(self) -> bool: ...
    def bars(self, symbol: str, interval: str = "5min") -> pd.DataFrame: ...      # ts, open, high, low, close, volume
    def index_bars(self, name: str = "NIFTY 50", interval: str = "5min") -> pd.DataFrame: ...
    def price_band(self, symbol: str) -> tuple[float, float] | None: ...


class NullFeed:
    def is_live(self) -> bool:
        return False


def vwap(b: pd.DataFrame) -> pd.Series:
    tp = (b["high"] + b["low"] + b["close"]) / 3
    return (tp * b["volume"]).cumsum() / b["volume"].cumsum().replace(0, float("nan"))


def detect(bars: pd.DataFrame, index_bars: pd.DataFrame, or_bars: int = 3,
           min_rel_weakness: float = 0.005) -> dict | None:
    """Return trigger details for the latest bar, or None."""
    if len(bars) <= or_bars or len(index_bars) <= or_bars:
        return None
    o = bars.iloc[:or_bars]
    or_hi, or_lo = float(o["high"].max()), float(o["low"].min())
    vw = vwap(bars)
    last = bars.iloc[-1]
    stock_ret = float(last["close"] / bars["open"].iloc[0] - 1)
    idx_ret = float(index_bars["close"].iloc[-1] / index_bars["open"].iloc[0] - 1)
    rel = stock_ret - idx_ret
    if not (last["close"] < or_lo and last["close"] < vw.iloc[-1] and rel <= -min_rel_weakness):
        return None
    stop = max(float(vw.iloc[-1]), (or_hi + or_lo) / 2)
    entry = float(last["close"])
    risk = stop - entry
    if risk <= 0:
        return None
    return {"entry": entry, "stop": stop, "risk": risk, "or_high": or_hi, "or_low": or_lo,
            "vwap": float(vw.iloc[-1]), "rel_weakness": rel, "bar_time": str(last.get("ts", ""))}


def run(ctx, feed: IntradayFeed | None = None, now: datetime | None = None) -> tuple[list, list]:
    ecfg = ctx.cfg.get("engines", {}).get("intraday_short", {})
    ts = (now or datetime.now(IST)).isoformat(timespec="seconds")
    if not ecfg.get("enabled", False):
        return [], [NoTrade("intraday_short", ts, ["engine disabled in config (needs a live intraday feed)"])]
    if feed is None or not feed.is_live():
        return [], [NoTrade("intraday_short", ts, ["no live intraday data feed configured; EOD data cannot "
                                                   "support this engine"])]
    now = (now or datetime.now(IST)).astimezone(IST)
    cutoff = datetime.strptime(ecfg.get("no_new_entries_after", "14:30"), "%H:%M").time()
    if now.time() >= cutoff:
        return [], [NoTrade("intraday_short", ts, [f"after the {cutoff:%H:%M} entry cutoff"])]
    idx = feed.index_bars()
    universe = [s for s in ctx.fno_lots["SYMBOL"] if s not in {"NIFTY", "BANKNIFTY", "FINNIFTY",
                                                                 "MIDCPNIFTY", "NIFTYNXT50"}]
    today = now.strftime("%Y%m%d")
    events_today = set(ctx.calendar[ctx.calendar["event_date"] == today]["symbol"]) if not ctx.calendar.empty else set()
    out = []
    for sym in universe:
        if sym in events_today:
            continue
        b = feed.bars(sym)
        sig = detect(b, idx)
        if not sig:
            continue
        band = feed.price_band(sym)
        if band and sig["entry"] <= band[0] * 1.02:
            continue
        cost = sig["entry"] * 0.0008       # intraday round trip incl. slippage, approx.
        unit_risk = sig["risk"] * 1.25 + cost  # 25% slippage allowance on a fast market
        comp = {"signal_strength": (conf.pct_score(-sig["rel_weakness"], 0.005, 0.03),
                                    f"{sig['rel_weakness']:+.2%} vs Nifty since open"),
                "agreement": (None, "sector and breadth confirmation not yet wired"),
                "regime_fit": (None, "no intraday regime study"),
                "historical_results": (None, "setup not validated on recorded intraday data"),
                "liquidity": (0.8, "F&O stock"), "data_freshness": (1.0, "live feed"),
                "model_certainty": (0.2, "unvalidated intraday setup")}
        cnf = conf.grade(comp, [("D", "intraday setup not validated on recorded data")])
        sq = ecfg.get("square_off_by", "15:10")
        out.append(Candidate(
            engine="intraday_short", symbol=sym, segment="cash", instrument="NSE EQ intraday (MIS) short",
            direction="short", horizon=f"intraday, flat by {sq}", data_timestamp=ts, setup=SETUP,
            thesis=(f"{sym} broke below its opening range and VWAP while {abs(sig['rel_weakness']):.2%} weaker "
                    "than Nifty 50 since the open."),
            plan=Plan(entry_trigger=f"sell on a break below {sig['or_low']:.2f} with the bar closing below VWAP",
                      entry_low=round(sig["entry"] * 0.998, 2), entry_high=round(sig["entry"], 2),
                      stop=round(sig["stop"], 2), invalidation=f"5-min close above {sig['stop']:.2f}",
                      targets=[round(sig["entry"] - sig["risk"], 2), round(sig["entry"] - 2 * sig["risk"], 2)],
                      time_exit=f"cover at {sq} regardless; never carried overnight", holding_window="minutes to hours"),
            evidence=[Evidence("technical", f"opening range {sig['or_low']:.2f}-{sig['or_high']:.2f}, VWAP {sig['vwap']:.2f}",
                               "live feed", sig["bar_time"])],
            scenarios=[Scenario("stop hit (+25% slippage)", round(sig["stop"], 2), round(-unit_risk, 2)),
                       Scenario("1R cover", round(sig["entry"] - sig["risk"], 2), round(sig["risk"] - cost, 2))],
            risks=["short squeeze / news spike", "upper/lower band halts", "broker auto square-off timing"],
            cancel_conditions=["price reclaims VWAP before entry", f"after {cutoff:%H:%M}"],
            confidence=cnf, unit_risk_inr=unit_risk, unit_capital_inr=sig["entry"] * 0.2,
            unit_loss_parts={"stop": sig["risk"], "gap": sig["risk"] * 0.25, "costs": cost},
            unit_notional_inr=sig["entry"], sector=ctx.sectors.get(sym, "Unknown"),
            adv_inr=float(ctx.adv20().get(sym, 0) or 0), price=sig["entry"], overnight=False))
    return out, ([] if out else [NoTrade("intraday_short", ts, ["no valid setups at this time"])])
