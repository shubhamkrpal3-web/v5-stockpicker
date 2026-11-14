"""Audit log, outcome reconciliation and calibration.

Every run appends every candidate (approved AND rejected) and every NO TRADE
to data/research/audit/YYYYMMDD.jsonl with its full content and data
timestamps. After the close, reconcile() replays each past candidate against
the prices that followed and stores the outcome. Those outcomes are genuinely
out-of-sample, so they are what calibrates a probability later.
"""
from __future__ import annotations

import glob
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research_system import paths

OUTCOMES = paths.AUDIT_DIR / "outcomes.parquet"
MIN_CALIBRATION_N = 200


def record(phase: str, candidates: list, no_trades: list, extra: dict | None = None) -> str:
    paths.ensure_dirs()
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    f = paths.AUDIT_DIR / f"{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with open(f, "a", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps({"run_at": run_at, "phase": phase, "type": "candidate", **c.to_dict()},
                                default=str) + "\n")
        for n in no_trades:
            fh.write(json.dumps({"run_at": run_at, "phase": phase, "type": "no_trade", **n.to_dict()},
                                default=str) + "\n")
        if extra:
            fh.write(json.dumps({"run_at": run_at, "phase": phase, "type": "run", **extra}, default=str) + "\n")
    return str(f)


def load_candidates() -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(paths.AUDIT_DIR / "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                if d.get("type") != "candidate":
                    continue
                p = d.get("plan") or {}
                rows.append({"id": d["id"], "run_at": d["run_at"], "engine": d["engine"], "symbol": d["symbol"],
                             "setup": d["setup"], "direction": d["direction"], "status": d["status"],
                             "data_date": str(d["data_timestamp"])[4:14] if str(d["data_timestamp"]).startswith("EOD")
                             else str(d["data_timestamp"])[:10],
                             "entry_low": p.get("entry_low"), "entry_high": p.get("entry_high"),
                             "stop": p.get("stop"), "target": (p.get("targets") or [None])[0],
                             "hold_until": d.get("hold_until"),
                             "grade": (d.get("confidence") or {}).get("grade"),
                             "score": (d.get("confidence") or {}).get("score")})
    df = pd.DataFrame(rows)
    return df.drop_duplicates("id", keep="first") if not df.empty else df


def reconcile(O: pd.DataFrame, H: pd.DataFrame, L: pd.DataFrame, C: pd.DataFrame) -> pd.DataFrame:
    """Replay each cash candidate: fill at the next open if inside the entry zone,
    then first touch of stop or target (stop wins a tie), else mark at hold end."""
    cands = load_candidates()
    if cands.empty:
        return pd.DataFrame()
    done = pd.read_parquet(OUTCOMES) if OUTCOMES.exists() else pd.DataFrame(columns=["id"])
    final = set(done.loc[done.get("final", pd.Series(dtype=bool)).astype(bool), "id"]) if len(done) else set()
    rows = []
    for c in cands.itertuples(index=False):
        if c.id in final or c.engine not in ("swing", "investment") or c.symbol not in C.columns:
            continue
        d = pd.Timestamp(c.data_date)
        after = C.index[C.index > d]
        if len(after) == 0:
            continue
        d1 = after[0]
        op = O.at[d1, c.symbol]
        if not np.isfinite(op):
            continue
        if c.entry_low is not None and c.entry_high is not None and not (c.entry_low <= op <= c.entry_high):
            rows.append({"id": c.id, "setup": c.setup, "engine": c.engine, "symbol": c.symbol, "status": c.status,
                         "entry_date": d1.strftime("%Y-%m-%d"), "filled": False, "final": True})
            continue
        has_hold = isinstance(c.hold_until, str) and c.hold_until.strip() != ""
        end = pd.Timestamp(c.hold_until) if has_hold else d1 + pd.offsets.BDay(20)
        end = max(end, d1)              # a review date before the first fill still gets one session
        win = C.index[(C.index >= d1) & (C.index <= end)]
        hit, exit_px, exit_d = None, None, None
        for t in win:
            lo, hi = L.at[t, c.symbol], H.at[t, c.symbol]
            if c.stop and lo <= c.stop:
                hit, exit_px, exit_d = "stop", min(c.stop, O.at[t, c.symbol]) if t != d1 else c.stop, t
                break
            if c.target and hi >= c.target:
                hit, exit_px, exit_d = "target", c.target, t
                break
        complete = win[-1] >= end if len(win) else False
        if hit is None:
            exit_d = win[-1]
            exit_px = C.at[exit_d, c.symbol]
            hit = "hold_end" if complete else "open"
        seg = C.loc[d1:exit_d, c.symbol]
        risk = op - c.stop if c.stop else np.nan
        rows.append({"id": c.id, "setup": c.setup, "engine": c.engine, "symbol": c.symbol, "status": c.status,
                     "grade": c.grade, "entry_date": d1.strftime("%Y-%m-%d"), "filled": True,
                     "entry": float(op), "exit": float(exit_px), "exit_date": exit_d.strftime("%Y-%m-%d"),
                     "exit_reason": hit, "ret": float(exit_px / op - 1),
                     "r_mult": float((exit_px - op) / risk) if risk and risk > 0 else np.nan,
                     "mfe": float(seg.max() / op - 1), "mae": float(seg.min() / op - 1),
                     "final": hit != "open"})
    if not rows:
        return done
    new = pd.DataFrame(rows)
    out = pd.concat([done[~done["id"].isin(new["id"])], new], ignore_index=True)
    out.to_parquet(OUTCOMES, index=False)
    return out


def calibration(setup: str) -> dict | None:
    """Hit rate of 'positive return at exit' for a setup, only with enough
    finished out-of-sample outcomes."""
    if not OUTCOMES.exists():
        return None
    o = pd.read_parquet(OUTCOMES)
    o = o[(o["setup"] == setup) & o["filled"].astype(bool) & o["final"].astype(bool)]
    if len(o) < MIN_CALIBRATION_N:
        return None
    return {"p": round(float((o["ret"] > 0).mean()), 3), "n": int(len(o)),
            "event": "position closes with a positive return (stop, target or hold end)",
            "horizon": "the setup's hold window", "period": f"{o['entry_date'].min()}..{o['exit_date'].max()}"}


def oos_summary(setup: str) -> dict:
    if not OUTCOMES.exists():
        return {"n": 0}
    o = pd.read_parquet(OUTCOMES)
    o = o[(o["setup"] == setup) & o["filled"].astype(bool) & o["final"].astype(bool)]
    if o.empty:
        return {"n": 0}
    return {"n": int(len(o)), "mean_ret": float(o["ret"].mean()), "win_rate": float((o["ret"] > 0).mean())}
