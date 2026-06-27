"""
email_body_builder.py
======================
Builds a clean, phone-readable PLAINTEXT summary of the V5 daily state,
intended for the *body* of the daily email (the Excel tracker stays attached
for full detail). This makes email a proper standalone channel — useful when
Telegram is unavailable, or just to glance at the key numbers without opening
a spreadsheet.

Reads the same ledger/signal/regime files the rest of the pipeline writes:
    equity_log.csv            - daily NAV (DATE,EQUITY,CASH,MTM_OPEN,INVESTED,REALIZED_PNL,N_OPEN)
    positions.csv             - open + closed positions ledger
    realized_trades.csv       - completed trades
    daily_live_portfolio.csv  - today's freshly generated signal (the picks)
    daily_regime.json         - regime state + triggers
    master_history.csv        - (optional) used to mark open positions to today's close

Prints the body to stdout. The workflow redirects it to a file and passes
that file to email_sender.py via --body-file.

Usage:
    python email_body_builder.py \
        --equity-log data/live_signals/equity_log.csv \
        --positions data/live_signals/positions.csv \
        --realized data/live_signals/realized_trades.csv \
        --signal data/live_signals/daily_live_portfolio.csv \
        --regime-json data/live_signals/daily_regime.json \
        --master data/market_database/master_history.csv \
        > /tmp/email_body.txt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _fmt_inr(v: float) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 10_000_000:
        return f"₹{v / 10_000_000:.2f} Cr"
    if abs(v) >= 100_000:
        return f"₹{v / 100_000:.2f} L"
    return f"₹{v:,.0f}"


def _latest_closes(master_path: Path, symbols: set, lookback_days: int = 10) -> dict:
    """Return {SYMBOL: latest_close} for the given symbols. Empty dict if master absent."""
    if not master_path or not master_path.exists() or not symbols:
        return {}
    try:
        df = pd.read_csv(master_path, low_memory=False)
    except Exception:
        return {}
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    if "DATE" not in df.columns or "SYMBOL" not in df.columns or "CLOSE" not in df.columns:
        return {}
    df["DATE_DT"] = pd.to_datetime(df["DATE"].astype(str), format="%Y%m%d", errors="coerce")
    df = df[df["DATE_DT"].notna()]
    if df.empty:
        return {}
    cutoff = df["DATE_DT"].max() - pd.Timedelta(days=lookback_days * 2)
    df = df[df["DATE_DT"] >= cutoff].copy()
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
    df = df[df["SYMBOL"].isin(symbols)]
    df["CLOSE"] = pd.to_numeric(df["CLOSE"], errors="coerce")
    df = df.dropna(subset=["CLOSE"]).sort_values("DATE_DT")
    if df.empty:
        return {}
    last = df.groupby("SYMBOL")["CLOSE"].last()
    return last.to_dict()


def build_body(
    equity_log: Path,
    positions: Path,
    realized: Path,
    signal: Path,
    regime_json: Path,
    master: Path | None = None,
) -> str:
    today = pd.Timestamp.now()
    today_str = today.strftime("%a %d %b %Y")
    today_iso = today.strftime("%Y-%m-%d")

    # ---- regime ----
    regime_state = "—"
    triggers = {}
    if regime_json and regime_json.exists():
        try:
            with open(regime_json) as f:
                rj = json.load(f)
            regime_state = rj.get("state", "—")
            triggers = rj.get("triggers", {}) or {}
        except Exception:
            pass
    gross = {"RISK_ON": "100%", "RISK_NEU": "70%", "RISK_OFF": "40%"}.get(regime_state, "—")
    trig_str = "  ".join(f"{k} {v}" for k, v in triggers.items()) if triggers else ""

    lines = []
    lines.append(f"V5 DAILY REPORT — {today_str}")
    lines.append(f"Regime: {regime_state}  (gross {gross}){'   ' + trig_str if trig_str else ''}")
    lines.append("")

    # ---- dashboard from equity_log ----
    nav = day_pnl = day_pct = ytd = dd = cash = invested = realized_all = 0.0
    n_open = 0
    if equity_log and equity_log.exists():
        eq = pd.read_csv(equity_log)
        if not eq.empty:
            eq["DATE"] = pd.to_datetime(eq["DATE"], errors="coerce")
            eq = eq.sort_values("DATE").reset_index(drop=True)
            nav = float(eq["EQUITY"].iloc[-1])
            cash = float(eq["CASH"].iloc[-1]) if "CASH" in eq else 0.0
            invested = float(eq["INVESTED"].iloc[-1]) if "INVESTED" in eq else 0.0
            realized_all = float(eq["REALIZED_PNL"].iloc[-1]) if "REALIZED_PNL" in eq else 0.0
            n_open = int(eq["N_OPEN"].iloc[-1]) if "N_OPEN" in eq else 0
            if len(eq) >= 2:
                prev = float(eq["EQUITY"].iloc[-2])
                day_pnl = nav - prev
                day_pct = (nav / prev - 1) * 100 if prev else 0.0
            peak = eq["EQUITY"].cummax().iloc[-1]
            dd = (nav / peak - 1) * 100 if peak else 0.0
            ytd_eq = eq[eq["DATE"].dt.year == today.year]
            if len(ytd_eq) >= 2:
                ytd = (ytd_eq["EQUITY"].iloc[-1] / ytd_eq["EQUITY"].iloc[0] - 1) * 100

        lines.append("PORTFOLIO")
        lines.append(f"  NAV ₹{nav:,.0f}   Day P&L ₹{day_pnl:+,.0f} ({day_pct:+.2f}%)")
        lines.append(f"  YTD {ytd:+.2f}%   DD from peak {dd:+.2f}%")
        lines.append(f"  Cash ₹{cash:,.0f}   Invested ₹{invested:,.0f}   Realized (all-time) ₹{realized_all:+,.0f}")
        lines.append(f"  Open positions: {n_open}")
    else:
        lines.append("PORTFOLIO")
        lines.append("  Paper trading hasn't started yet — ledger is empty.")
    lines.append("")

    # ---- today's exits (from realized_trades) ----
    if realized and realized.exists():
        real = pd.read_csv(realized)
        if not real.empty and "exit_date" in real.columns:
            real["exit_date"] = real["exit_date"].astype(str)
            todays = real[real["exit_date"] == today_iso]
            if not todays.empty:
                lines.append(f"EXITS TODAY ({len(todays)})")
                tot = 0.0
                for r in todays.itertuples():
                    sym = getattr(r, "symbol", "?")
                    reason = getattr(r, "exit_reason", "")
                    px = float(getattr(r, "exit_price", 0) or 0)
                    pnl = float(getattr(r, "pnl_inr", 0) or 0)
                    pct = float(getattr(r, "pnl_pct", 0) or 0) * 100
                    mark = "+" if pnl > 0 else ("-" if pnl < 0 else "=")
                    lines.append(f"  [{mark}] {sym}  {reason} @ ₹{px:.2f}  ({pct:+.2f}% / ₹{pnl:+,.0f})")
                    tot += pnl
                lines.append(f"  Today's realized: ₹{tot:+,.0f}")
                lines.append("")

    # ---- today's signal (the picks) ----
    if signal and signal.exists():
        sig = pd.read_csv(signal)
        sig.columns = [c.strip().upper() for c in sig.columns]
        if not sig.empty:
            lines.append(f"TODAY'S SIGNAL ({len(sig)}) — candidate picks for the next entry day")
            sig = sig.sort_values("CONFIDENCE", ascending=False) if "CONFIDENCE" in sig.columns else sig
            for i, r in enumerate(sig.itertuples(), 1):
                sym = getattr(r, "SYMBOL", "?")
                close = float(getattr(r, "CLOSE", 0) or 0)
                stop = float(getattr(r, "STOP_PRICE", 0) or 0)
                conv = float(getattr(r, "CONFIDENCE", 0) or 0)
                size = float(getattr(r, "SIZE_INR", 0) or 0)
                lines.append(
                    f"  {i:>2}. {sym:<12} Conv {conv:.1f}  Entry@open ₹{close:.1f}  "
                    f"Stop ₹{stop:.1f}  Size {_fmt_inr(size)}"
                )
            lines.append("")
        else:
            lines.append("TODAY'S SIGNAL: no eligible picks today.")
            lines.append("")

    # ---- open positions ----
    if positions and positions.exists():
        pos = pd.read_csv(positions)
        if not pos.empty and "status" in pos.columns:
            opens = pos[pos["status"].astype(str).str.lower() == "open"].copy()
            if not opens.empty:
                opens["symbol"] = opens["symbol"].astype(str).str.upper()
                closes = _latest_closes(master, set(opens["symbol"]), ) if master else {}
                lines.append(f"OPEN POSITIONS ({len(opens)})")
                for r in opens.itertuples():
                    sym = str(getattr(r, "symbol", "?"))
                    entry = float(getattr(r, "entry_price", 0) or 0)
                    stop = float(getattr(r, "current_stop", 0) or 0)
                    cur = closes.get(sym)
                    if cur:
                        pct = (cur / entry - 1) * 100 if entry else 0.0
                        lines.append(
                            f"  {sym:<12} entry ₹{entry:.1f}  now ₹{cur:.1f}  "
                            f"({pct:+.2f}%)  stop ₹{stop:.1f}"
                        )
                    else:
                        lines.append(f"  {sym:<12} entry ₹{entry:.1f}  stop ₹{stop:.1f}")
                lines.append("")

    lines.append("Full detail (MTM, equity curve, closed trades) is in the attached Excel.")
    lines.append("— V5 automation")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--signal", default="./data/live_signals/daily_live_portfolio.csv")
    ap.add_argument("--regime-json", default="./data/live_signals/daily_regime.json")
    ap.add_argument("--master", default=None,
                    help="Optional master_history.csv to mark open positions to today's close.")
    args = ap.parse_args()

    body = build_body(
        equity_log=Path(args.equity_log),
        positions=Path(args.positions),
        realized=Path(args.realized),
        signal=Path(args.signal),
        regime_json=Path(args.regime_json),
        master=Path(args.master) if args.master else None,
    )
    print(body)


if __name__ == "__main__":
    main()
