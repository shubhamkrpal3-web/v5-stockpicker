"""Shared fixtures. TEST VALUES ONLY: the budget numbers below exist to exercise
the risk engine and are not a suggestion of suitable amounts."""
from __future__ import annotations

import copy

import pandas as pd

from research_system import config as cfgmod
from research_system import confidence as conf
from research_system.candidate import Candidate, Leg, Plan
from research_system.portfolio import Portfolio, Position

FRESH = {"datasets": {k: {"status": "FRESH", "last": "20260918", "lag_sessions": 0}
                      for k in ("equity_eod", "indices", "macro", "fno_summary", "fundamentals")}}


def test_config(**over) -> dict:
    cfg = cfgmod.load(path="__does_not_exist__.toml")
    cfg["account"].update(configured=True, capital_inr=1_000_000, fno_margin_inr=200_000)
    cfg["limits"].update(per_trade_max_loss_inr=5_000, daily_loss_limit_inr=20_000, max_drawdown_pct=0.15,
                         max_gross_exposure_pct=1.0, max_net_exposure_pct=1.0, max_position_pct=0.10,
                         max_sector_pct=0.30, max_open_positions=20)
    cfg["_source"] = "tests"
    for k, v in over.items():
        sec, key = k.split("__")
        cfg.setdefault(sec, {})[key] = v
    return cfg


def cand(symbol="AAA", engine="swing", price=100.0, unit_risk=5.0, grade_score=0.7, sector="Healthcare",
         adv=1e9, segment="cash", direction="long", overnight=True, legs=None, hold_until=None,
         loss_parts="split") -> Candidate:
    """loss_parts: "split" states unit_risk as 75% stop + 20% gap allowance + 5% costs; or a dict, or None."""
    comps = {k: (grade_score, "test") for k in conf.WEIGHTS}
    if loss_parts == "split":
        loss_parts = {"stop": unit_risk * 0.75, "gap": unit_risk * 0.2, "costs": unit_risk * 0.05}
    return Candidate(engine=engine, symbol=symbol, segment=segment, instrument="test", direction=direction,
                     horizon="test", data_timestamp="EOD 2026-09-18", setup="test_setup", thesis="test",
                     plan=Plan(entry_trigger="test", stop=price - unit_risk, targets=[price + 2 * unit_risk]),
                     legs=legs or [], confidence=conf.grade(comps), unit_risk_inr=unit_risk,
                     unit_capital_inr=price, unit_notional_inr=price, unit_loss_parts=loss_parts, sector=sector,
                     adv_inr=adv, price=price, overnight=overnight, hold_until=hold_until)


def empty_book(capital=1_000_000) -> Portfolio:
    return Portfolio(capital=capital, positions=[], equity=pd.Series(dtype=float))


def broad(regime="RISK_ON", ra=0.0) -> dict:
    return {"broad": {"regime": regime, "risk_appetite_z": ra}}
