"""
quality_gate.py
===============
Mandatory fundamental & event filters for the V5 stock-picker.

The single highest-leverage fix for the V4 strategy is to refuse to buy
operator-driven micro-caps that pure momentum keeps surfacing. This module
applies hard pass/fail filters using free public data sources.

Data sources (all free, no API key):
    - screener.in        : fundamental ratios scraped from /company/{symbol}/
    - api.bseindia.com   : shareholding pattern (promoter%, pledge%), board meetings
    - nsearchives.nse... : ASM/GSM lists, sec_band changes
    - yfinance           : fallback for ratios when Screener parse fails

Caching: all fetches are cached to a local SQLite DB (fundamentals_cache.db)
for 24 hours by default. Be a good citizen — keep rate to ~1 req/2 sec.

Usage:
    from quality_gate import QualityGate

    gate = QualityGate(cache_dir="./fundamentals_cache")
    passes, reasons = gate.evaluate("BIOCON")
    # passes is bool; reasons is dict with each filter's status

    # Batch usage:
    universe = ["BIOCON", "MANKIND", "ADANIPORTS", ...]
    pass_mask = gate.batch_evaluate(universe)
    eligible = universe[pass_mask]

NOTE: This is a scaffolding module. The HTTP fetch + parse functions need to
be implemented against the current pages of screener.in / bseindia.com.
Parsing logic is brittle — re-test the scrapers monthly.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import pandas as pd


# -----------------------------------------------------
# Pass/fail criteria
# -----------------------------------------------------
@dataclass
class QualityRules:
    min_roce_pct:         float = 12.0
    max_de_ratio:         float = 1.0   # 1.5 for NBFC/utilities (override per-symbol)
    require_cfo_positive_years: int = 2
    max_promoter_pledge_pct: float = 25.0
    max_promoter_holding_qoq_drop: float = 2.0   # percentage-point QoQ drop
    forbid_asm_stage:     int = 2     # ASM stage >= this is forbidden
    forbid_gsm:           bool = True
    forbid_t2t:           bool = True
    forbid_auditor_change_recent_yrs: int = 2
    min_listing_age_days: int = 540   # ~18 months
    min_market_cap_inr:   float = 5_000_000_000     # ₹500 Cr


@dataclass
class EventRules:
    skip_around_results_days: int = 3
    skip_around_ex_bonus_days: int = 5
    skip_around_ex_split_days: int = 5
    skip_around_ex_rights_days: int = 10
    skip_around_agm_days: int = 1


# -----------------------------------------------------
# Result record
# -----------------------------------------------------
@dataclass
class QualityResult:
    symbol: str
    passed: bool
    reasons_failed: list = field(default_factory=list)
    reasons_passed: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# -----------------------------------------------------
# Cache
# -----------------------------------------------------
class FundamentalsCache:
    def __init__(self, db_path: str | Path):
        # Allow ':memory:' for unit tests or when filesystem is read-only.
        path_str = str(db_path)
        try:
            self.db = sqlite3.connect(path_str)
        except sqlite3.OperationalError:
            # Fallback to in-memory if filesystem can't host SQLite
            self.db = sqlite3.connect(":memory:")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS cache(
                source TEXT, symbol TEXT, fetched_at INTEGER, payload TEXT,
                PRIMARY KEY (source, symbol)
            )
        """)
        self.db.commit()

    def get(self, source: str, symbol: str, max_age_seconds: int = 86_400):
        row = self.db.execute(
            "SELECT fetched_at, payload FROM cache WHERE source=? AND symbol=?",
            (source, symbol),
        ).fetchone()
        if not row:
            return None
        fetched_at, payload = row
        if time.time() - fetched_at > max_age_seconds:
            return None
        return json.loads(payload)

    def put(self, source: str, symbol: str, payload: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO cache(source, symbol, fetched_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (source, symbol, int(time.time()), json.dumps(payload)),
        )
        self.db.commit()


# -----------------------------------------------------
# Stubs for data fetchers — replace with real implementations
# -----------------------------------------------------
def fetch_screener_ratios(symbol: str, cache: FundamentalsCache) -> dict:
    """
    Scrape https://www.screener.in/company/{symbol}/consolidated/

    Returns dict with keys (all floats, may be None):
        roce_pct, roe_pct, debt_to_equity, opm_pct,
        cfo_last_year, cfo_prior_year, sales_growth_3y, pat_growth_3y,
        market_cap_inr
    """
    cached = cache.get("screener", symbol)
    if cached is not None:
        return cached
    # TODO: implement using requests + BeautifulSoup with 2-sec sleep
    # For now return an empty payload so the gate fails closed.
    payload = {
        "roce_pct": None, "roe_pct": None, "debt_to_equity": None,
        "opm_pct": None, "cfo_last_year": None, "cfo_prior_year": None,
        "sales_growth_3y": None, "pat_growth_3y": None,
        "market_cap_inr": None, "_fetched": False,
    }
    cache.put("screener", symbol, payload)
    return payload


def fetch_bse_shareholding(symbol: str, cache: FundamentalsCache) -> dict:
    """
    Fetch shareholding pattern from BSE Corporate Filings.

    Returns dict with:
        promoter_holding_pct, promoter_holding_pct_prev_q,
        promoter_pledge_pct, last_quarter_filed
    """
    cached = cache.get("bse_shp", symbol)
    if cached is not None:
        return cached
    # TODO: implement
    payload = {
        "promoter_holding_pct": None, "promoter_holding_pct_prev_q": None,
        "promoter_pledge_pct": None, "last_quarter_filed": None, "_fetched": False,
    }
    cache.put("bse_shp", symbol, payload)
    return payload


def fetch_nse_surveillance(symbol: str, cache: FundamentalsCache) -> dict:
    """
    Daily NSE ASM/GSM/T2T status.

    Returns:
        in_asm: bool, asm_stage: int, in_gsm: bool, in_t2t: bool, last_updated
    """
    cached = cache.get("nse_surv", symbol, max_age_seconds=8 * 3600)
    if cached is not None:
        return cached
    # TODO: download asm_grade.csv, sec_band.csv, etc.
    payload = {
        "in_asm": False, "asm_stage": 0, "in_gsm": False, "in_t2t": False,
        "_fetched": False,
    }
    cache.put("nse_surv", symbol, payload)
    return payload


def fetch_event_calendar(symbol: str, cache: FundamentalsCache) -> dict:
    """
    Returns next results / AGM / corp-action dates for the symbol.
        next_results_date, next_agm_date, next_ex_bonus_date, ...
    """
    cached = cache.get("events", symbol, max_age_seconds=24 * 3600)
    if cached is not None:
        return cached
    # TODO: parse BSE board meetings + NSE corp_actions CSV
    payload = {
        "next_results_date": None, "next_agm_date": None,
        "next_ex_bonus_date": None, "next_ex_split_date": None,
        "next_ex_rights_date": None, "_fetched": False,
    }
    cache.put("events", symbol, payload)
    return payload


# -----------------------------------------------------
# Main gate
# -----------------------------------------------------
class QualityGate:
    def __init__(
        self,
        cache_dir: str | Path = "./fundamentals_cache",
        rules: QualityRules | None = None,
        events: EventRules | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache = FundamentalsCache(self.cache_dir / "fundamentals_cache.db")
        self.rules = rules or QualityRules()
        self.events = events or EventRules()

    def evaluate(self, symbol: str, today: pd.Timestamp | None = None) -> QualityResult:
        today = pd.Timestamp(today) if today else pd.Timestamp.utcnow().normalize()
        ratios = fetch_screener_ratios(symbol, self.cache)
        shp = fetch_bse_shareholding(symbol, self.cache)
        surv = fetch_nse_surveillance(symbol, self.cache)
        events = fetch_event_calendar(symbol, self.cache)

        r = self.rules
        e = self.events
        res = QualityResult(symbol=symbol, passed=True,
                            raw={"ratios": ratios, "shp": shp, "surv": surv, "events": events})

        def check(condition: bool, label: str):
            if condition:
                res.reasons_passed.append(label)
            else:
                res.passed = False
                res.reasons_failed.append(label)

        # Fundamental checks. If data is missing (_fetched=False), fail closed.
        if not ratios.get("_fetched", False):
            check(False, "RATIOS_NOT_AVAILABLE")
        else:
            check(ratios["roce_pct"] is not None and ratios["roce_pct"] >= r.min_roce_pct,
                  f"ROCE>={r.min_roce_pct}%")
            check(ratios["debt_to_equity"] is not None and ratios["debt_to_equity"] <= r.max_de_ratio,
                  f"D/E<={r.max_de_ratio}")
            cfo_pos = (
                ratios["cfo_last_year"] is not None and ratios["cfo_last_year"] > 0
                and ratios["cfo_prior_year"] is not None and ratios["cfo_prior_year"] > 0
            )
            check(cfo_pos, "CFO positive 2 yrs")
            check(ratios["market_cap_inr"] is not None and ratios["market_cap_inr"] >= r.min_market_cap_inr,
                  f"MarketCap>=₹{r.min_market_cap_inr/1e7:.0f}Cr")

        if not shp.get("_fetched", False):
            check(False, "SHAREHOLDING_NOT_AVAILABLE")
        else:
            check(shp["promoter_pledge_pct"] is not None and shp["promoter_pledge_pct"] < r.max_promoter_pledge_pct,
                  f"Pledge<{r.max_promoter_pledge_pct}%")
            if shp["promoter_holding_pct"] is not None and shp["promoter_holding_pct_prev_q"] is not None:
                drop = shp["promoter_holding_pct_prev_q"] - shp["promoter_holding_pct"]
                check(drop <= r.max_promoter_holding_qoq_drop,
                      f"PromoterHoldingDropQoQ<={r.max_promoter_holding_qoq_drop}pp")
            else:
                check(False, "PROMOTER_HOLDING_HISTORY_MISSING")

        if surv.get("_fetched", False):
            check(not (surv["in_asm"] and surv["asm_stage"] >= r.forbid_asm_stage),
                  f"NotInASMStage>={r.forbid_asm_stage}")
            check(not (r.forbid_gsm and surv["in_gsm"]), "NotInGSM")
            check(not (r.forbid_t2t and surv["in_t2t"]), "NotInT2T")

        # Event window checks (skip near events)
        if events.get("_fetched", False):
            def near(date_str, window):
                if not date_str:
                    return False
                try:
                    d = pd.Timestamp(date_str)
                except Exception:
                    return False
                return abs((d - today).days) <= window
            check(not near(events.get("next_results_date"), e.skip_around_results_days),
                  "NotInResultsWindow")
            check(not near(events.get("next_agm_date"), e.skip_around_agm_days),
                  "NotInAGMWindow")
            check(not near(events.get("next_ex_bonus_date"), e.skip_around_ex_bonus_days),
                  "NotInBonusWindow")
            check(not near(events.get("next_ex_split_date"), e.skip_around_ex_split_days),
                  "NotInSplitWindow")
            check(not near(events.get("next_ex_rights_date"), e.skip_around_ex_rights_days),
                  "NotInRightsWindow")

        return res

    def batch_evaluate(self, symbols: Iterable[str], today=None) -> pd.DataFrame:
        rows = []
        for s in symbols:
            r = self.evaluate(s, today=today)
            rows.append({
                "SYMBOL": s,
                "PASSED": r.passed,
                "N_PASSED": len(r.reasons_passed),
                "N_FAILED": len(r.reasons_failed),
                "FAILED_REASONS": "; ".join(r.reasons_failed),
            })
            time.sleep(0.2)
        return pd.DataFrame(rows)


if __name__ == "__main__":
    import tempfile
    # Use a temp dir to avoid filesystem quirks during demo
    with tempfile.TemporaryDirectory() as tmp:
        gate = QualityGate(cache_dir=tmp)
        test_symbols = ["BIOCON", "MANKIND", "SOLARINDS", "ADANIPORTS"]
        print("Note: this demo runs with EMPTY data — all stocks will FAIL closed.")
        print("Implement the fetch_* functions to enable real evaluation.\n")
        df = gate.batch_evaluate(test_symbols)
        print(df.to_string(index=False))
 tempfile.TemporaryDirectory() as tmp:
        gate = QualityGate(cache_dir=tmp)
        test_symbols = ["BIOCON", "MANKIND", "SOLARINDS", "ADANIPORTS"]
        print("Note: this demo runs with EMPTY data — all stocks will FAIL closed.")
        print("Implement the fetch_* functions to enable real evaluation.\n")
        df = gate.batch_evaluate(test_symbols)
        print(df.to_string(index=False))
