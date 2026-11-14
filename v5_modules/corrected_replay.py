"""
corrected_replay.py
===================
Rebuilds the paper ledger's history from a recorded state, one session at a time, with the current ledger
code (paper_trade_manager.run) and the exit rules and fees in force on each date (PAPER_RULES). Used when a
correction - e.g. a merger the ledger had not applied - changes what happened from some session on: every
later entry, exit, cash balance and slot is recomputed in order, instead of inserting a back-dated exit into
a history that was produced without it.

The recorded ledger stays the original history. This writes a separate, labelled copy (never into
data/live_signals):
  OUT/positions.csv, OUT/realized_trades.csv, OUT/equity_log.csv
      equity-log and realised-trade rows dated from the start session on have SOURCE = "corrected replay";
      earlier rows "recorded"
  OUT/REPLAY_MANIFEST.json
      the inputs: base state, price file, the signal file used on each entry day, rules, fee evidence

Inputs
  --base      positions.csv, equity_log.csv, realized_trades.csv as recorded just before the start session
  --master    price file containing every session to be replayed
  --signals   folder of YYYYMMDD.csv: the signal file the ledger read on that entry day (missing = no entries)
  --start     first session to rebuild (YYYYMMDD); the base equity log must end before it

Usage:
  python corrected_replay.py --base B --master M --signals S --out O --start 20260807 [--end 20261006]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paper_trade_manager as ptm  # noqa: E402

LIVE_DIR = Path(__file__).resolve().parent.parent / "data" / "live_signals"
LABEL, RECORDED = "corrected replay", "recorded"
FILES = ("positions.csv", "equity_log.csv", "realized_trades.csv")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cut(full: tuple, d: str) -> tuple:
    """The loaded market as a run on session d would see it: nothing after d."""
    raw, atr, lows, tv, sessions = full
    keep = lambda s: s[s.index.get_level_values(1) <= d] if len(s) else s
    return keep(raw), keep(atr), {n: keep(v) for n, v in lows.items()}, keep(tv), [x for x in sessions if x <= d]


def _label(out: Path, start: str) -> None:
    """SOURCE on every equity-log and realised-trade row: recorded before `start`, corrected replay from it."""
    for name, col in (("equity_log.csv", "DATE"), ("realized_trades.csv", "exit_date")):
        p = out / name
        if not p.exists():
            continue
        df = pd.read_csv(p, dtype={col: str})
        if col not in df.columns or df.empty:
            continue
        when = df[col].astype(str).str.replace("-", "").str[:8]
        df["SOURCE"] = [LABEL if w >= start else RECORDED for w in when]
        df.to_csv(p, index=False)


def rebuild(base_dir, master, signals_dir, out_dir, start: str, end: str | None = None, *,
            fee_evidence=ptm.FEE_EVIDENCE, corporate_actions=None, portfolio_inr: float = 200000.0,
            entry_day: str = "mon", max_open_positions: int = 15, max_holding_days: int = 0,
            base_label: str = "", price_label: str = "") -> dict:
    out = Path(out_dir).resolve()
    live = LIVE_DIR.resolve()
    if out == live or live in out.parents:
        raise ValueError("a corrected replay is never written into the live ledger folder")
    base = Path(base_dir)
    eq = pd.read_csv(base / "equity_log.csv", dtype={"DATE": str})
    last = eq["DATE"].str.replace("-", "").max()
    if not last < start:
        raise ValueError(f"the base state already records session {last}; it must end before {start}")
    pos = ptm._load_positions(base / "positions.csv")
    active = pos[pos["status"].isin(ptm.ACTIVE)]
    first = min([ptm._ymd(x) for x in active["entry_date"]] + [start])
    since = (pd.Timestamp(first) - pd.Timedelta(days=45)).strftime("%Y%m%d")
    ca = Path(corporate_actions) if corporate_actions else Path(__file__).resolve().parent.parent / "data" / \
        "market_database" / "corporate_actions.csv"
    lookbacks = tuple(sorted({r[2] for r in ptm.PAPER_RULES if r[2]}))
    full = ptm._load_market(Path(master), since, ca, lookbacks)      # fails before anything is written
    out.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        if (base / f).exists():
            shutil.copyfile(base / f, out / f)
        elif (out / f).exists():
            (out / f).unlink()
    sessions = [s for s in full[4] if s >= start and (end is None or s <= end)]
    if not sessions:
        raise ValueError(f"the price file has no session from {start}")
    signals = {}
    for d in sessions:
        sig = Path(signals_dir) / f"{d}.csv"
        args = argparse.Namespace(master=str(master), signal=str(sig), positions=str(out / "positions.csv"),
                                  equity_log=str(out / "equity_log.csv"), realized=str(out / "realized_trades.csv"),
                                  corporate_actions=str(ca), portfolio_inr=portfolio_inr, entry_day=entry_day,
                                  max_holding_days=max_holding_days, max_open_positions=max_open_positions,
                                  dry_run=False, fee_evidence=str(fee_evidence), counterfactual=False)
        res = ptm.run(args, market=lambda _since, d=d: _cut(full, d))
        ptm.write_outputs(args, res)
        if sig.exists():
            signals[d] = {"file": sig.name, "sha256": _sha(sig)}
    _label(out, start)
    manifest = {
        "label": LABEL,
        "meaning": ("history rebuilt session by session from the recorded state before `start`, with the current "
                    "ledger code and the exit rules and fees in force on each date; the recorded ledger is "
                    "unchanged and remains the original history"),
        "base": base_label or str(base), "start": start, "end": sessions[-1], "sessions": len(sessions),
        "rules": [list(r) for r in ptm.PAPER_RULES], "cost_model_from": ptm.COST_MODEL_FROM,
        "price_file": {"label": price_label or str(master), "sha256": _sha(Path(master))},
        "fee_evidence": {"path": str(fee_evidence),
                         "sha256": _sha(Path(fee_evidence)) if Path(fee_evidence).exists() else None},
        "signals_by_entry_session": signals,
    }
    (out / "REPLAY_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--master", required=True)
    ap.add_argument("--signals", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=None)
    ap.add_argument("--fee-evidence", default=str(ptm.FEE_EVIDENCE))
    ap.add_argument("--corporate-actions", default=None)
    ap.add_argument("--portfolio-inr", type=float, default=200000.0)
    ap.add_argument("--base-label", default="")
    ap.add_argument("--price-label", default="")
    a = ap.parse_args()
    m = rebuild(a.base, a.master, a.signals, a.out, a.start, a.end, fee_evidence=a.fee_evidence,
                corporate_actions=a.corporate_actions, portfolio_inr=a.portfolio_inr, base_label=a.base_label,
                price_label=a.price_label)
    print(f"[INFO] {LABEL}: {m['sessions']} sessions {m['start']}..{m['end']} -> {a.out}")


if __name__ == "__main__":
    main()
