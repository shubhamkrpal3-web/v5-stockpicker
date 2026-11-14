"""Swing engine (days to weeks).

Setups are admitted only through the setup registry (setup_registry.json),
which records how each one performed historically. Today there is one:

  momentum_lowvol_rank  - the corrected V5 ranking: 62.5% skip-month momentum
                          (3-1 and 6-1) + 37.5% low 90-day volatility, top 15 of
                          the liquid universe, entry next open, initial stop
                          2 ATR, chandelier trailing stop 4 ATR, no time stop.
                          Backtested 2018-2026 on split-adjusted prices.

Rules are kept identical to the backtest so the recorded statistics apply.
New swing setups must pass research_system / swing_signal_lab validation and
be added to the registry before this engine will emit them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research_system import confidence as conf
from research_system.candidate import Candidate, Evidence, NoTrade, Plan, Scenario
from research_system.engines import common

SETUP = "momentum_lowvol_rank"
TOP_N = 15
W_MOM, W_LV = 0.625, 0.375


def score_universe(ctx) -> pd.DataFrame:
    C, TV = ctx.C, ctx.TV
    ret = C.pct_change(fill_method=None)
    last = C.index[-1]
    df = pd.DataFrame({
        "CLOSE": C.iloc[-1],
        "TV": TV.iloc[-1],
        "MOM_3_1": C.shift(21).iloc[-1] / C.shift(63).iloc[-1] - 1,
        "MOM_6_1": C.shift(21).iloc[-1] / C.shift(126).iloc[-1] - 1,
        "VOL_90D": ret.rolling(90, min_periods=60).std().iloc[-1] * np.sqrt(252),
        "ATR14": ctx.atr14(),
        "ADV20": ctx.adv20(),
    })
    df = df[(C.iloc[-1].notna()) & (df["CLOSE"] >= 20) & (df["TV"] >= 1e7)]   # backtested universe
    df = df.dropna(subset=["MOM_3_1", "MOM_6_1", "VOL_90D", "ATR14"])
    mom = (df["MOM_3_1"].rank(pct=True) + df["MOM_6_1"].rank(pct=True)) / 2
    df["SCORE"] = W_MOM * mom + W_LV * (1 - df["VOL_90D"].rank(pct=True))
    df["SCORE_PCT"] = df["SCORE"].rank(pct=True)
    df.attrs["asof"] = last
    return df.sort_values("SCORE", ascending=False)


def _quality_pass(ctx, sym: str) -> bool | None:
    f = ctx.fundamentals
    if f.empty or "SYMBOL" not in f.columns:
        return None
    r = f[f["SYMBOL"] == sym]
    if r.empty or not bool(r["_fetched_ok"].iloc[0]):
        return None
    r = r.iloc[0]
    return bool((r.get("roce_pct") or -1) >= 12 and (r.get("debt_to_equity") if r.get("debt_to_equity") is not None else 99) <= 1
                and (r.get("cfo_last_year") or -1) > 0 and (r.get("promoter_drop_qoq_pp") or 0) <= 2
                and (r.get("market_cap_inr") or 0) >= 5e9)


def run(ctx) -> tuple[list, list]:
    ecfg = ctx.cfg.get("engines", {}).get("swing", {})
    ts = ctx.data_timestamp()
    stats = common.setup_stats(ctx, SETUP)
    if not stats:
        return [], [NoTrade("swing", ts, [f"setup {SETUP} missing from the registry"])]

    uni = score_universe(ctx)
    top = uni.head(TOP_N)
    regime = (ctx.ms.get("broad") or {}).get("regime", "RISK_NEU")
    gap_allow = float(ecfg.get("gap_allowance_atr", 1.0))
    med_days = int((stats.get("stats") or {}).get("median_days", 20) or 20)
    n500_ret63 = None
    out = []
    for sym, r in top.iterrows():
        px, atr = float(r["CLOSE"]), float(r["ATR14"])
        stop = px - 2 * atr
        cps = ctx.cost_per_share(sym, px)
        sector = ctx.sectors.get(sym, "Unknown")
        sec_ev, sec = common.sector_evidence(ctx, sector)
        s200 = ctx.C[sym].tail(200).mean()
        above200 = bool(px > s200)
        qg = _quality_pass(ctx, sym)
        gap5 = ctx.adverse_gap_atr(sym)

        ev = [
            Evidence("technical", f"skip-month momentum: 3-1 {r['MOM_3_1']:+.1%}, 6-1 {r['MOM_6_1']:+.1%}",
                     "nse bhavcopy (split-adjusted)", ts),
            Evidence("technical", f"90-day volatility {r['VOL_90D']:.0%} (lower ranks better)",
                     "nse bhavcopy (split-adjusted)", ts),
            Evidence("technical", f"composite rank {r['SCORE_PCT']:.1%} of {len(uni)} liquid names; "
                                  f"{'above' if above200 else 'below'} 200-day average",
                     "research_system.engines.swing", ts),
        ]
        if sec_ev:
            ev.append(sec_ev)
        ev += common.macro_evidence(ctx, sector)
        if qg is not None:
            ev.append(Evidence("fundamental", f"quality gate {'PASS' if qg else 'FAIL'} "
                                              "(ROCE>=12, D/E<=1, CFO>0, promoter drop<=2pp, mcap>=500 Cr)",
                               f"https://www.screener.in/company/{sym}/consolidated/", "screener snapshot"))
        upcoming = common.upcoming_events(ctx, sym)
        for e in upcoming.itertuples():
            ev.append(Evidence("event", f"{e.purpose} on {e.event_date}", e.source_url, e.event_date))

        checks = [above200,
                  bool(sec and (sec.get("rs_vs_n500_63d") or 0) > 0),
                  bool(sec and sec.get("above_200dma")),
                  regime != "RISK_OFF"]
        if qg is not None:
            checks.append(qg)
        comp = {
            "signal_strength": (conf.pct_score(r["SCORE_PCT"], 0.90, 1.0), f"rank percentile {r['SCORE_PCT']:.1%}"),
            "agreement": (sum(checks) / len(checks), f"{sum(checks)}/{len(checks)} independent checks agree "
                          "(200-DMA, sector RS, sector trend, regime, quality gate)"),
            "regime_fit": common.regime_component(stats, regime),
            "historical_results": common.historical_component(stats),
            "liquidity": common.liquidity_component(float(r["ADV20"])),
            "data_freshness": common.freshness_component(ctx, "equity_eod", "indices"),
            "model_certainty": (0.4, "backtest only; sleeve weights were revised on overlapping data"),
        }
        caps = common.outcome_cap(stats)
        cnf = conf.grade(comp, caps)

        unit_risk = (2 + gap_allow) * atr + cps
        risks = [f"gap: 5th-percentile overnight gap {gap5:+.2f} ATR over the last year" if gap5 is not None
                 else "gap: history too short to estimate",
                 "stop is a planned control; a gap through it fills at the open, not the stop"]
        if not upcoming.empty:
            risks.append("event: scheduled results/ex-date within 45 days (see evidence)")
        cand = Candidate(
            engine="swing", symbol=sym, segment="cash", instrument="NSE EQ (delivery)", direction="long",
            horizon=f"days to weeks (median historical hold {med_days} sessions)", data_timestamp=ts,
            setup=SETUP,
            thesis=(f"{sym} ranks in the top {TOP_N} of the liquid NSE universe on skip-month momentum "
                    f"combined with low volatility. The setup has a backtested record (see historical "
                    f"results); it is a statistical edge across many trades, not a forecast for this stock."),
            plan=Plan(entry_trigger="buy at the next session's open if it opens inside the entry zone",
                      entry_low=round(px - 0.5 * atr, 2), entry_high=round(px + 1.0 * atr, 2),
                      stop=round(stop, 2),
                      invalidation=f"close below {stop:.2f} (entry reference - 2 ATR); then trail at highest close - 4 ATR",
                      targets=[round(px + 4 * atr, 2), round(px + 6 * atr, 2)],
                      time_exit="none in the validated rule; re-check the rank after 60 sessions",
                      holding_window=f"median {med_days} sessions",
                      review_by=common.sessions_ahead(ctx.asof, 60)),
            evidence=ev,
            scenarios=[Scenario("stop hit (incl. gap allowance + charges)", round(stop, 2), round(-unit_risk, 2)),
                       Scenario("+2R reference", round(px + 4 * atr, 2), round(4 * atr - cps, 2)),
                       Scenario("+3R reference", round(px + 6 * atr, 2), round(6 * atr - cps, 2)),
                       Scenario("flat after hold", round(px, 2), round(-cps, 2))],
            risks=risks,
            missing_inputs=["no live quote: entry reference is the last close"] +
                           (["fundamentals unavailable for the quality check"] if qg is None else []),
            cancel_conditions=["next open outside the entry zone",
                               "results or ex-date announced within 3 days of entry",
                               "market regime worsens before entry (the risk engine re-sizes on the next run)",
                               f"falls out of the top {TOP_N * 2} ranks before entry"],
            confidence=cnf,
            unit_risk_inr=round(unit_risk, 4), unit_capital_inr=px, unit_notional_inr=px,
            unit_loss_parts={"stop": 2 * atr, "gap": gap_allow * atr, "costs": cps},
            sector=sector, adv_inr=float(r["ADV20"]), price=px, overnight=True,
            hold_until=common.sessions_ahead(ctx.asof, med_days),
        )
        out.append(cand)
    nt = [] if out else [NoTrade("swing", ts, ["no names passed the universe filter"])]
    return out, nt
