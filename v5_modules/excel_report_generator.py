"""
excel_report_generator.py
=========================
Builds a multi-sheet daily Excel report from the live ledger files.

Sheets:
  1. Dashboard       — one-line snapshot (NAV, Day P&L, YTD, regime)
  2. Open Positions  — every open holding with MTM, unrealized P&L, stop distance, ATR, days held
  3. Closed Trades   — realized trades log
  4. Equity Curve    — daily NAV history (also serves as data for charting)
  5. Today's Signal  — the latest daily_live_portfolio.csv

Inputs:
  data/live_signals/positions.csv
  data/live_signals/equity_log.csv
  data/live_signals/realized_trades.csv
  data/live_signals/daily_live_portfolio.csv
  data/live_signals/daily_regime.json
  data/market_database/master_history.csv  (for today's close prices to compute MTM)

Output:
  data/reports/V5_Daily_Report_YYYY-MM-DD.xlsx
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import CellIsRule

NAVY = "1F3A5F"
LIGHT_BLUE = "DCE6F1"
GREEN = "C6EFCE"
RED = "FFC7CE"
GREY = "F2F2F2"
YELLOW = "FFEB9C"

header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
header_fill = PatternFill("solid", fgColor=NAVY)
default_font = Font(name="Arial", size=10)
thin = Side(border_style="thin", color="999999")
border = Border(left=thin, right=thin, top=thin, bottom=thin)


def _load_master_today(master_path: Path) -> pd.DataFrame:
    """Last bar from master, indexed by SYMBOL."""
    df = pd.read_csv(master_path, low_memory=False)
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    df["DATE_DT"] = pd.to_datetime(df["DATE"].astype(str), format="%Y%m%d", errors="coerce")
    df = df[df["DATE_DT"].notna()].copy()
    today = df["DATE_DT"].max()
    today_df = df[df["DATE_DT"] == today].copy()
    today_df["SYMBOL"] = today_df["SYMBOL"].astype(str).str.upper()
    for c in ("OPEN", "HIGH", "LOW", "CLOSE"):
        if c in today_df.columns:
            today_df[c] = pd.to_numeric(today_df[c], errors="coerce")
    return today_df.set_index("SYMBOL"), today


def _autosize(ws, max_width: int = 50):
    for col in ws.columns:
        max_len = 0
        col_letter = None
        for cell in col:
            try:
                col_letter = cell.column_letter
            except AttributeError:
                continue
            if cell.value is None:
                continue
            v_len = len(str(cell.value))
            if v_len > max_len:
                max_len = v_len
        if col_letter:
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 10), max_width)


def _styled_header(ws, headers: list, row: int = 1):
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ws.row_dimensions[row].height = 30


def _build_dashboard(ws, positions, equity, regime_state, today_date):
    ws.sheet_view.showGridLines = False
    ws.cell(row=1, column=1, value="V5 DAILY DASHBOARD").font = Font(name="Arial", size=18, bold=True, color=NAVY)
    ws.cell(row=2, column=1, value=f"As of {today_date.strftime('%a %d %b %Y')}").font = Font(name="Arial", size=11, italic=True, color="555555")

    # Compute headline metrics
    if not equity.empty:
        equity = equity.copy()
        equity["DATE"] = pd.to_datetime(equity["DATE"])
        equity = equity.sort_values("DATE")
        latest = equity.iloc[-1]
        prev = equity.iloc[-2] if len(equity) >= 2 else latest
        day_pnl = float(latest["EQUITY"] - prev["EQUITY"])
        day_pnl_pct = day_pnl / prev["EQUITY"] if prev["EQUITY"] else 0

        first_of_year = equity[equity["DATE"].dt.year == today_date.year].iloc[0]["EQUITY"] if len(equity[equity["DATE"].dt.year == today_date.year]) else latest["EQUITY"]
        ytd_pnl_pct = float(latest["EQUITY"]) / first_of_year - 1
        peak = float(equity["EQUITY"].cummax().iloc[-1])
        dd = float(latest["EQUITY"]) / peak - 1

        nav = float(latest["EQUITY"])
        cash = float(latest["CASH"])
        mtm = float(latest["MTM_OPEN"])
        invested = float(latest["INVESTED"])
        realized_total = float(latest["REALIZED_PNL"])
        n_open = int(latest["N_OPEN"])
    else:
        nav = 200000; cash = 200000; mtm = 0; invested = 0; realized_total = 0; n_open = 0
        day_pnl = 0; day_pnl_pct = 0; ytd_pnl_pct = 0; dd = 0

    metrics = [
        ("Portfolio NAV", f"₹{nav:,.0f}"),
        ("Day P&L", f"₹{day_pnl:+,.0f}  ({day_pnl_pct*100:+.2f}%)"),
        ("YTD Return", f"{ytd_pnl_pct*100:+.2f}%"),
        ("Drawdown from Peak", f"{dd*100:+.2f}%"),
        ("Cash on Hand", f"₹{cash:,.0f}"),
        ("Invested Capital", f"₹{invested:,.0f}"),
        ("Mark-to-Market (Open)", f"₹{mtm:,.0f}"),
        ("Realized P&L (All Time)", f"₹{realized_total:+,.0f}"),
        ("Open Positions", f"{n_open}"),
        ("Regime", regime_state),
    ]
    for i, (k, v) in enumerate(metrics, start=4):
        kc = ws.cell(row=i, column=1, value=k)
        vc = ws.cell(row=i, column=2, value=v)
        kc.font = Font(name="Arial", size=11, bold=True)
        vc.font = Font(name="Arial", size=11)
        kc.border = border; vc.border = border
        kc.fill = PatternFill("solid", fgColor=LIGHT_BLUE)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 30


def _build_open_positions(ws, positions, today_bar):
    headers = [
        "Symbol", "Entry Date", "Entry Price", "Qty", "Size Invested",
        "Current Close", "MTM Value", "Unrealized P&L", "Unrealized %",
        "Initial Stop", "Current Stop", "Stop Distance %",
        "ATR(14) Entry", "Days Held",
    ]
    _styled_header(ws, headers)
    if positions.empty:
        ws.cell(row=2, column=1, value="No open positions yet.").font = Font(italic=True, color="555555")
        return
    opens = positions[positions["status"] == "open"].copy()
    if opens.empty:
        ws.cell(row=2, column=1, value="No open positions today.").font = Font(italic=True, color="555555")
        return
    r = 2
    for _, p in opens.iterrows():
        sym = str(p["symbol"]).upper()
        entry_price = float(p["entry_price"])
        qty = float(p["qty"])
        size_inr = float(p["size_inr"])
        current_stop = float(p["current_stop"])
        initial_stop = float(p["initial_stop"])
        atr = float(p["atr_at_entry"]) if pd.notna(p["atr_at_entry"]) else 0
        entry_date = pd.to_datetime(p["entry_date"])
        days_held = (pd.Timestamp.now().normalize() - entry_date).days

        close = float(today_bar.loc[sym, "CLOSE"]) if sym in today_bar.index else entry_price
        mtm_value = close * qty
        unreal_pnl = (close - entry_price) * qty
        unreal_pct = (close - entry_price) / entry_price if entry_price else 0
        stop_dist_pct = (close - current_stop) / close if close else 0

        row_vals = [
            sym, entry_date.strftime("%Y-%m-%d"),
            entry_price, qty, size_inr,
            close, mtm_value, unreal_pnl, unreal_pct,
            initial_stop, current_stop, stop_dist_pct,
            atr, days_held,
        ]
        for col, v in enumerate(row_vals, 1):
            c = ws.cell(row=r, column=col, value=v)
            c.font = default_font
            c.border = border
            c.alignment = Alignment(horizontal="center" if col in (1, 2, 14) else "right")
        # number formats
        ws.cell(row=r, column=3).number_format = "₹#,##0.00"
        ws.cell(row=r, column=4).number_format = "0"
        ws.cell(row=r, column=5).number_format = "₹#,##0"
        ws.cell(row=r, column=6).number_format = "₹#,##0.00"
        ws.cell(row=r, column=7).number_format = "₹#,##0"
        ws.cell(row=r, column=8).number_format = "₹#,##0;[Red](₹#,##0);-"
        ws.cell(row=r, column=9).number_format = "0.00%;[Red]-0.00%;-"
        ws.cell(row=r, column=10).number_format = "₹#,##0.00"
        ws.cell(row=r, column=11).number_format = "₹#,##0.00"
        ws.cell(row=r, column=12).number_format = "0.00%"
        ws.cell(row=r, column=13).number_format = "0.00"
        r += 1
    # Conditional formatting on P&L
    ws.conditional_formatting.add(f"H2:H{r-1}",
        CellIsRule(operator="greaterThan", formula=["0"], fill=PatternFill("solid", fgColor=GREEN)))
    ws.conditional_formatting.add(f"H2:H{r-1}",
        CellIsRule(operator="lessThan", formula=["0"], fill=PatternFill("solid", fgColor=RED)))
    _autosize(ws, max_width=20)


def _build_closed_trades(ws, realized):
    headers = ["Symbol", "Entry Date", "Entry Price", "Qty", "Exit Date", "Exit Price", "Exit Reason", "Days Held", "P&L ₹", "P&L %", "R-Multiple"]
    _styled_header(ws, headers)
    if realized.empty:
        ws.cell(row=2, column=1, value="No closed trades yet.").font = Font(italic=True, color="555555")
        return
    realized = realized.sort_values("exit_date", ascending=False).reset_index(drop=True)
    for i, t in realized.iterrows():
        r = i + 2
        vals = [
            t["symbol"], t["entry_date"], float(t["entry_price"]), float(t["qty"]),
            t["exit_date"], float(t["exit_price"]), t["exit_reason"],
            int(t["days_held"]) if pd.notna(t["days_held"]) else 0,
            float(t["pnl_inr"]), float(t["pnl_pct"]), float(t.get("r_multiple", 0)),
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=col, value=v)
            c.font = default_font
            c.border = border
        ws.cell(row=r, column=3).number_format = "₹#,##0.00"
        ws.cell(row=r, column=4).number_format = "0"
        ws.cell(row=r, column=6).number_format = "₹#,##0.00"
        ws.cell(row=r, column=9).number_format = "₹#,##0;[Red](₹#,##0);-"
        ws.cell(row=r, column=10).number_format = "0.00%;[Red]-0.00%;-"
        ws.cell(row=r, column=11).number_format = '0.00"R"'
    ws.conditional_formatting.add(f"I2:I{len(realized)+1}",
        CellIsRule(operator="greaterThan", formula=["0"], fill=PatternFill("solid", fgColor=GREEN)))
    ws.conditional_formatting.add(f"I2:I{len(realized)+1}",
        CellIsRule(operator="lessThan", formula=["0"], fill=PatternFill("solid", fgColor=RED)))
    _autosize(ws, max_width=20)


def _build_equity_curve(ws, equity):
    headers = ["Date", "Equity", "Cash", "MTM Open", "Invested", "Realized P&L", "Open Positions"]
    _styled_header(ws, headers)
    if equity.empty:
        ws.cell(row=2, column=1, value="No equity history yet.").font = Font(italic=True, color="555555")
        return
    equity = equity.copy()
    equity["DATE"] = pd.to_datetime(equity["DATE"])
    equity = equity.sort_values("DATE")
    for i, e in equity.reset_index(drop=True).iterrows():
        r = i + 2
        vals = [
            e["DATE"].strftime("%Y-%m-%d"),
            float(e["EQUITY"]), float(e["CASH"]), float(e["MTM_OPEN"]),
            float(e["INVESTED"]), float(e["REALIZED_PNL"]), int(e["N_OPEN"]),
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=col, value=v)
            c.font = default_font
            c.border = border
        for col in (2, 3, 4, 5, 6):
            ws.cell(row=r, column=col).number_format = "₹#,##0"
    _autosize(ws, max_width=18)


def _build_signal(ws, signal):
    if signal.empty:
        ws.cell(row=1, column=1, value="No active signal.").font = Font(italic=True, color="555555")
        return
    sig = signal.copy()
    sig.columns = [c.upper() for c in sig.columns]
    cols_order = ["DATE", "SYMBOL", "CLOSE", "STOP_PRICE", "WEIGHT", "SIZE_INR",
                  "CONFIDENCE", "V5_SCORE", "ACTION", "REGIME"]
    available = [c for c in cols_order if c in sig.columns]
    _styled_header(ws, available)
    for i, row in sig.reset_index(drop=True).iterrows():
        r = i + 2
        for col, c in enumerate(available, 1):
            v = row.get(c)
            cell = ws.cell(row=r, column=col, value=float(v) if isinstance(v, (int, float)) else v)
            cell.font = default_font
            cell.border = border
    _autosize(ws, max_width=18)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", default="./data/live_signals/positions.csv")
    ap.add_argument("--equity-log", default="./data/live_signals/equity_log.csv")
    ap.add_argument("--realized", default="./data/live_signals/realized_trades.csv")
    ap.add_argument("--signal", default="./data/live_signals/daily_live_portfolio.csv")
    ap.add_argument("--regime-json", default="./data/live_signals/daily_regime.json")
    ap.add_argument("--master", default="./data/market_database/master_history.csv")
    ap.add_argument("--output-dir", default=".")
    ap.add_argument("--filename", default="V5_Live_Tracker.xlsx",
                    help="Output filename. Default is a constant name so the file overwrites itself daily.")
    args = ap.parse_args()

    positions = pd.read_csv(args.positions) if Path(args.positions).exists() else pd.DataFrame()
    equity = pd.read_csv(args.equity_log) if Path(args.equity_log).exists() else pd.DataFrame()
    realized = pd.read_csv(args.realized) if Path(args.realized).exists() else pd.DataFrame()
    signal = pd.read_csv(args.signal) if Path(args.signal).exists() else pd.DataFrame()

    regime_state = "—"
    if Path(args.regime_json).exists():
        with open(args.regime_json) as f:
            rj = json.load(f)
        regime_state = rj.get("state", "—")

    if Path(args.master).exists():
        today_bar, today_date = _load_master_today(Path(args.master))
    else:
        today_bar = pd.DataFrame(); today_date = pd.Timestamp.now()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / args.filename

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Dashboard"
    _build_dashboard(ws1, positions, equity, regime_state, today_date)
    _build_open_positions(wb.create_sheet("Open Positions"), positions, today_bar)
    _build_closed_trades(wb.create_sheet("Closed Trades"), realized)
    _build_equity_curve(wb.create_sheet("Equity Curve"), equity)
    _build_signal(wb.create_sheet("Today's Signal"), signal)

    wb.save(out_path)
    print(f"[INFO] Wrote {out_path} ({out_path.stat().st_size} bytes)")
    print(out_path)  # Final stdout line — used by the workflow to capture the path


if __name__ == "__main__":
    main()
