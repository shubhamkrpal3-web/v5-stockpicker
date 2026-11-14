"""Risk configuration loading and validation.

The example file supplies structure and non-budget defaults. The user's
config/risk_config.toml overrides it. Budget fields have no usable default:
if they are not set, budget_status() reports what is missing and the risk
engine rejects everything.
"""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path

from research_system import paths

REQUIRED_BUDGET = [
    ("account", "capital_inr"),
    ("limits", "per_trade_max_loss_inr"),
    ("limits", "daily_loss_limit_inr"),
    ("limits", "max_drawdown_pct"),
    ("limits", "max_gross_exposure_pct"),
    ("limits", "max_net_exposure_pct"),
    ("limits", "max_position_pct"),
    ("limits", "max_sector_pct"),
    ("limits", "max_open_positions"),
]
GRADES = ["A", "B", "C", "D", "E"]


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> dict:
    with open(paths.RISK_CONFIG_EXAMPLE, "rb") as f:
        cfg = tomllib.load(f)
    p = Path(path) if path else paths.RISK_CONFIG
    cfg["_source"] = "example (not configured)"
    if p.exists():
        with open(p, "rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
        cfg["_source"] = str(p)
    return cfg


def budget_status(cfg: dict) -> tuple[bool, list[str]]:
    """(configured, problems). Configured only when explicitly enabled and every
    required budget value is a positive number."""
    problems = []
    if not cfg.get("account", {}).get("configured", False):
        problems.append("account.configured is false")
    for sec, key in REQUIRED_BUDGET:
        v = cfg.get(sec, {}).get(key, 0)
        if not isinstance(v, (int, float)) or v <= 0:
            problems.append(f"{sec}.{key} not set")
    lim = cfg.get("limits", {})
    if lim.get("per_trade_max_loss_inr", 0) and cfg["account"].get("capital_inr", 0) \
            and lim["per_trade_max_loss_inr"] > cfg["account"]["capital_inr"]:
        problems.append("per_trade_max_loss_inr exceeds capital_inr")
    return (not problems), problems


def grade_at_least(grade: str, minimum: str) -> bool:
    return GRADES.index(grade) <= GRADES.index(minimum)
