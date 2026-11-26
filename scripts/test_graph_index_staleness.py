#!/usr/bin/env python3.12
"""Prove the neighbourhood index can tell whether it is stale.

The defect this pins: the index was rebuilt only when it was MISSING, so a graph rebuild
(pipeline/graph_etl.py --rebuild drops and recreates nodes and graph_edges) left an index
belonging to a graph that no longer existed.  It looked correct in every way that could be
checked - right table, right shape, plausible counts - and 15,420 was served to users as the
neighbourhood of a node that had 257 edges.

So this test asserts three things:
  1. an index with no recorded fingerprint is treated as stale (not silently trusted),
  2. an index whose recorded fingerprint matches the graph is trusted (no needless rebuild),
  3. after the graph changes underneath it, the index is rebuilt and the counts agree again -
     and that the OLD "does the table exist" test would have accepted the stale index, i.e. the
     test fails if the fingerprint check is removed.

Runs against a COPY of graph.db.  The live database is never opened for writing.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import graph_neighborhood as gn  # noqa: E402

LIVE = ROOT / "data" / "graph.db"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def mismatches(conn: sqlite3.Connection, n: int = 50) -> int:
    bad = 0
    for nid, et, cnt in conn.execute(
        f"SELECT node_id, edge_type, count FROM {gn.INDEX_TABLE} ORDER BY count DESC LIMIT {n}"
    ):
        direct = conn.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE edge_type = ? AND (source_id = ? OR target_id = ?)",
            (et, nid, nid),
        ).fetchone()[0]
        if direct != cnt:
            bad += 1
    return bad


def main() -> int:
    if not LIVE.exists():
        print(f"  no graph at {LIVE} — cannot test")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="graph-stale-")) / "graph.db"
    shutil.copyfile(LIVE, tmp)
    gn.GRAPH_DB = tmp                     # every write in the module now lands here
    conn = sqlite3.connect(tmp)

    print("== 1. an index with no recorded fingerprint is stale, not trusted ==")
    conn.execute(f"DROP TABLE IF EXISTS {gn.META_TABLE}")
    conn.commit()
    check("table exists (so the old check would accept it)", gn._index_exists(conn) is True)
    check("but it is not current", gn._index_is_current(conn) is False,
          "no fingerprint recorded")

    print("== 2. _ensure_index rebuilds it and records the graph it was built from ==")
    gn._ensure_index(conn)
    check("now current", gn._index_is_current(conn) is True)
    check("fingerprint recorded", conn.execute(
        f"SELECT v FROM {gn.META_TABLE} WHERE k='graph_fingerprint'").fetchone() is not None,
        gn._graph_fingerprint(conn))
    check("counts agree with direct SQL after rebuild", mismatches(conn) == 0)

    print("== 3. the graph changes underneath it: stale again, and it heals ==")
    # a real graph change: one new edge touching a node the index already covers
    nid, et = conn.execute(
        f"SELECT node_id, edge_type FROM {gn.INDEX_TABLE} ORDER BY count DESC LIMIT 1").fetchone()
    src = conn.execute("SELECT id FROM nodes WHERE id != ? LIMIT 1", (nid,)).fetchone()[0]
    conn.execute(
        "INSERT INTO graph_edges (source_id, target_id, edge_type) VALUES (?,?,?)", (src, nid, et))
    conn.commit()
    check("index is now stale", gn._index_is_current(conn) is False,
          "edge count changed -> fingerprint differs")
    check("and the OLD check would have trusted it", gn._index_exists(conn) is True,
          "'table exists' is still true — this is why the stale index survived")
    check("the count is wrong while stale", mismatches(conn) > 0,
          f"{mismatches(conn)} of top-50 disagree")
    gn._ensure_index(conn)
    check("after the rebuild the counts agree again", mismatches(conn) == 0)
    check("index current again", gn._index_is_current(conn) is True)

    print("== 4. the check must be able to fail (negative control) ==")
    # if _index_is_current were reduced to the old existence test, step 3 above would pass
    # "stale" spuriously - assert the two predicates genuinely differ on a fresh mutation
    conn.execute(
        "INSERT INTO graph_edges (source_id, target_id, edge_type) VALUES (?,?,?)", (src, nid, et))
    conn.commit()
    exists = gn._index_exists(conn)
    current = gn._index_is_current(conn)
    check("existence and currency disagree after a mutation", exists and not current,
          f"exists={exists} current={current}")

    conn.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
