# V5 Build & Deployment Checklist

A personal tracker. Check items off as you complete them. "ME" = I (your assistant) handle in our next session. "YOU" = you act on your machine.

---

## TODAY — 4 actions, ~45 min

- [ ] **YOU** Disable V4 Telegram broadcasts on PC (Task Scheduler → find task → Disable).
- [ ] **YOU** Locate your Telegram bot token + channel ID, save in `telegram_creds.txt`.
- [ ] **YOU** Sign up at **oracle.com/cloud/free** (Mumbai region). Just register, don't provision anything yet.
- [ ] **YOU** Open a free broker account at Upstox (or Fyers / 5paisa) if you don't already have one. API not yet activated.

## THIS WEEK

- [ ] **ME** Build `multifactor_score.py` (50% momentum + 30% low-vol + 20% breadth, quality/value as hooks).
- [ ] **ME** Build `v5_walk_forward.py` and run on full 2023–2026.
- [ ] **ME** Produce a one-page walk-forward report (Sharpe, Sortino, IR, year-by-year).
- [ ] **YOU** Review the walk-forward report. Gate decision: proceed if IR > 0 vs benchmark.

## NEXT 2 WEEKS

- [ ] **ME** Upgrade Telegram broadcast format (regime tag, confidence per pick, stops, sizing).
- [ ] **ME** Implement quality gate fetchers (screener.in + BSE + NSE ASM).
- [ ] **ME** Build position tracker (`positions.csv` ledger).
- [ ] **WE BOTH** Oracle Cloud setup using `setup.sh` script I provide (~2 hours of your time).
- [ ] **WE BOTH** Test signal — receive a "test successful" Telegram message from Oracle.

## WEEKS 4–14 — Paper trading

- [ ] Receive weekly signal Mondays 9:10 AM on Telegram.
- [ ] Track in spreadsheet (template provided): signal price, hypothetical fill, slippage, stops.
- [ ] **DO NOT execute trades with real money.**
- [ ] Weekly review every Saturday — does reality match the signal?
- [ ] After 8 weeks, evaluate: IR > 0 vs Nifty 500 TRI? Drawdown acceptable? Comfortable emotionally?

## WEEK 15+ — Go live (only if paper trading green)

- [ ] Week 15: Deploy ₹50,000, max ₹500 risk per trade.
- [ ] Week 19: If green, scale to ₹1,00,000.
- [ ] Week 27: If green, scale to target capital.
- [ ] **Hard rule:** if 3 consecutive months underperform Nifty 500 TRI, pause and review. Never scale during drawdown.

---

## Telegram message formats you'll receive

**Monday morning weekly signal** — new entries, holds, exits with confidence + stops + sizing.

**Trading day evening** (only if exits fire) — symbol, reason, exit instruction.

**Saturday morning** — weekly PnL summary, regime status, kill-switch status.

---

## Files in this project

- `Stock_Picker_Audit_and_Refinement_Blueprint.docx` — full audit + refinement plan.
- `V5_Action_Checklist.md` — this file.
- `v5_modules/` — Python modules for the V5 architecture.
- `StockPicker_Automation - Copy.zip` — your original V4 build.

---

*Last updated: 27 May 2026*
