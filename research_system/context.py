"""Everything an engine needs for one run, loaded once.

Engines read from the context and never fetch data themselves, so every
candidate in a run is built from the same timestamped snapshot.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research_system import config as cfgmod
from research_system import market_state, paths
from research_system.data import events as events_mod
from research_system.data import fno as fno_mod
from research_system.data import prices, quality
from research_system.portfolio import _sector_map

sys.path.insert(0, str(paths.ROOT / "v5_modules"))
from realistic_cost_model import round_trip_cost  # noqa: E402


@dataclass
class Context:
    cfg: dict
    asof: pd.Timestamp
    C: pd.DataFrame            # adjusted closes (dates x symbols)
    O: pd.DataFrame
    H: pd.DataFrame
    L: pd.DataFrame
    TV: pd.DataFrame
    ms: dict
    quality: dict
    calendar: pd.DataFrame
    fundamentals: pd.DataFrame
    fno_lots: pd.DataFrame
    fno_bhav: pd.DataFrame
    fno_summary: pd.DataFrame
    sectors: dict
    registry: dict
    built_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # ---- derived per-symbol helpers
    def atr14(self) -> pd.Series:
        prev = self.C.shift(1)
        tr = np.maximum(self.H - self.L, np.maximum((self.H - prev).abs(), (self.L - prev).abs()))
        return tr.rolling(14, min_periods=10).mean().iloc[-1]

    def adv20(self) -> pd.Series:
        return self.TV.rolling(20, min_periods=10).mean().iloc[-1]

    def adverse_gap_atr(self, symbol: str, q: float = 0.05) -> float | None:
        """5th-percentile overnight gap over the last year, in ATR units (negative = down)."""
        if symbol not in self.C.columns:
            return None
        gap = (self.O[symbol] / self.C[symbol].shift(1) - 1).tail(252).dropna()
        atr = self.atr14().get(symbol)
        px = self.C[symbol].iloc[-1]
        if len(gap) < 60 or not atr or not px:
            return None
        return float(gap.quantile(q) * px / atr)

    def cost_per_share(self, symbol: str, price: float, notional: float = 50_000) -> float:
        adv = float(self.adv20().get(symbol, 0) or 0)
        return round_trip_cost(notional, adv if adv > 0 else None) * price

    def data_timestamp(self) -> str:
        return f"EOD {self.asof.strftime('%Y-%m-%d')} (built {self.built_at})"

    def fresh(self, *datasets: str) -> bool:
        d = self.quality.get("datasets", {})
        return all(d.get(x, {}).get("status") == "FRESH" for x in datasets)


def _fundamentals() -> pd.DataFrame:
    if not paths.FUNDAMENTALS_DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(str(paths.FUNDAMENTALS_DB))
    rows = con.execute("SELECT symbol, fetched_at, payload FROM sc").fetchall()
    con.close()
    out = []
    for sym, ts, payload in rows:
        try:
            d = json.loads(payload)
        except ValueError:
            continue
        d["SYMBOL"] = str(sym).upper()
        d["FETCHED_AT"] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None
        out.append(d)
    return pd.DataFrame(out)


def load_registry() -> dict:
    if paths.SETUP_REGISTRY.exists():
        return json.loads(paths.SETUP_REGISTRY.read_text())
    return {}


def build(cfg: dict | None = None, lookback_start: str | None = None, rebuild_state: bool = False) -> Context:
    cfg = cfg or cfgmod.load()
    px = prices.load_panel(start=lookback_start or (pd.Timestamp.today() - pd.Timedelta(days=760)).strftime("%Y-%m-%d"))
    piv = {c: px.pivot(index="DATE_DT", columns="SYMBOL", values=c).sort_index()
           for c in ("OPEN", "HIGH", "LOW", "CLOSE", "TRADED_VALUE")}
    ms = market_state.build() if rebuild_state or not paths.MARKET_STATE.exists() else market_state.load()
    asof = piv["CLOSE"].index[-1]
    cal = events_mod.calendar_for(start=(asof - pd.Timedelta(days=5)).strftime("%Y%m%d"))
    return Context(cfg=cfg, asof=asof, C=piv["CLOSE"], O=piv["OPEN"], H=piv["HIGH"], L=piv["LOW"],
                   TV=piv["TRADED_VALUE"], ms=ms, quality=quality.check(),
                   calendar=cal, fundamentals=_fundamentals(), fno_lots=fno_mod.load_lots(),
                   fno_bhav=fno_mod.load_bhav(), fno_summary=fno_mod.load_summary(),
                   sectors=_sector_map(), registry=load_registry())
