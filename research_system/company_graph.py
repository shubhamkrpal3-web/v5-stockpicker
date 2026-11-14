"""Company Intelligence Graph.

Each relationship is a sourced, dated edge with a confidence level:

    company -> supplier / customer / competitor / peer
            -> geography / commodity / currency / regulator
            -> listed or global company

Disclosed facts (annual report, filing, transcript) are kept apart from
model-inferred links. The graph estimates TRANSMISSION PATHS: which listed
companies an event at one node could reach, through which relationship, and
which line item it would hit (revenue, margin, cost, capex, working capital,
demand).

Seeding: industry peers from the NSE Nifty 500 classification (inferred, low
confidence). Disclosed edges are added from documents with a source link:

    python -m research_system.company_graph --import edges.csv
    edges.csv: src,src_type,relation,dst,dst_type,channel,strength,evidence_url,evidence_text,as_of,disclosed,confidence

    python -m research_system.company_graph --paths "commodity:crude oil" --depth 2
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import deque
from datetime import datetime, timezone

import pandas as pd

from research_system import paths

RELATIONS = {"supplier_of", "customer_of", "competitor_of", "peer_of", "exposed_to", "regulated_by",
             "subsidiary_of", "operates_in", "input_cost", "sells_into"}
CHANNELS = {"revenue", "margin", "costs", "capex", "working_capital", "demand", "funding", "unknown"}


def _db() -> sqlite3.Connection:
    paths.ensure_dirs()
    con = sqlite3.connect(str(paths.GRAPH_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS nodes(
        id TEXT PRIMARY KEY, type TEXT, name TEXT, symbol TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS edges(
        src TEXT, relation TEXT, dst TEXT, channel TEXT, strength REAL,
        evidence_url TEXT, evidence_text TEXT, as_of TEXT, disclosed INTEGER,
        confidence REAL, added_at TEXT,
        PRIMARY KEY(src, relation, dst))""")
    return con


def node_id(kind: str, name: str) -> str:
    return f"{kind}:{name.strip().lower()}"


def add_edge(src: str, src_type: str, relation: str, dst: str, dst_type: str, *, channel: str = "unknown",
             strength: float = 0.5, evidence_url: str = "", evidence_text: str = "", as_of: str = "",
             disclosed: bool = False, confidence: float = 0.3, con: sqlite3.Connection | None = None) -> None:
    if relation not in RELATIONS:
        raise ValueError(f"unknown relation {relation}")
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel}")
    if disclosed and not evidence_url:
        raise ValueError("a disclosed edge needs a source link")
    own = con is None
    con = con or _db()
    s, d = node_id(src_type, src), node_id(dst_type, dst)
    for nid, kind, name in ((s, src_type, src), (d, dst_type, dst)):
        con.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?,?)",
                    (nid, kind, name, name.upper() if kind == "company" else None))
    con.execute("INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (s, relation, d, channel, float(strength), evidence_url, evidence_text[:1000], as_of,
                 int(disclosed), float(confidence), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    if own:
        con.commit()
        con.close()


def seed_peers() -> int:
    """Industry peers from the Nifty 500 list: inferred, low confidence."""
    if not paths.NIFTY500_LIST.exists():
        return 0
    df = pd.read_csv(paths.NIFTY500_LIST)
    con = _db()
    n = 0
    for ind, g in df.groupby("Industry"):
        for sym in g["Symbol"]:
            add_edge(sym, "company", "operates_in", ind, "sector", channel="demand", strength=0.5,
                     evidence_url="https://www.niftyindices.com", evidence_text="NSE industry classification",
                     disclosed=False, confidence=0.6, con=con)
            n += 1
    con.commit()
    con.close()
    return n


def import_csv(path: str) -> int:
    df = pd.read_csv(path)
    con = _db()
    for r in df.itertuples(index=False):
        add_edge(r.src, r.src_type, r.relation, r.dst, r.dst_type, channel=r.channel, strength=float(r.strength),
                 evidence_url=str(r.evidence_url or ""), evidence_text=str(r.evidence_text or ""),
                 as_of=str(r.as_of or ""), disclosed=bool(r.disclosed), confidence=float(r.confidence), con=con)
    con.commit()
    con.close()
    return len(df)


def edges() -> pd.DataFrame:
    con = _db()
    df = pd.read_sql("SELECT * FROM edges", con)
    con.close()
    return df


def transmission_paths(start: str, depth: int = 2, min_confidence: float = 0.0) -> pd.DataFrame:
    """Listed companies reachable from `start` (a node id such as 'commodity:crude oil').

    Edges are walked in both directions (a supplier shock reaches the
    customer). Path confidence is the product of edge confidences, so long or
    inferred chains are visibly weaker."""
    e = edges()
    e = e[e["confidence"] >= min_confidence]
    adj: dict = {}
    for r in e.itertuples(index=False):
        adj.setdefault(r.src, []).append((r.dst, r))
        adj.setdefault(r.dst, []).append((r.src, r))
    out, seen = [], {start}
    q = deque([(start, [], [], 1.0)])
    while q:
        node, path, disc, cf = q.popleft()
        if len(path) >= depth:
            continue
        for nxt, r in adj.get(node, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            p = path + [f"{r.src} -{r.relation}/{r.channel}-> {r.dst}"]
            d = disc + [bool(r.disclosed)]
            c = cf * float(r.confidence)
            if nxt.startswith("company:"):
                out.append({"company": nxt.split(":", 1)[1].upper(), "hops": len(p), "path": " | ".join(p),
                            "path_confidence": round(c, 3), "all_disclosed": all(d)})
            q.append((nxt, p, d, c))
    return pd.DataFrame(out).sort_values(["hops", "path_confidence"], ascending=[True, False]) if out else pd.DataFrame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--import", dest="imp")
    ap.add_argument("--paths")
    ap.add_argument("--depth", type=int, default=2)
    a = ap.parse_args()
    if a.seed:
        print(f"seeded {seed_peers()} company->sector edges")
    if a.imp:
        print(f"imported {import_csv(a.imp)} edges")
    if a.paths:
        print(transmission_paths(a.paths, a.depth).head(40).to_string(index=False))


if __name__ == "__main__":
    main()
