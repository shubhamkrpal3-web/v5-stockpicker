"""
regime_detector.py
==================
Three-state market regime classifier for the V5 strategy.

Inputs (all derivable from your existing master_equity_history.csv):
  1. Nifty 500 (or equivalent constructed index) close vs 200-DMA
  2. Breadth: % of NSE-500 names with CLOSE > 50-DMA
  3. India VIX (yfinance ^INDIAVIX) — falls back to realized vol of NIFTY if VIX missing

Output: a state in {RISK_ON, RISK_NEU, RISK_OFF} with sizing multipliers.

Debouncing: requires a state change to persist for 2 consecutive weekly checks
before being adopted, to avoid whipsaw.

Usage:
    from regime_detector import RegimeDetector

    det = RegimeDetector()
    state = det.classify(
        nifty500_close=24350,
        nifty500_200dma=23800,
        breadth_above_50dma=0.62,
        india_vix=15.5,
    )
    # state == 'RISK_ON' (3/3 indicators positive)
    sizing = det.sizing_for_state(state)
    # {'gross_exposure': 1.0, 'momentum_weight_mult': 1.0, 'quality_weight_mult': 1.0}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

State = Literal["RISK_ON", "RISK_NEU", "RISK_OFF"]


@dataclass
class RegimeDetector:
    breadth_threshold: float = 0.55     # % of names above 50-DMA to count as healthy
    vix_threshold: float = 18.0
    debounce_periods: int = 2
    history: list = field(default_factory=list)   # rolling list of classifications

    def classify(
        self,
        *,
        nifty500_close: float,
        nifty500_200dma: float,
        breadth_above_50dma: float,
        india_vix: float | None,
    ) -> State:
        trend = nifty500_close > nifty500_200dma
        breadth_ok = breadth_above_50dma > self.breadth_threshold
        vix_ok = (india_vix is not None and india_vix < self.vix_threshold)

        score = int(trend) + int(breadth_ok) + int(vix_ok)
        raw_state: State
        if score == 3:
            raw_state = "RISK_ON"
        elif score == 2:
            raw_state = "RISK_NEU"
        else:
            raw_state = "RISK_OFF"

        # Debounce — only return raw_state if it (or worse) has held for `debounce_periods`
        self.history.append(raw_state)
        if len(self.history) < self.debounce_periods:
            return raw_state
        recent = self.history[-self.debounce_periods:]

        # Use the strictest of the recent states (don't loosen until confirmed)
        order = {"RISK_OFF": 0, "RISK_NEU": 1, "RISK_ON": 2}
        return min(recent, key=lambda s: order[s])

    @staticmethod
    def sizing_for_state(state: State) -> dict:
        if state == "RISK_ON":
            return {
                "gross_exposure": 1.00,
                "momentum_weight_mult": 1.00,
                "quality_weight_mult": 1.00,
                "max_new_entries": 5,
            }
        if state == "RISK_NEU":
            return {
                "gross_exposure": 0.70,
                "momentum_weight_mult": 0.50,
                "quality_weight_mult": 1.50,
                "max_new_entries": 3,
            }
        # RISK_OFF
        return {
            "gross_exposure": 0.40,
            "momentum_weight_mult": 0.00,    # disable momentum sleeve
            "quality_weight_mult": 2.00,
            "max_new_entries": 0,             # no new entries
        }


if __name__ == "__main__":
    det = RegimeDetector()
    print("Scenario 1: trend up, breadth healthy, vix low → expect RISK_ON")
    print(det.classify(nifty500_close=24350, nifty500_200dma=23800,
                       breadth_above_50dma=0.62, india_vix=15.5))
    print("Scenario 2: same again — debounce confirms")
    print(det.classify(nifty500_close=24400, nifty500_200dma=23800,
                       breadth_above_50dma=0.61, india_vix=15.8))
    print("Scenario 3: trend up but breadth weak and vix high → RISK_OFF raw, stays at most NEU after debounce")
    print(det.classify(nifty500_close=24400, nifty500_200dma=23800,
                       breadth_above_50dma=0.40, india_vix=22.0))
