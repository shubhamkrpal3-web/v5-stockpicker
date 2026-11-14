"""Back-adjusted end-of-day equity prices for the research system.

Wraps the existing V5 master history (and the yearly parquet archive) with the
split/bonus adjustment from v5_modules/corporate_actions.py.
"""
from __future__ import annotations

import sys
from functools import lru_cache

import pandas as pd

from research_system import paths

sys.path.insert(0, str(paths.ROOT / "v5_modules"))
from corporate_actions import adjust_long, load_events, load_price_panel  # noqa: E402


def _master_path():
    return paths.MASTER_CSV if paths.MASTER_CSV.exists() else paths.MASTER


@lru_cache(maxsize=4)
def load_panel(start: str = "2023-01-01", with_history: bool = False) -> pd.DataFrame:
    """Long frame: SYMBOL, DATE, DATE_DT, OHLC (adjusted), VOLUME, TRADED_VALUE."""
    px = load_price_panel(_master_path(), paths.HISTORY_DIR if with_history else None, start=start)
    px["DATE_DT"] = pd.to_datetime(px["DATE"], format="%Y%m%d")
    return adjust_long(px, load_events(paths.CORPORATE_ACTIONS))


def load_symbols(symbols, start: str = "2017-01-01") -> pd.DataFrame:
    with_hist = pd.Timestamp(start) < pd.Timestamp("2023-01-01")
    px = load_panel(start=start, with_history=with_hist)
    return px[px["SYMBOL"].isin([s.upper() for s in symbols])].copy()


def wide(field: str = "CLOSE", start: str = "2023-01-01", with_history: bool = False) -> pd.DataFrame:
    px = load_panel(start=start, with_history=with_history)
    w = px.pivot(index="DATE_DT", columns="SYMBOL", values=field)
    return w.sort_index()


def last_session() -> pd.Timestamp | None:
    px = load_panel()
    return px["DATE_DT"].max() if not px.empty else None
