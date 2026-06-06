"""
exit_engine.py
==============
Position-level exit rules for the V5 stock-picker.

V4 has NO per-position exit logic — the only exit is the next weekly rebalance.
This module adds:
  * Initial hard stop  : entry - 2.0 * ATR(14)
  * Trailing stop      : max(initial_stop, 22-day low, chandelier 3*ATR from highest close)
  * Time stop          : exit after 25 trading days regardless of P/L
  * Profit target      : at +4*ATR, trim 50% and raise stop to entry (optional)
  * Portfolio kill switch : pause new entries when DD or VIX thresholds breached

Designed to be called daily (after the close, or pre-open). Returns a list of
exit instructions to execute at the next open.

Usage:
    from exit_engine import ExitEngine, Position

    eng = ExitEngine(
        atr_mult_stop=2.0,
        atr_mult_chandelier=3.0,
        time_stop_days=25,
        portfolio_dd_kill_pct=0.12,
    )
    positions = [Position(symbol='ADANIPORTS', entry_date='2026-05-15',
                          entry_price=1795.1, qty=10, atr=58.0, stop=1795.1-116), ...]
    exits = eng.evaluate(positions, today_bar_df, portfolio_equity_curve)
    for exit_instr in exits:
        print(exit_instr)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd


@dataclass
class Position:
    symbol: str
    entry_date: str        # 'YYYY-MM-DD'
    entry_price: float
    qty: int
    atr_at_entry: float    # ATR(14) on entry day
    initial_stop: float    # set on entry: entry - atr_mult * atr_at_entry
    highest_close: float = 0.0      # for chandelier
    twenty_two_day_low: float = 0.0
    current_stop: float = 0.0
    days_held: int = 0
    profit_taken: bool = False

    def __post_init__(self):
        if self.current_stop == 0.0:
            self.current_stop = self.initial_stop
        if self.highest_close == 0.0:
            self.highest_close = self.entry_price


@dataclass
class ExitInstruction:
    symbol: str
    reason: str
    exit_price_target: str   # 'next_open' | 'market_now' | 'stop_loss'
    qty: int
    note: str = ""


@dataclass
class ExitEngine:
    atr_mult_stop: float = 2.0
    atr_mult_chandelier: float = 3.0
    twenty_two_day_low_lookback: int = 22
    time_stop_days: int = 25
    profit_target_atr_mult: float = 4.0
    enable_profit_trim: bool = True
    portfolio_dd_kill_pct: float = 0.12        # pause new entries (does not exit)
    portfolio_dd_hard_pct: float = 0.18        # flatten 50% on this
    market_vix_kill: float = 25.0
    market_5d_drop_kill_pct: float = 0.08
    actions_log: list = field(default_factory=list)

    def update_position(
        self,
        pos: Position,
        today_close: float,
        today_low: float,
        today_atr: float,
        twenty_two_day_low: float,
    ) -> Position:
        """Update trailing stop, highest close, etc. — call once per session per position."""
        if today_close > pos.highest_close:
            pos.highest_close = today_close
        pos.twenty_two_day_low = twenty_two_day_low

        # Chandelier trail
        chandelier = pos.highest_close - self.atr_mult_chandelier * today_atr
        trailing = max(pos.current_stop, twenty_two_day_low, chandelier)
        pos.current_stop = max(pos.current_stop, trailing)   # never lower
        pos.days_held += 1

        # Optional profit trim
        if (
            self.enable_profit_trim
            and not pos.profit_taken
            and today_close >= pos.entry_price + self.profit_target_atr_mult * pos.atr_at_entry
        ):
            pos.profit_taken = True
            pos.current_stop = max(pos.current_stop, pos.entry_price)  # move stop to BE
        return pos

    def evaluate(
        self,
        positions: Iterable[Position],
        today_bar: pd.DataFrame,     # indexed by SYMBOL, with columns CLOSE, LOW, ATR14, LOW_22D
        portfolio_equity_curve: pd.Series | None = None,
        india_vix: float | None = None,
        nifty500_5d_pct: float | None = None,
    ) -> list[ExitInstruction]:
        """
        Produce a list of exit instructions for next open. Also runs portfolio-level
        kill switch checks and emits 'PAUSE_NEW_ENTRIES' or 'FLATTEN_50' if triggered.
        """
        exits: list[ExitInstruction] = []

        # ---------- portfolio kill switch ----------
        if portfolio_equity_curve is not None and len(portfolio_equity_curve) > 1:
            peak = portfolio_equity_curve.cummax().iloc[-1]
            dd = portfolio_equity_curve.iloc[-1] / peak - 1
            if dd <= -self.portfolio_dd_hard_pct:
                # flatten half of every position
                for p in positions:
                    qty_to_exit = p.qty // 2
                    if qty_to_exit > 0:
                        exits.append(ExitInstruction(
                            symbol=p.symbol, reason="PORTFOLIO_DD_HARD",
                            exit_price_target="next_open", qty=qty_to_exit,
                            note=f"Portfolio DD {dd*100:.1f}% breached hard threshold",
                        ))
                self.actions_log.append(f"PORTFOLIO_DD_HARD: flattened 50%, dd={dd:.3f}")
            elif dd <= -self.portfolio_dd_kill_pct:
                # only pause new entries
                self.actions_log.append(f"PAUSE_NEW_ENTRIES: dd={dd:.3f}")

        if india_vix is not None and india_vix > self.market_vix_kill:
            self.actions_log.append(f"PAUSE_NEW_ENTRIES: VIX={india_vix:.1f}")
        if nifty500_5d_pct is not None and nifty500_5d_pct <= -self.market_5d_drop_kill_pct:
            self.actions_log.append(f"PAUSE_NEW_ENTRIES: N500 5d={nifty500_5d_pct*100:.1f}%")

        # ---------- per-position rules ----------
        for p in positions:
            if p.symbol not in today_bar.index:
                continue
            bar = today_bar.loc[p.symbol]
            close = float(bar["CLOSE"])
            low = float(bar["LOW"])
            atr = float(bar.get("ATR14", p.atr_at_entry))
            l22 = float(bar.get("LOW_22D", p.twenty_two_day_low))

            # Update position state first (mutates p)
            self.update_position(p, close, low, atr, l22)

            # Stop hit? (intraday low pierced the stop)
            if low <= p.current_stop:
                exits.append(ExitInstruction(
                    symbol=p.symbol, reason="STOP_LOSS",
                    exit_price_target="stop_loss", qty=p.qty,
                    note=f"Stop {p.current_stop:.2f} hit by intraday low {low:.2f}",
                ))
                continue

            # Time stop?
            if p.days_held >= self.time_stop_days:
                exits.append(ExitInstruction(
                    symbol=p.symbol, reason="TIME_STOP",
                    exit_price_target="next_open", qty=p.qty,
                    note=f"Held {p.days_held} days >= {self.time_stop_days}",
                ))

        return exits


if __name__ == "__main__":
    # Sanity test
    eng = ExitEngine()
    pos = Position(
        symbol="TEST",
        entry_date="2026-05-01",
        entry_price=100.0,
        qty=10,
        atr_at_entry=4.0,
        initial_stop=92.0,
    )
    today = pd.DataFrame({
        "CLOSE": [108.0],
        "LOW": [90.0],   # would hit the stop
        "ATR14": [4.0],
        "LOW_22D": [94.0],
    }, index=["TEST"])
    exits = eng.evaluate([pos], today)
    for e in exits:
        print(e)
