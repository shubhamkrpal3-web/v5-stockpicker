"""
paper_trade_manager.py
======================
Automated paper-trade lifecycle. Replaces the manual Excel tracker.

Runs nightly after the bhavcopy download. For each call:
  1. Loads positions.csv (current open + closed positions ledger)
  2. For each OPEN position, uses today's OHLC to decide:
       - STOP_LOSS hit if today's LOW <= current_stop  -> exit at stop price
       - TIME_STOP if days_held >= max_holding_days    -> exit at today's CLOSE
       - Otherwise update trailing stop using chandelier (highest_close - 3*ATR14)
                                            or 22-day low, whichever is higher
  3. On Monday OPEN trading days, reads the latest daily_live_portfolio.csv
     and opens new positions at today's actual OPEN price for any pick not
     already held.
  4. Recomputes equity_log.csv: cash + open positions valued at today's CLOSE.
  5. Appends any new closed trades to realized_trades.csv.

Outputs:
  data/live_signals/positions.csv          - open + closed positions ledger
  data/live_signals/equity_log.csv         - daily portfolio NAV
  data/live_signals/realized_trades.csv    - completed trade log

Usage from cron / orchestrator:
  python paper_trade_manager.py \
      --master ./data/market_database/master_history.csv \
      --signal ./data/live_signals/daily_live_portfolio.csv \
      --positions ./data/live_signals/positions.csv \
      --equity-log ./data/live_signals/equity_log.csv \
      --realized ./data/live_signals/realized_trades.csv \
      --portfolio-inr 200000
"""
from __future__ import annotations

import argparse
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd


POSITIONS_COLS = [
    "symbol", "signal_date", "entry_date", "entry_price", "qty",
    "size_inr", "initial_stop", "current_stop", "highest_close",
    "atr_at_entry", "status", "exit_date", "exit_price", "exit_reason",
    "realized_pnl_inr", "realized_pct",
]

REALIZED_COLS = [
    "symbol", "entry_date", "entry_price", "qty", "exit_date",
    "exit_price", "exit_reason", "days_held", "pnl_inr", "pnl_pct",
    "r_multiple",
]


def _load_positions(path: Path) -> pd.DataFrame:
    if path.exists():
        df = pd.read_csv(path)
        # Ensure all columns exist
        for c in POSITIONS_COLS:
            if c not in df.columns:
                df[c] = None
        return df[POSITIONS_COLS]
    return pd.DataFrame(columns=POSITIONS_COLS)


def _load_realized(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=REALIZED_COLS)


