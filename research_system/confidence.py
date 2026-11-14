"""Confidence grade (A-E) with visible components.

A raw ranking score is NOT a probability. The grade is a transparent weighted
summary of seven components, each 0..1 with a note. Hard caps (unvalidated
setup, stale data, non point-in-time fundamentals ...) lower the grade and are
listed. A probability is attached only when calibration.lookup() finds enough
out-of-sample outcomes for the same setup and horizon.
"""
from __future__ import annotations

from research_system.candidate import Confidence
from research_system.config import GRADES

WEIGHTS = {
    "signal_strength": 0.20,      # how extreme the setup reading is within its universe
    "agreement": 0.15,            # independent evidence pointing the same way
    "regime_fit": 0.15,           # setup's historical behaviour in the current regime
    "historical_results": 0.20,   # out-of-sample / backtest expectancy and sample size
    "liquidity": 0.10,            # ease of entry/exit at the planned size
    "data_freshness": 0.10,       # source quality and recency
    "model_certainty": 0.10,      # 1 - model uncertainty (validation, PIT data, sample size)
}
THRESHOLDS = [("A", 0.80), ("B", 0.65), ("C", 0.50), ("D", 0.35)]


def _grade_for(score: float) -> str:
    for g, t in THRESHOLDS:
        if score >= t:
            return g
    return "E"


def grade(components: dict, caps: list | None = None, probability: dict | None = None) -> Confidence:
    """components: name -> (score 0..1, note). caps: [(max_grade, reason)]."""
    comp = {}
    total, wsum = 0.0, 0.0
    for name, w in WEIGHTS.items():
        s, note = components.get(name, (None, "not assessed"))
        if s is None:
            # Missing evidence is uncertainty, not neutrality: score it low.
            s, note = 0.25, f"not assessed ({note})" if note != "not assessed" else "not assessed"
        s = float(max(0.0, min(1.0, s)))
        comp[name] = {"score": round(s, 3), "weight": w, "note": note}
        total += w * s
        wsum += w
    score = total / wsum
    g = _grade_for(score)
    applied = []
    for max_g, reason in caps or []:
        if GRADES.index(g) < GRADES.index(max_g):
            g = max_g
        applied.append(f"max {max_g}: {reason}")
    conf = Confidence(grade=g, score=round(score, 3), components=comp, caps=applied)
    if probability:
        conf.probability = probability
        conf.calibration_note = (f"calibrated on {probability['n']} out-of-sample outcomes, "
                                 f"{probability['period']}")
    return conf


def pct_score(x: float | None, lo: float, hi: float) -> float | None:
    """Linear map of x from [lo, hi] to [0, 1]."""
    if x is None:
        return None
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))
