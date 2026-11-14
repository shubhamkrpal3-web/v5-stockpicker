"""Investment engine (months to years).

Looks for businesses with durable economics at a reasonable valuation in
context: quality (returns on capital, balance sheet, cash conversion,
promoter behaviour, growth) and valuation versus industry peers and the
sector index. Output is a thesis with peer-multiple valuation scenarios, an
accumulation range, thesis-breaking conditions and a review date.

Honest limits, surfaced on every candidate:
  * Fundamentals are the current Screener.in snapshot, not point-in-time.
    Until pit_fundamentals.py has ~6-8 quarterly vintages the screen cannot be
    backtested honestly, so the grade is capped.
  * Valuation versus the company's OWN history needs PIT data; only peer and
    sector context is available today.
  * Financials (banks, NBFCs, insurers) need different metrics (NIM, credit
    cost, GNPA, capital adequacy) that are not scraped; they are excluded with
    a reason rather than judged on ROCE / D/E, which do not apply.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research_system import confidence as conf
from research_system.candidate import Candidate, Evidence, NoTrade, Plan, Scenario
from research_system.engines import common

SETUP = "quality_at_reasonable_value"
MAX_CANDIDATES = 10
EXCLUDED_SECTORS = {"Financial Services"}


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def screen(ctx) -> pd.DataFrame:
    f = ctx.fundamentals.copy()
    if f.empty:
        return f
    f = f[f["_fetched_ok"].astype(bool)].copy()
    f["SECTOR"] = f["SYMBOL"].map(ctx.sectors)
    f = f[f["SECTOR"].notna()]                       # peer group needs a classification
    for c in ["roce_pct", "roe_pct", "debt_to_equity", "opm_pct", "stock_pe", "market_cap_inr",
              "cfo_last_year", "cfo_prior_year", "sales_growth_3y_pct", "pat_growth_3y_pct",
              "promoter_drop_qoq_pp"]:
        f[c] = _num(f.get(c))
    price = ctx.C.iloc[-1]
    f["PRICE"] = f["SYMBOL"].map(price)
    f["ADV20"] = f["SYMBOL"].map(ctx.adv20())
    f = f[f["PRICE"].notna()]
    # peer statistics on P/E (positive earnings only)
    pe = f[(f["stock_pe"] > 0) & (f["stock_pe"] < 300)]
    peers = pe.groupby("SECTOR")["stock_pe"].describe(percentiles=[.25, .5, .75])
    f = f.join(peers[["count", "25%", "50%", "75%"]].rename(
        columns={"count": "PEERS", "25%": "PE_P25", "50%": "PE_MED", "75%": "PE_P75"}), on="SECTOR")
    elig = f[~f["SECTOR"].isin(EXCLUDED_SECTORS)].copy()
    q = pd.DataFrame(index=elig.index)
    q["roce"] = elig["roce_pct"].rank(pct=True)
    q["roe"] = elig["roe_pct"].rank(pct=True)
    q["lev"] = 1 - elig["debt_to_equity"].rank(pct=True)
    q["cfo"] = ((elig["cfo_last_year"] > 0) & (elig["cfo_prior_year"] > 0)).astype(float)
    q["growth"] = (elig["sales_growth_3y_pct"].rank(pct=True) + elig["pat_growth_3y_pct"].rank(pct=True)) / 2
    q["promoter"] = (elig["promoter_drop_qoq_pp"].fillna(0) <= 0.5).astype(float)
    elig["QUALITY"] = q.mean(axis=1, skipna=True)
    elig["QUALITY_PCT"] = elig["QUALITY"].rank(pct=True)
    elig["PE_VS_PEER"] = elig["stock_pe"] / elig["PE_MED"]
    return elig


def run(ctx) -> tuple[list, list]:
    ts = ctx.data_timestamp()
    ecfg = ctx.cfg.get("engines", {}).get("investment", {})
    f = screen(ctx)
    if f.empty:
        return [], [NoTrade("investment", ts, ["no fundamentals cache"])]
    pick = f[(f["QUALITY_PCT"] >= 0.80) & (f["stock_pe"] > 0) & (f["PE_VS_PEER"] <= 1.0)
             & (f["PEERS"] >= 5) & (f["market_cap_inr"] >= 5e9) & (f["debt_to_equity"] <= 1)
             & (f["cfo_last_year"] > 0)]
    pick = pick.sort_values(["QUALITY_PCT", "PE_VS_PEER"], ascending=[False, True]).head(MAX_CANDIDATES)
    review_dd = float(ecfg.get("review_drawdown_pct", 0.25))
    sec_pe = {}
    try:
        from research_system.data import indices as idx_mod
        pe_w = idx_mod.wide("PE")
        sec_pe = pe_w.iloc[-1].dropna().to_dict() if not pe_w.empty else {}
    except Exception:
        pass

    out = []
    for _, r in pick.iterrows():
        sym, px = r["SYMBOL"], float(r["PRICE"])
        eps = px / r["stock_pe"]
        v_bear, v_base, v_bull = eps * r["PE_P25"], eps * r["PE_MED"], eps * r["PE_P75"]
        review_px = px * (1 - review_dd)
        sector = r["SECTOR"]
        sec_ev, sec = common.sector_evidence(ctx, sector)
        url = f"https://www.screener.in/company/{sym}/consolidated/"
        fa = r.get("FETCHED_AT") or "unknown date"
        ev = [
            Evidence("fundamental", f"ROCE {r['roce_pct']:.1f}%, ROE {r['roe_pct']:.1f}%, D/E {r['debt_to_equity']:.2f}, "
                                    f"OPM {r['opm_pct']:.1f}%" if pd.notna(r['opm_pct']) else
                     f"ROCE {r['roce_pct']:.1f}%, ROE {r['roe_pct']:.1f}%, D/E {r['debt_to_equity']:.2f}", url, fa),
            Evidence("fundamental", f"operating cash flow last two years: Rs{r['cfo_last_year']:,.0f} Cr / "
                                    f"Rs{r['cfo_prior_year']:,.0f} Cr; 3y sales growth {r['sales_growth_3y_pct']}%, "
                                    f"3y profit growth {r['pat_growth_3y_pct']}%", url, fa),
            Evidence("fundamental", f"promoter holding change last quarter {-(r['promoter_drop_qoq_pp'] or 0):+.2f} pp",
                     url, fa),
            Evidence("fundamental", f"P/E {r['stock_pe']:.1f} vs {int(r['PEERS'])} {sector} peers: "
                                    f"25th {r['PE_P25']:.1f} / median {r['PE_MED']:.1f} / 75th {r['PE_P75']:.1f}",
                     url, fa),
        ]
        if sec_ev:
            ev.append(sec_ev)
        ev += common.macro_evidence(ctx, sector)
        nifty_pe = (ctx.ms.get("broad") or {}).get("valuation", {})
        if nifty_pe:
            ev.append(Evidence("macro", f"Nifty 500 P/E {nifty_pe['nifty500_pe']:.1f} "
                                        f"({nifty_pe['pe_percentile']:.0%} percentile since {nifty_pe['pe_percentile_since']})",
                               "nse index file", ctx.ms.get("as_of", "")))
        upcoming = common.upcoming_events(ctx, sym, days=120)
        for e in upcoming.itertuples():
            ev.append(Evidence("event", f"{e.purpose} on {e.event_date}", e.source_url, e.event_date))
        next_results = next((e.event_date for e in upcoming.itertuples() if "result" in e.purpose), None)

        checks = [r["roce_pct"] >= 15, r["debt_to_equity"] <= 0.5, r["cfo_prior_year"] > 0,
                  (r["sales_growth_3y_pct"] or 0) > 8, r["PE_VS_PEER"] <= 0.85,
                  bool(sec and sec.get("above_200dma"))]
        comp = {
            "signal_strength": (conf.pct_score(r["QUALITY_PCT"], 0.8, 1.0), f"quality percentile {r['QUALITY_PCT']:.0%} "
                                f"among {len(f)} non-financial names; P/E {r['PE_VS_PEER']:.2f}x peer median"),
            "agreement": (sum(map(bool, checks)) / len(checks),
                          f"{sum(map(bool, checks))}/{len(checks)}: ROCE>=15, D/E<=0.5, CFO+ both years, "
                          "sales growth>8%, P/E<=0.85x peers, sector trend"),
            "regime_fit": (None, "no regime study for this setup"),
            "historical_results": (None, "cannot be backtested until point-in-time fundamentals accumulate"),
            "liquidity": common.liquidity_component(float(r["ADV20"] or 0)),
            "data_freshness": common.freshness_component(ctx, "equity_eod", "fundamentals"),
            "model_certainty": (0.3, "current-snapshot fundamentals; peer multiples are not a fair-value model"),
        }
        caps = [("C", "fundamentals are not point-in-time; no historical validation possible yet")]
        cnf = conf.grade(comp, caps)
        cps = ctx.cost_per_share(sym, px)
        unit_risk = px - review_px + cps
        cand = Candidate(
            engine="investment", symbol=sym, segment="cash", instrument="NSE EQ (delivery)", direction="long",
            horizon="months to years", data_timestamp=ts, setup=SETUP,
            thesis=(f"{sym} ({sector}) scores in the top {100 - int(r['QUALITY_PCT'] * 100) + 1}% on returns on "
                    f"capital, balance sheet, cash conversion and growth, while trading at {r['PE_VS_PEER']:.2f}x the "
                    f"median P/E of {int(r['PEERS'])} industry peers. The thesis is quality at a peer-relative "
                    f"discount; it assumes the discount is not explained by risks the screen cannot see."),
            plan=Plan(entry_trigger="accumulate in tranches inside the range; no single-day entry needed",
                      entry_low=round(min(v_bear, px), 2), entry_high=round(min(v_base, px), 2),
                      stop=round(review_px, 2),
                      invalidation=(f"thesis review if price falls below {review_px:.2f} "
                                    f"({review_dd:.0%} below reference) or any thesis-breaking condition triggers"),
                      targets=[round(v_base, 2), round(v_bull, 2)],
                      time_exit="re-underwrite at every results release; exit if the thesis is broken",
                      holding_window="12-36 months", review_by=next_results or common.sessions_ahead(ctx.asof, 63)),
            evidence=ev,
            scenarios=[Scenario("bear: peer 25th pct P/E on trailing EPS", round(v_bear, 2), round(v_bear - px - cps, 2)),
                       Scenario("base: peer median P/E", round(v_base, 2), round(v_base - px - cps, 2)),
                       Scenario("bull: peer 75th pct P/E", round(v_bull, 2), round(v_bull - px - cps, 2)),
                       Scenario("review level hit", round(review_px, 2), round(-unit_risk, 2))],
            risks=["valuation scenarios re-rate trailing EPS only; earnings can fall",
                   "peer group is the NSE industry label, which can mix different businesses",
                   "gap risk on results days"],
            missing_inputs=["point-in-time fundamentals", "valuation vs own history",
                            "consensus estimates (not licensed)", "company relationship graph edges"],
            cancel_conditions=["ROCE falls below 12%", "debt/equity rises above 1", "operating cash flow turns negative",
                               "promoter holding falls > 2 pp in a quarter or pledging rises",
                               f"P/E rises above the peer 75th percentile ({r['PE_P75']:.1f})",
                               "adverse regulatory, governance or credit-rating event"],
            confidence=cnf,
            unit_risk_inr=round(unit_risk, 4), unit_capital_inr=px, unit_notional_inr=px,
            # no gap allowance is configured for this engine, so none is stated (the shared allocator rejects)
            unit_loss_parts={"stop": px - review_px, "gap": None, "costs": cps},
            sector=sector, adv_inr=float(r["ADV20"] or 0), price=px, overnight=True,
            hold_until=next_results,
        )
        out.append(cand)
    nt = []
    if not out:
        nt.append(NoTrade("investment", ts, ["no non-financial company met quality >= 80th pct, "
                                             "P/E <= peer median, D/E <= 1 and positive cash flow"]))
    nt.append(NoTrade("investment", ts, ["Financial Services excluded: bank/NBFC metrics (NIM, credit cost, "
                                         "GNPA, capital adequacy) are not yet collected"]))
    return out, nt
