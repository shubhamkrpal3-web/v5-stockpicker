"""The recommendation record every engine emits and the risk engine decides on.

A Candidate is an idea, not an order. Its status starts as "candidate" and
only the central risk engine can move it to "approved" or "rejected".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Evidence:
    kind: str            # fundamental | technical | macro | sector | event | flows | derivatives
    text: str
    source: str          # URL or dataset name
    as_of: str           # date/time the evidence refers to


@dataclass
class Leg:
    instrument: str      # EQ | FUT | CE | PE
    symbol: str
    side: str            # BUY | SELL
    price: float         # reference price (last close / settlement)
    expiry: Optional[str] = None
    strike: Optional[float] = None
    ratio: int = 1       # lots (or shares for EQ) per unit of the structure
    lot_size: int = 1
    iv: Optional[float] = None
    delta: Optional[float] = None
    oi: Optional[float] = None


@dataclass
class Plan:
    entry_trigger: str
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop: Optional[float] = None             # invalidation price of the underlying
    invalidation: str = ""
    targets: list = field(default_factory=list)
    time_exit: str = ""
    holding_window: str = ""
    review_by: Optional[str] = None          # date the idea must be re-checked


@dataclass
class Sizing:
    quantity: int = 0                        # shares, or units of the structure (lots)
    capital_inr: float = 0.0
    margin_inr: float = 0.0
    max_planned_loss_inr: float = 0.0
    exposure_after: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    underlying_price: Optional[float]
    pnl_inr: float                           # per unit, after estimated charges and slippage
    note: str = ""


@dataclass
class Confidence:
    grade: str
    score: float
    components: dict                         # name -> {"score": 0..1, "note": str}
    caps: list = field(default_factory=list)  # reasons the grade was capped
    probability: Optional[dict] = None       # only once calibrated out of sample
    calibration_note: str = "uncalibrated: a probability is shown only after out-of-sample calibration"


@dataclass
class Candidate:
    engine: str                              # investment | swing | fno | intraday_short
    symbol: str
    segment: str                             # cash | fno
    instrument: str                          # human description, e.g. "Bull call spread 26-Oct 2000/2100"
    direction: str                           # long | short
    horizon: str
    data_timestamp: str
    setup: str
    thesis: str
    plan: Plan
    evidence: list = field(default_factory=list)
    legs: list = field(default_factory=list)
    scenarios: list = field(default_factory=list)
    risks: list = field(default_factory=list)            # gap, liquidity, expiry, event ...
    missing_inputs: list = field(default_factory=list)
    cancel_conditions: list = field(default_factory=list)
    confidence: Optional[Confidence] = None
    # Inputs the risk engine needs for sizing (per unit of `quantity`)
    unit_risk_inr: float = 0.0               # planned loss per unit at the stop incl. gap + charges
    unit_capital_inr: float = 0.0            # cash needed per unit
    unit_margin_inr: float = 0.0             # margin needed per unit (derivatives)
    unit_notional_inr: float = 0.0           # exposure per unit (delta-adjusted for options)
    # unit_risk_inr broken into {"stop", "gap", "costs"} (rupees per unit); a part that is None or missing is
    # not stated, and the shared allocator then rejects the candidate rather than assume it is zero
    unit_loss_parts: Optional[dict] = None
    sector: str = "Unknown"
    adv_inr: float = 0.0
    price: float = 0.0
    overnight: bool = True
    hold_until: Optional[str] = None         # YYYYMMDD; event-risk window end
    # Decision
    status: str = "candidate"                # candidate | approved | rejected
    sizing: Optional[Sizing] = None
    risk_checks: list = field(default_factory=list)       # [{"check", "result", "detail"}]

    @property
    def id(self) -> str:
        key = f"{self.engine}|{self.symbol}|{self.instrument}|{self.setup}|{self.data_timestamp}"
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


@dataclass
class NoTrade:
    """Explicit NO TRADE state for an engine, with the reasons."""
    engine: str
    data_timestamp: str
    reasons: list

    def to_dict(self) -> dict:
        return {"engine": self.engine, "status": "no_trade", "data_timestamp": self.data_timestamp,
                "reasons": self.reasons}
