"""Event Intelligence.

For each public filing: source and publication time -> affected entities ->
transmission path (company graph) -> estimated financial effect and timing ->
observed price/volume response -> whether expectations appear to have moved.

What this module will NOT do: claim an impact is "not yet priced". It can flag
a POSSIBLE expectation gap only when an estimated financial effect exists for
the event (supplied in data/research/event_estimates.csv with a rationale and
source) and the observed abnormal move is much smaller than that estimate.
Without an estimate the gap is reported as "not assessed".

Inputs are public exchange filings only. Private, leaked or material
non-public information is never an input and must never be sought.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research_system import company_graph, paths
from research_system.data import events as events_mod

MATERIAL = ("credit_rating", "results", "order_win", "regulatory", "m_and_a", "pledge", "management",
            "capital_raise", "clarification")
ESTIMATES = paths.RESEARCH / "event_estimates.csv"   # id, est_effect_pct_mcap, horizon, rationale, source


def _reaction_session(published_at: pd.Timestamp, sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """First session whose close could reflect the filing (after 15:30 IST -> next session)."""
    day = published_at.normalize()
    after_close = published_at.hour * 60 + published_at.minute >= 15 * 60 + 30
    cand = sessions[sessions > day] if after_close else sessions[sessions >= day]
    return cand[0] if len(cand) else None


def response(symbol: str, published_at: pd.Timestamp, C: pd.DataFrame, TV: pd.DataFrame,
             market: pd.Series) -> dict:
    if symbol not in C.columns:
        return {"status": "no price data"}
    s = C[symbol].dropna()
    sess = s.index
    d0 = _reaction_session(pd.Timestamp(published_at), sess)
    if d0 is None:
        return {"status": "awaiting first reaction session"}
    i = sess.get_loc(d0)
    if i < 60:
        return {"status": "insufficient history"}
    r = s.pct_change()
    m = market.reindex(sess).pct_change()
    est = pd.concat([r, m], axis=1, sort=True).iloc[i - 250:i - 5].dropna()
    beta = float(np.cov(est.iloc[:, 0], est.iloc[:, 1])[0, 1] / est.iloc[:, 1].var()) if len(est) > 60 else 1.0
    resid_sd = float((est.iloc[:, 0] - beta * est.iloc[:, 1]).std()) if len(est) > 60 else float(r.iloc[:i].std())
    ar0 = float(r.iloc[i] - beta * m.iloc[i])
    j = min(i + 4, len(sess) - 1)
    car = float((r.iloc[i:j + 1] - beta * m.iloc[i:j + 1]).sum())
    tv = TV[symbol].reindex(sess)
    vol_ratio = float(tv.iloc[i] / tv.iloc[i - 20:i].mean()) if tv.iloc[i - 20:i].mean() > 0 else None
    return {"status": "measured", "reaction_session": d0.strftime("%Y-%m-%d"),
            "abnormal_return_day0": round(ar0, 4), "car_0_to_4": round(car, 4), "days_in_car": j - i + 1,
            "z_day0": round(ar0 / resid_sd, 2) if resid_sd > 0 else None,
            "turnover_vs_20d": round(vol_ratio, 2) if vol_ratio else None, "beta_used": round(beta, 2)}


def expectation_gap(event_id: str, observed: dict, resid_sd_daily: float | None = None) -> dict:
    if not ESTIMATES.exists():
        return {"status": "not assessed", "reason": "no estimated financial effect recorded for this event"}
    est = pd.read_csv(ESTIMATES)
    row = est[est["id"] == event_id]
    if row.empty or observed.get("status") != "measured":
        return {"status": "not assessed", "reason": "no estimate for this event or no measured response"}
    e = float(row["est_effect_pct_mcap"].iloc[0])
    obs = observed["car_0_to_4"]
    noise = (resid_sd_daily or 0.02) * math.sqrt(observed.get("days_in_car", 5)) * 2
    gap = e - obs
    flagged = abs(gap) > noise and abs(obs) < abs(e) / 2
    return {"status": "possible expectation gap" if flagged else "no gap detected",
            "estimated_effect": e, "observed_car": obs, "gap": round(gap, 4), "noise_band": round(noise, 4),
            "rationale": row["rationale"].iloc[0], "source": row["source"].iloc[0],
            "caveat": "an estimate, not knowledge; prices can reflect information the estimate does not"}


def recent(ctx, symbols=None, days: int = 7) -> pd.DataFrame:
    """Material filings for `symbols` in the last `days`, with measured responses."""
    since = (ctx.asof - pd.Timedelta(days=days)).isoformat()
    rows = []
    syms = symbols if symbols is not None else []
    # equal-weighted market proxy is adequate for a 1-5 day abnormal-return estimate
    mkt_level = (1 + ctx.C.pct_change(fill_method=None).mean(axis=1).fillna(0)).cumprod()
    for sym in syms:
        a = events_mod.announcements_for(sym, since=since)
        a = a[a["category"].isin(MATERIAL)]
        for e in a.itertuples(index=False):
            resp = response(sym, pd.Timestamp(e.published_at), ctx.C, ctx.TV, mkt_level)
            paths_ = company_graph.transmission_paths(f"company:{sym.lower()}", depth=1)
            rows.append({"symbol": sym, "published_at": e.published_at, "category": e.category,
                         "subject": e.subject, "source_url": e.source_url, **resp,
                         "graph_links": int(len(paths_)),
                         "expectation_gap": expectation_gap(e.id, resp)["status"]})
    return pd.DataFrame(rows)
