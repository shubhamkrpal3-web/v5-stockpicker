"""
Reproduce corrected replay v1 from this archive alone and compare it with outputs/.

    python reproduce.py [--out FOLDER]

1. checks every file listed in SHA256SUMS;
2. runs v5_modules/corrected_replay.py with the archived code and inputs only (relative paths, no network, no
   PYTHONPATH, no bytecode written into the archive), into a NEW folder outside this archive;
3. compares the three ledger files byte for byte with outputs/.
Exit code 0 only if everything matches.
"""
import argparse
import hashlib
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ("positions.csv", "realized_trades.csv", "equity_log.csv")
TESTED = {"python": "3.12.10", "pandas": "3.0.3", "numpy": "2.2.6"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="new folder for the reproduction (default: a temporary folder)")
    a = ap.parse_args()
    bad = []
    for line in (HERE / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        if hashlib.sha256((HERE / rel).read_bytes()).hexdigest() != digest:
            bad.append(rel)
    if bad:
        sys.exit(f"archive files changed: {bad}")
    out = Path(a.out).resolve() if a.out else Path(tempfile.mkdtemp(prefix="corrected_replay_v1_"))
    if out == HERE or HERE in out.parents:
        sys.exit("write the reproduction outside the archive")
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    cmd = [sys.executable, "-W", "ignore", "v5_modules/corrected_replay.py", "--base", "inputs/start_state",
           "--master", "data/market_database/master_history.csv.gz", "--signals", "inputs/signals", "--out", str(out),
           "--start", "20260807", "--fee-evidence", "data/ledger_evidence/historical_entry_fees.csv",
           "--corporate-actions", "data/market_database/corporate_actions.csv",
           "--base-label", "inputs/start_state (recorded ledger at git cea8737)",
           "--price-label", "data/market_database/master_history.csv.gz (git 52fd2fe)"]
    r = subprocess.run(cmd, cwd=HERE, env=env, capture_output=True, text=True)
    if r.returncode:
        sys.exit("replay failed:\n" + r.stdout[-2000:] + r.stderr[-2000:])
    import numpy, pandas  # noqa: E401
    have = {"python": platform.python_version(), "pandas": pandas.__version__, "numpy": numpy.__version__}
    # The ledger writes CSV with the operating system's line ending; outputs/ was produced on Windows (CRLF).
    # A run elsewhere is accepted when the files are identical once line endings are normalised, and says so.
    print(f"reproduction written to {out}")
    ok_all = True
    for f in FILES:
        a, b = (out / f).read_bytes(), (HERE / "outputs" / f).read_bytes()
        if a == b:
            verdict = "identical to outputs/"
        elif a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"):
            verdict = "identical to outputs/ except line endings (written on a different operating system)"
        else:
            verdict, ok_all = "DIFFERS from outputs/", False
        print(f"  {f:22s} {verdict}")
    if have != TESTED:
        print(f"  note: tested with {TESTED}; this run used {have}")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
