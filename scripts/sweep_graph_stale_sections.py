#!/usr/bin/env python3.12
"""The graph ETL's missing stale-node sweep (graph_etl.py is upsert-only).

`pipeline/graph_etl.py` writes nodes with INSERT OR IGNORE and never removes one, so a section
deleted from the corpus stays in data/graph.db and keeps answering: its edges still resolve, so
the ETL's own orphan check (:585) reports 0 and says nothing. This is the sweep that producer
should have: a section node whose `content_ref` file is no longer on disk is stale.

Scope is the producer's scope, not the corpus: only node_type='section' nodes whose content_ref
is `data/<act>/sections/...` for an act in graph_etl.ACTS are candidates, so a node belonging to
another producer (a ruling, a case, a definition, an act the ETL does not walk) can never be
deleted by it. `neighborhood_index` is built by build_neighborhood_index-style tooling from these
same nodes and is left alone; this script reports rows that reference a deleted node instead.

Dry run by default. --commit applies, in one transaction.

Usage:
  /usr/bin/python3.12 scripts/sweep_graph_stale_sections.py [--commit] [--json OUT]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "graph.db"


def _graph_acts() -> list[str]:
    """graph_etl.ACTS, imported rather than copied, so the scope cannot drift."""
    spec = importlib.util.spec_from_file_location("graph_etl", ROOT / "pipeline" / "graph_etl.py")
    if spec is None or spec.loader is None:
        raise SystemExit("cannot import pipeline/graph_etl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.ACTS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="apply (default: report only)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    acts = _graph_acts()
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT id, key, content_ref FROM nodes WHERE node_type = 'section'"
        " AND content_ref LIKE 'data/%/sections/%'").fetchall()

    stale: list[tuple[int, str, str]] = []
    for node_id, key, ref in rows:
        rel = ref.split("data/", 1)[1]
        act = rel.split("/", 1)[0]
        if act not in acts:                 # another producer's node
            continue
        if not (ROOT / ref).is_file():
            stale.append((node_id, key, ref))

    edges = 0
    if stale:
        ids = [s[0] for s in stale]
        marks = ",".join("?" * len(ids))
        edges = conn.execute(
            f"SELECT COUNT(*) FROM graph_edges WHERE source_id IN ({marks})"
            f" OR target_id IN ({marks})", ids + ids).fetchone()[0]
        neighbors = conn.execute(
            f"SELECT COUNT(*) FROM neighborhood_index WHERE node_id IN ({marks})", ids).fetchone()[0]
    else:
        neighbors = 0

    by_act: dict[str, int] = {}
    for _, _, ref in stale:
        by_act[ref.split("data/", 1)[1].split("/", 1)[0]] = \
            by_act.get(ref.split("data/", 1)[1].split("/", 1)[0], 0) + 1

    report = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "producer": "scripts/sweep_graph_stale_sections.py",
        "mode": "commit" if args.commit else "dry-run",
        "acts_in_scope": acts,
        "section_nodes_scanned": len(rows),
        "stale_nodes": len(stale),
        "stale_by_act": by_act,
        "edges_touching_stale_nodes": edges,
        "neighborhood_index_rows_touching_stale_nodes": neighbors,
        "sample": [s[1] for s in stale[:5]],
    }

    if args.commit and stale:
        ids = [s[0] for s in stale]
        marks = ",".join("?" * len(ids))
        with conn:
            conn.execute(f"DELETE FROM graph_edges WHERE source_id IN ({marks})"
                         f" OR target_id IN ({marks})", ids + ids)
            conn.execute(f"DELETE FROM nodes WHERE id IN ({marks})", ids)
        report["deleted_nodes"] = len(stale)
        report["deleted_edges"] = edges
        report["nodes_after"] = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        report["edges_after"] = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    elif args.commit:
        report["deleted_nodes"] = 0
        report["deleted_edges"] = 0

    conn.close()
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
