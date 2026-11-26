#!/usr/bin/env python3
"""Validate the built graph (data/graph.db) per docs/specs/graph.md §9 step 7.

Checks:
- node/edge counts by type
- every edge endpoint resolves (no orphans)
- workflow fetch anchors exist as graph node keys
- private ruling nodes present and labelled EV/{authnum}

Usage: python3 scripts/validate_graph.py [--db path]
Exit 0 = green.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "graph.db"))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    n_nodes = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = cur.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    check("graph: nodes + edges present", n_nodes > 50000 and n_edges > 200000,
          f"{n_nodes} nodes, {n_edges} edges")

    print("  node types:")
    for t, c in cur.execute("SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type ORDER BY 2 DESC"):
        print(f"    {t}: {c}")

    print("  edge types:")
    for t, c in cur.execute("SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type ORDER BY 2 DESC"):
        print(f"    {t}: {c}")

    # orphan edges (FK should prevent, but verify)
    orphan = cur.execute("""
        SELECT COUNT(*) FROM graph_edges e
        LEFT JOIN nodes s ON s.id = e.source_id
        LEFT JOIN nodes t ON t.id = e.target_id
        WHERE s.id IS NULL OR t.id IS NULL
    """).fetchone()[0]
    check("graph: no orphan edges", orphan == 0, f"{orphan} orphans")

    # duplicate edge triples (should be zero — UNIQUE constraint)
    dup = cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT source_id, target_id, edge_type, source_doc, COUNT(*) c
            FROM graph_edges GROUP BY 1,2,3,4 HAVING c > 1
        )
    """).fetchone()[0]
    check("graph: no duplicate edges", dup == 0, f"{dup} dup groups")

    # private ruling coverage vs mop-up corpus
    priv_nodes = cur.execute(
        "SELECT COUNT(*) FROM nodes WHERE node_type='private_ruling'").fetchone()[0]
    check("graph: private ruling nodes ~57.6k", 50000 <= priv_nodes <= 58000,
          f"{priv_nodes}")

    # private ruling label format
    bad_label = cur.execute("""
        SELECT COUNT(*) FROM nodes
        WHERE node_type='private_ruling' AND label NOT LIKE 'EV/%'
    """).fetchone()[0]
    check("graph: private ruling labels EV/{authnum}", bad_label == 0, f"{bad_label} bad")

    # private rulings with zero edges (isolated) — mop-up err files expected, report only
    isolated = cur.execute("""
        SELECT COUNT(*) FROM nodes n
        WHERE n.node_type='private_ruling'
          AND NOT EXISTS (SELECT 1 FROM graph_edges e WHERE e.source_id=n.id)
          AND NOT EXISTS (SELECT 1 FROM graph_edges e WHERE e.target_id=n.id)
    """).fetchone()[0]
    print(f"    (info: {isolated} isolated private ruling nodes — mop-up err files)")

    # workflow fetch anchors resolve
    wf_dir = ROOT / "data" / "workflows"
    if wf_dir.is_dir():
        keys = {r[0] for r in cur.execute("SELECT key FROM nodes")}
        keys_lower = {k.lower() for k in keys}
        missing_anchors = []
        total_anchors = 0
        for wf in sorted(wf_dir.glob("*.yaml")):
            txt = wf.read_text()
            import re
            for m in re.finditer(r'fetch:\s*\[([^\]]*)\]', txt):
                for raw in m.group(1).split(","):
                    a = raw.strip().strip('"\'')
                    if not a:
                        continue
                    total_anchors += 1
                    # workflow YAMLs use unprefixed, sometimes lowercase section keys
                    # ("itaa-1997:108-5", "itaa-1936:109ba"); graph nodes are
                    # type-prefixed with tree-case ids ("section:itaa-1936:109BA")
                    if (a in keys or f"section:{a}" in keys
                            or a.lower() in keys_lower
                            or f"section:{a}".lower() in keys_lower):
                        continue
                    missing_anchors.append(f"{wf.stem}:{a}")
        check("graph: workflow fetch anchors resolve", len(missing_anchors) == 0,
              f"{len(missing_anchors)}/{total_anchors} missing: {missing_anchors[:5]}")

    print()
    fails = [r for r in results if not r[1]]
    print(f"{len(results) - len(fails)}/{len(results)} checks passed, {len(fails)} failed")
    for name, _, detail in fails:
        print(f"  FAIL: {name} — {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
