"""Filesystem layout. Every path is relative to the repository root."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
MARKET_DB = DATA / "market_database"
# RS_MASTER overrides the price master (e.g. a fresher copy outside the repo)
_MASTER_OVERRIDE = os.environ.get("RS_MASTER")
MASTER = Path(_MASTER_OVERRIDE) if _MASTER_OVERRIDE else MARKET_DB / "master_history.csv.gz"
MASTER_CSV = Path(_MASTER_OVERRIDE) if _MASTER_OVERRIDE else MARKET_DB / "master_history.csv"
HISTORY_DIR = MARKET_DB / "history"
CORPORATE_ACTIONS = MARKET_DB / "corporate_actions.csv"
FUNDAMENTALS_DB = DATA / "fundamentals" / "screener.sqlite"
PIT_FUNDAMENTALS = DATA / "fundamentals" / "pit_fundamentals.parquet"
NIFTY500_LIST = ROOT / "nifty500.csv"
V5_POSITIONS = DATA / "live_signals" / "positions.csv"
V5_EQUITY_LOG = DATA / "live_signals" / "equity_log.csv"

RESEARCH = DATA / "research"
INDICES = RESEARCH / "indices.parquet"
MACRO = RESEARCH / "macro.parquet"
FII_DII_CASH = RESEARCH / "fii_dii_cash.csv"
PARTICIPANT_OI = RESEARCH / "participant_oi.parquet"
FNO_DIR = RESEARCH / "fno"
FNO_LOTS = RESEARCH / "fno_lots.csv"
EVENTS_DB = RESEARCH / "events.sqlite"
GRAPH_DB = RESEARCH / "company_graph.sqlite"
CALENDAR = RESEARCH / "nse_holidays.json"
MARKET_STATE = RESEARCH / "market_state.json"
MARKET_STATE_HISTORY = RESEARCH / "market_state_history.parquet"
SETUP_REGISTRY = ROOT / "research_system" / "setup_registry.json"
PORTFOLIO = RESEARCH / "portfolio.csv"
AUDIT_DIR = RESEARCH / "audit"
REPORT_DIR = RESEARCH / "reports"
QUALITY = RESEARCH / "data_quality.json"

CONFIG_DIR = ROOT / "config"
RISK_CONFIG = CONFIG_DIR / "risk_config.toml"
RISK_CONFIG_EXAMPLE = CONFIG_DIR / "risk_config.example.toml"


def ensure_dirs() -> None:
    for d in (RESEARCH, FNO_DIR, AUDIT_DIR, REPORT_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
