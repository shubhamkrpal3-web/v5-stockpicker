"""Equity-derivatives engine (intraday to weeks).

Finds a suitable futures/options EXPRESSION for an underlying view produced by
another engine (today: swing). It does not create directional views itself.

Order of work for each view:
  1. Contract universe: the underlying must be in NSE's current F&O list
     (fo_mktlots). A listed equity is not automatically F&O-eligible.
  2. Expiry: the nearest expiry that outlives the expected hold, and at least
     min_days_to_expiry away.
  3. Structure: a defined-risk bull call spread (buy near-the-money call,
     sell the call nearest the view's first target). Naked short options are
     never proposed.
  4. Pricing: Black-76 off the same-expiry future using NSE settlement prices
     (end of day; no live bid/ask), implied vol per leg, Greeks, stress grid.
  5. Cash vs derivative: the candidate states why the spread is preferable to
     the cash stock (or the engine returns NO TRADE for that view when it isn't).

Stock options on NSE are physically settled: an in-the-money position held
into expiry becomes a delivery obligation. Every plan exits before expiry week.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research_system import confidence as conf
from research_system.candidate import Candidate, Evidence, Leg, NoTrade, Plan, Scenario
from research_system.engines import common
from research_system.pricing import black76, implied_vol

SETUP = "view_expression_bull_call_spread"


def option_charges(prem_buy: float, prem_sell: float, lot: int) -> float:
    """Approximate round-trip charges for a two-leg spread (entry + exit), in Rs.

    Brokerage Rs20/order x 4 orders, STT 0.1% on option sale premium (both
    sides' sell legs), exchange 0.035% + SEBI on premium turnover, GST 18% on
    brokerage + exchange, stamp 0.003% on buy premium, and slippage of 2% of
    each leg's premium per side (EOD settlement prices, no live spread)."""
    turnover = 2 * (prem_buy + prem_sell) * lot
    brokerage = 4 * 20.0
    stt = 0.001 * (prem_buy + prem_sell) * lot
    exch = 0.00035 * turnover + 0.000001 * turnover
    gst = 0.18 * (brokerage + exch)
    stamp = 0.00003 * (prem_buy + prem_sell) * lot
    slippage = 0.02 * turnover
    return brokerage + stt + exch + gst + stamp + slippage


def _spread_value(F, k1, k2, T, r, iv1, iv2):
    return black76(F, k1, T, r, iv1, True).price - black76(F, k2, T, r, iv2, True).price


def express(ctx, view: Candidate, ecfg: dict) -> tuple[Candidate | None, str]:
    sym = view.symbol
    lots = ctx.fno_lots
    if lots.empty or sym not in set(lots["SYMBOL"]):
        return None, f"{sym}: not in the current NSE F&O contract list"
    bhav = ctx.fno_bhav
    chain = bhav[bhav["SYMBOL"] == sym]
    if chain.empty:
        return None, f"{sym}: no contract data in the latest F&O file"
    d0 = pd.Timestamp(chain["DATE"].iloc[0])
    r = float(ecfg.get("risk_free_rate", 0.065))
    min_dte = int(ecfg.get("min_days_to_expiry", 7))
    min_oi = float(ecfg.get("min_oi_contracts", 200))
    hold_sessions = int((common.setup_stats(ctx, view.setup).get("stats") or {}).get("median_days", 20) or 20)
    need_days = max(min_dte, int(hold_sessions * 1.45) + 5)       # sessions -> calendar days + exit buffer

    futs = chain[chain["TYPE"] == "STF"].sort_values("EXPIRY")
    exp_ok = [e for e in futs["EXPIRY"] if (pd.Timestamp(e) - d0).days >= need_days]
    if not exp_ok:
        return None, f"{sym}: no expiry at least {need_days} days out"
    expiry = exp_ok[0]
    fut = futs[futs["EXPIRY"] == expiry].iloc[0]
    F = float(fut["SETTLE"])
    spot = float(fut["UNDERLYING_PX"])
    T = (pd.Timestamp(expiry) - d0).days / 365.0
    lot = int(fut.get("LOT") or lots.loc[lots["SYMBOL"] == sym, "LOT_SIZE"].iloc[0])

    calls = chain[(chain["TYPE"] == "STO") & (chain["OPT"] == "CE") & (chain["EXPIRY"] == expiry)]
    calls = calls[calls["OI"] >= min_oi].sort_values("STRIKE")
    target = view.plan.targets[0] if view.plan.targets else spot * 1.08
    k1s = calls[calls["STRIKE"] >= spot]
    if k1s.empty:
        return None, f"{sym}: no liquid call at or above spot (OI >= {min_oi:.0f})"
    k1row = k1s.iloc[0]
    k2s = calls[calls["STRIKE"] > k1row["STRIKE"]]
    if k2s.empty:
        return None, f"{sym}: no liquid call above {k1row['STRIKE']:.0f} for the short leg"
    k2row = k2s.iloc[(k2s["STRIKE"] - target).abs().argsort()].iloc[0]
    k1, k2 = float(k1row["STRIKE"]), float(k2row["STRIKE"])
    p1, p2 = float(k1row["SETTLE"]), float(k2row["SETTLE"])
    debit = p1 - p2
    if not (0 < debit < (k2 - k1)):
        return None, f"{sym}: spread {k1:.0f}/{k2:.0f} mispriced at settlement (debit {debit:.2f})"
    iv1, iv2 = implied_vol(p1, F, k1, T, r, True), implied_vol(p2, F, k2, T, r, True)
    if not iv1 or not iv2:
        return None, f"{sym}: implied vol not solvable from settlement prices"

    g1, g2 = black76(F, k1, T, r, iv1, True), black76(F, k2, T, r, iv2, True)
    net = {"delta": (g1.delta - g2.delta) * lot, "gamma": (g1.gamma - g2.gamma) * lot,
           "vega_per_volpt": (g1.vega - g2.vega) * lot / 100, "theta_per_day": (g1.theta - g2.theta) * lot / 365}
    charges = option_charges(p1, p2, lot)
    max_loss = debit * lot + charges
    max_gain = (k2 - k1 - debit) * lot - charges
    if max_gain <= 0:
        return None, f"{sym}: spread cannot cover charges"

    # scenarios at the end of the expected hold (time decay included)
    hold_T = min(hold_sessions * 1.45 / 365, T * 0.8)
    T_rem = T - hold_T
    basis = F / spot

    def at(s, iv_shift=0.0):
        v = _spread_value(s * basis, k1, k2, T_rem, r, max(iv1 + iv_shift, 0.01), max(iv2 + iv_shift, 0.01))
        return (v - debit) * lot - charges

    stop = view.plan.stop or spot * 0.92
    sig = iv1 * math.sqrt(hold_T)
    stress = float(ecfg.get("stress_sigma", 2.0))
    scen = [Scenario("underlying at view stop, end of hold", round(stop, 2), round(at(stop), 2)),
            Scenario("underlying unchanged, end of hold (time decay)", round(spot, 2), round(at(spot), 2)),
            Scenario("underlying at view target, end of hold", round(target, 2), round(at(target), 2)),
            Scenario(f"stress -{stress:g} sigma", round(spot * math.exp(-stress * sig), 2),
                     round(at(spot * math.exp(-stress * sig)), 2)),
            Scenario(f"stress +{stress:g} sigma", round(spot * math.exp(stress * sig), 2),
                     round(at(spot * math.exp(stress * sig)), 2)),
            Scenario("vol -5 pts, spot unchanged", round(spot, 2), round(at(spot, -0.05), 2)),
            Scenario("max loss (defined)", None, round(-max_loss, 2)),
            Scenario("max gain at/above short strike at expiry", k2, round(max_gain, 2))]

    summ = ctx.fno_summary
    hist = summ[summ["SYMBOL"] == sym].sort_values("DATE") if not summ.empty else pd.DataFrame()
    ivp = None
    if len(hist) >= 20:
        x = hist["ATM_IV"].dropna().tail(252)
        ivp = float((x < x.iloc[-1]).mean())
    rv = float(ctx.C[sym].pct_change().tail(20).std() * np.sqrt(252)) if sym in ctx.C.columns else None
    iv_rv = iv1 / rv if rv else None

    cash_capital_same_delta = abs(net["delta"]) * spot
    why = (f"Defined maximum loss Rs{max_loss:,.0f} per spread regardless of overnight gaps (the cash stop can be "
           f"gapped through), for a delta exposure equivalent to Rs{cash_capital_same_delta:,.0f} of stock. "
           f"Trade-offs: gains capped at the {k2:.0f} strike, time decay "
           f"Rs{net['theta_per_day']:,.0f}/day, and the view must play out before expiry.")
    if iv_rv is not None and iv_rv > 1.5:
        why += f" Premium looks rich (IV/realised {iv_rv:.1f}x) which makes buying options less attractive."

    ev = list(view.evidence[:3]) + [
        Evidence("derivatives", f"{expiry} future {F:.2f} vs spot {spot:.2f} (basis {(basis - 1):+.2%}); lot {lot}",
                 "nse F&O bhavcopy", d0.strftime("%Y-%m-%d")),
        Evidence("derivatives", f"legs: buy {k1:.0f} CE @ {p1:.2f} (IV {iv1:.1%}, OI {k1row['OI']:,.0f}), "
                                f"sell {k2:.0f} CE @ {p2:.2f} (IV {iv2:.1%}, OI {k2row['OI']:,.0f})",
                 "nse F&O bhavcopy settlement prices", d0.strftime("%Y-%m-%d")),
        Evidence("derivatives", f"net Greeks per spread: delta {net['delta']:+.1f} sh, gamma {net['gamma']:+.4f}, "
                                f"vega {net['vega_per_volpt']:+.1f}/vol-pt, theta {net['theta_per_day']:+.1f}/day",
                 "research_system.pricing (Black-76)", d0.strftime("%Y-%m-%d")),
    ]
    if iv_rv is not None:
        ev.append(Evidence("derivatives", f"ATM IV {iv1:.1%} vs 20d realised {rv:.1%} (ratio {iv_rv:.2f})"
                           + (f"; IV percentile {ivp:.0%}" if ivp is not None else "; IV history still accumulating"),
                           "nse F&O bhavcopy", d0.strftime("%Y-%m-%d")))

    vc = view.confidence.components if view.confidence else {}
    liq_s = conf.pct_score(math.log10(max(min(k1row["OI"], k2row["OI"]), 1)),
                           math.log10(min_oi), math.log10(min_oi * 50))
    comp = {
        "signal_strength": (vc.get("signal_strength", {}).get("score"), "from the underlying swing view"),
        "agreement": (vc.get("agreement", {}).get("score"), "from the underlying swing view"),
        "regime_fit": (vc.get("regime_fit", {}).get("score"), "underlying setup in the current regime"),
        "historical_results": (vc.get("historical_results", {}).get("score"),
                               "underlying setup history; the option expression itself is not backtested"),
        "liquidity": (liq_s, f"min leg OI {min(k1row['OI'], k2row['OI']):,.0f} contracts"),
        "data_freshness": common.freshness_component(ctx, "equity_eod", "fno_summary"),
        "model_certainty": (0.3 if (iv_rv or 1) <= 1.5 else 0.2,
                            "EOD settlement prices (no bid/ask); Black-76 with flat per-leg IV"),
    }
    caps = [("C", "option expression not backtested; underlying setup is backtest-only")]
    cnf = conf.grade(comp, caps)
    exit_by = (pd.Timestamp(expiry) - pd.offsets.BDay(3)).strftime("%Y-%m-%d")
    cand = Candidate(
        engine="fno", symbol=sym, segment="fno",
        instrument=f"Bull call spread {pd.Timestamp(expiry):%d-%b-%Y} {k1:.0f}/{k2:.0f} CE x1 lot ({lot})",
        direction="long", horizon=f"until {exit_by} at the latest", data_timestamp=ctx.data_timestamp(),
        setup=SETUP,
        thesis=f"Defined-risk expression of the swing view on {sym} ({view.setup}). " + why,
        plan=Plan(entry_trigger=(f"enter both legs together as a spread order, net debit at or below "
                                 f"{debit * 1.05:.2f} (settlement {debit:.2f} + 5%)"),
                  entry_low=None, entry_high=round(debit * 1.05, 2), stop=view.plan.stop,
                  invalidation=f"underlying closes below {stop:.2f} (the view's stop) -> close the spread",
                  targets=[round(k2, 2)], time_exit=f"close by {exit_by} (3 sessions before expiry; physical settlement)",
                  holding_window=f"{hold_sessions} sessions median for the underlying view",
                  review_by=exit_by.replace("-", "")),
        evidence=ev,
        legs=[Leg("CE", sym, "BUY", p1, expiry, k1, 1, lot, iv1, g1.delta, float(k1row["OI"])),
              Leg("CE", sym, "SELL", p2, expiry, k2, 1, lot, iv2, g2.delta, float(k2row["OI"]))],
        scenarios=scen,
        risks=[f"expiry {expiry}: physical settlement if held into expiry; exit by {exit_by}",
               "settlement prices are not executable quotes; check the live bid/ask before entry",
               f"time decay {net['theta_per_day']:+.0f} Rs/day per spread", "liquidity: option spreads can widen sharply on news",
               "margin rules change; the broker may block more than the premium for the short leg"],
        missing_inputs=["live option quotes (bid/ask, depth)", "official SPAN margin for this structure"],
        cancel_conditions=list(view.cancel_conditions) + [f"net debit above {debit * 1.05:.2f}",
                                                         "either leg's open interest falls below the minimum"],
        confidence=cnf,
        unit_risk_inr=round(max_loss, 2), unit_capital_inr=round(debit * lot + charges, 2),
        unit_margin_inr=round(max_loss, 2), unit_notional_inr=round(abs(net["delta"]) * spot, 2),
        # defined risk: the loss cannot exceed the debit plus charges whatever the gap, so the gap part is zero
        unit_loss_parts={"stop": debit * lot, "gap": 0.0, "costs": charges},
        sector=view.sector, adv_inr=view.adv_inr, price=spot, overnight=True,
        hold_until=exit_by.replace("-", ""),
    )
    return cand, ""


def run(ctx, views: list) -> tuple[list, list]:
    ts = ctx.data_timestamp()
    ecfg = ctx.cfg.get("engines", {}).get("fno", {})
    if ctx.fno_bhav is None or ctx.fno_bhav.empty:
        return [], [NoTrade("fno", ts, ["no F&O contract data stored (run research_system.data.fno)"])]
    if ctx.fno_lots.empty:
        return [], [NoTrade("fno", ts, ["NSE F&O contract list unavailable"])]
    out, skipped = [], []
    for v in views:
        if v.direction != "long":
            continue
        c, why = express(ctx, v, ecfg)
        if c is not None:
            out.append(c)
        else:
            skipped.append(why)
    nt = []
    if not out:
        nt.append(NoTrade("fno", ts, skipped or ["no underlying views to express"]))
    elif skipped:
        nt.append(NoTrade("fno", ts, ["views without a suitable contract: " + "; ".join(skipped)]))
    nt.append(NoTrade("fno", ts, ["bearish expressions: no validated bearish setup feeds this engine yet"]))
    return out, nt