def _load_master_recent(path: Path, lookback_days: int = 60) -> pd.DataFrame:
    """Load only the last `lookback_days` of OHLCV from master, for speed."""
    df = pd.read_csv(path, low_memory=False)
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    df["DATE"] = df["DATE"].astype(str)
    df["DATE_DT"] = pd.to_datetime(df["DATE"], format="%Y%m%d", errors="coerce")
    df = df[df["DATE_DT"].notna()].copy()
    cutoff = df["DATE_DT"].max() - pd.Timedelta(days=lookback_days * 1.5)
    df = df[df["DATE_DT"] >= cutoff].copy()
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    for c in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _compute_atr14(master: pd.DataFrame, symbol: str, as_of: pd.Timestamp) -> float | None:
    """Return ATR(14) for `symbol` as of date `as_of`, computed from master OHLC."""
    sub = master[(master["SYMBOL"] == symbol) & (master["DATE_DT"] <= as_of)].sort_values("DATE_DT").tail(15)
    if len(sub) < 14:
        return None
    hl = sub["HIGH"] - sub["LOW"]
    hc = (sub["HIGH"] - sub["CLOSE"].shift(1)).abs()
    lc = (sub["LOW"] - sub["CLOSE"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def _today_bar(master: pd.DataFrame) -> tuple[pd.Timestamp, pd.DataFrame]:
    """Returns (today's_date, dataframe indexed by SYMBOL with OHLC for today)."""
    snap_date = master["DATE_DT"].max()
    today = master[master["DATE_DT"] == snap_date].copy()
    today = today.set_index("SYMBOL")[["OPEN", "HIGH", "LOW", "CLOSE"]]
    return snap_date, today


def _update_trailing_stops(positions: pd.DataFrame, master: pd.DataFrame,
                           today_bar: pd.DataFrame, today_date: pd.Timestamp,
                           atr_mult_chandelier: float = 3.0,
                           low_lookback: int = 22) -> pd.DataFrame:
    """For each OPEN position, ratchet the current_stop UP using the higher of:
       - initial_stop
       - chandelier(highest_close - 3*ATR14)
       - 22-day rolling low
       current_stop is never lowered."""
    open_idx = positions.index[positions["status"] == "open"]
    for idx in open_idx:
        sym = positions.at[idx, "symbol"]
        if sym not in today_bar.index:
            continue
        bar = today_bar.loc[sym]
        # Update highest_close watermark
        prev_high = float(positions.at[idx, "highest_close"] or positions.at[idx, "entry_price"])
        new_high = max(prev_high, float(bar["CLOSE"]))
        positions.at[idx, "highest_close"] = new_high

        # Compute candidate stops
        atr = _compute_atr14(master, sym, today_date) or 0
        chandelier = new_high - atr_mult_chandelier * atr if atr else 0

        sub = master[(master["SYMBOL"] == sym) & (master["DATE_DT"] <= today_date)]
        rolling_low = float(sub["LOW"].tail(low_lookback).min()) if len(sub) else 0

        candidates = [
            float(positions.at[idx, "current_stop"]),
            chandelier,
            rolling_low,
        ]
        new_stop = max(c for c in candidates if c)
        # Never lower the stop
        positions.at[idx, "current_stop"] = max(
            float(positions.at[idx, "current_stop"]), new_stop
        )
    return positions


def _check_exits(positions: pd.DataFrame, today_bar: pd.DataFrame,
                 today_date: pd.Timestamp, max_holding_days: int = 25) -> tuple[pd.DataFrame, list]:
    """For each OPEN position, check whether it should be closed today."""
    closed = []
    for idx in positions.index[positions["status"] == "open"]:
        sym = positions.at[idx, "symbol"]
        if sym not in today_bar.index:
            continue
        bar = today_bar.loc[sym]
        entry_date = pd.to_datetime(positions.at[idx, "entry_date"])
        days_held = (today_date - entry_date).days
        current_stop = float(positions.at[idx, "current_stop"])
        entry_price = float(positions.at[idx, "entry_price"])
        qty = float(positions.at[idx, "qty"])
        initial_stop = float(positions.at[idx, "initial_stop"])
        dollar_at_risk = max(qty * (entry_price - initial_stop), 0.01)

        exit_price = None
        exit_reason = None
        if bar["LOW"] <= current_stop:
            exit_price = current_stop
            exit_reason = "STOP_LOSS"
        elif days_held >= max_holding_days:
            exit_price = float(bar["CLOSE"])
            exit_reason = "TIME_STOP"

        if exit_price is not None:
            pnl_inr = (exit_price - entry_price) * qty
            pnl_pct = (exit_price / entry_price - 1) if entry_price else 0
            positions.at[idx, "status"] = "closed"
            positions.at[idx, "exit_date"] = today_date.strftime("%Y-%m-%d")
            positions.at[idx, "exit_price"] = round(exit_price, 2)
            positions.at[idx, "exit_reason"] = exit_reason
            positions.at[idx, "realized_pnl_inr"] = round(pnl_inr, 2)
            positions.at[idx, "realized_pct"] = round(pnl_pct, 4)
            closed.append({
                "symbol": sym, "entry_date": positions.at[idx, "entry_date"],
                "entry_price": entry_price, "qty": qty,
                "exit_date": positions.at[idx, "exit_date"],
                "exit_price": round(exit_price, 2), "exit_reason": exit_reason,
                "days_held": days_held, "pnl_inr": round(pnl_inr, 2),
                "pnl_pct": round(pnl_pct, 4),
                "r_multiple": round(pnl_inr / dollar_at_risk, 2),
            })
    return positions, closed


def _open_new_positions(positions: pd.DataFrame, signal: pd.DataFrame,
                         today_bar: pd.DataFrame, today_date: pd.Timestamp,
                         master: pd.DataFrame) -> pd.DataFrame:
    """On Monday, open new positions at today's open for any signal pick not already held."""
    if signal.empty:
        return positions
    open_syms = set(positions.loc[positions["status"] == "open", "symbol"].astype(str).str.upper())
    new_rows = []
    for _, pick in signal.iterrows():
        sym = str(pick.get("SYMBOL", "")).upper()
        if not sym or sym in open_syms:
            continue
        if sym not in today_bar.index:
            print(f"[WARN] {sym} not in today's bhavcopy — skipping entry")
            continue
        entry = float(today_bar.loc[sym, "OPEN"])
        if entry <= 0:
            continue
        size_inr = float(pick.get("SIZE_INR", 0))
        qty = int(size_inr / entry) if entry > 0 else 0
        if qty < 1:
            continue
        signal_stop = float(pick.get("STOP_PRICE", 0))
        atr = _compute_atr14(master, sym, today_date) or 0
        new_rows.append({
            "symbol": sym,
            "signal_date": pick.get("DATE", ""),
            "entry_date": today_date.strftime("%Y-%m-%d"),
            "entry_price": round(entry, 2),
            "qty": qty,
            "size_inr": round(qty * entry, 2),
            "initial_stop": round(signal_stop, 2),
            "current_stop": round(signal_stop, 2),
            "highest_close": round(entry, 2),
            "atr_at_entry": round(atr, 2),
            "status": "open",
            "exit_date": "", "exit_price": "", "exit_reason": "",
            "realized_pnl_inr": "", "realized_pct": "",
        })
    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if positions.empty:
            positions = new_df
        else:
            # Align columns to avoid pandas deprecation warning about empty/NA dtypes
            positions = pd.concat(
                [positions.reindex(columns=new_df.columns.union(positions.columns)), new_df],
                ignore_index=True,
            )
    return positions


def _compute_equity(positions: pd.DataFrame, today_bar: pd.DataFrame,
                    today_date: pd.Timestamp, portfolio_inr: float) -> dict:
    """Compute today's NAV: starting cash + realized P&L + open positions' MTM."""
    realized_pnl = pd.to_numeric(
        positions.loc[positions["status"] == "closed", "realized_pnl_inr"], errors="coerce"
    ).fillna(0).sum()

    open_positions = positions[positions["status"] == "open"].copy()
    invested_capital = pd.to_numeric(open_positions["size_inr"], errors="coerce").fillna(0).sum()
    mtm_value = 0.0
    for _, p in open_positions.iterrows():
        sym = str(p["symbol"]).upper()
        if sym in today_bar.index:
            close = float(today_bar.loc[sym, "CLOSE"])
            qty = float(p["qty"])
            mtm_value += close * qty
        else:
            mtm_value += float(p["size_inr"])

    cash = portfolio_inr - invested_capital + realized_pnl
    equity = cash + mtm_value
    return {
        "DATE": today_date.strftime("%Y-%m-%d"),
        "EQUITY": round(equity, 2),
        "CASH": round(cash, 2),
        "MTM_OPEN": round(mtm_value, 2),
        "INVESTED": round(invested_capital, 2),
        "REALIZED_PNL": round(realized_pnl, 2),
        "N_OPEN": int(len(open_positions)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", required=True)
    ap.add_argument("--signal", default="./data/live_signals/daily_live_portfolio.csv")
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--portfolio-inr", type=float, default=200000.0)
    ap.add_argument("--entry-day", default="mon",
                    help="Day-of-week when new positions are opened. mon/tue/wed/thu/fri or 'any' for daily")
    ap.add_argument("--max-holding-days", type=int, default=25)
    args = ap.parse_args()

    master = _load_master_recent(Path(args.master))
    today_date, today_bar = _today_bar(master)
    print(f"[INFO] today's bhavcopy snapshot: {today_date.date()}")

    positions = _load_positions(Path(args.positions))
    realized_log = _load_realized(Path(args.realized))

    # 1. Update trailing stops on existing open positions
    positions = _update_trailing_stops(positions, master, today_bar, today_date)

    # 2. Check stop & time exits
    positions, closed_today = _check_exits(positions, today_bar, today_date, args.max_holding_days)
    if closed_today:
        realized_log = pd.concat([realized_log, pd.DataFrame(closed_today)], ignore_index=True)
        print(f"[INFO] closed {len(closed_today)} positions today: "
              + ", ".join(f"{c['symbol']}({c['exit_reason']})" for c in closed_today))

    # 3. On Monday, open new positions from the latest signal
    dow_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "any": -1}
    target_dow = dow_map.get(args.entry_day, 0)
    is_entry_day = (target_dow == -1) or (today_date.weekday() == target_dow)

    if is_entry_day and Path(args.signal).exists():
        signal = pd.read_csv(args.signal)
        signal.columns = [c.strip().upper() for c in signal.columns]
        if not signal.empty:
            n_before = len(positions[positions["status"] == "open"])
            positions = _open_new_positions(positions, signal, today_bar, today_date, master)
            n_after = len(positions[positions["status"] == "open"])
            n_new = n_after - n_before
            print(f"[INFO] opened {n_new} new positions today from signal")
    elif not is_entry_day:
        print(f"[INFO] not entry day ({today_date.strftime('%A')}); skipping new entries")

    # 4. Compute today's equity and append to equity log
    eq_row = _compute_equity(positions, today_bar, today_date, args.portfolio_inr)
    if Path(args.equity_log).exists():
        eq_log = pd.read_csv(args.equity_log)
        eq_log = eq_log[eq_log["DATE"] != eq_row["DATE"]]
        eq_log = pd.concat([eq_log, pd.DataFrame([eq_row])], ignore_index=True)
    else:
        eq_log = pd.DataFrame([eq_row])
    eq_log = eq_log.sort_values("DATE").reset_index(drop=True)

    # 5. Save outputs
    Path(args.positions).parent.mkdir(parents=True, exist_ok=True)
    positions.to_csv(args.positions, index=False)
    eq_log.to_csv(args.equity_log, index=False)
    realized_log.to_csv(args.realized, index=False)

    # 6. Summary
    print()
    print(f"=== {today_date.date()} summary ===")
    print(f"  Open positions: {eq_row['N_OPEN']}")
    print(f"  Cash:           ₹{eq_row['CASH']:,.2f}")
    print(f"  MTM of open:    ₹{eq_row['MTM_OPEN']:,.2f}")
    print(f"  Realized P&L:   ₹{eq_row['REALIZED_PNL']:,.2f}")
    print(f"  Total equity:   ₹{eq_row['EQUITY']:,.2f}")
    print(f"  Return vs ₹{args.portfolio_inr:,.0f}: "
          f"{(eq_row['EQUITY'] / args.portfolio_inr - 1) * 100:+.2f}%")


if __name__ == "__main__":
    main()
