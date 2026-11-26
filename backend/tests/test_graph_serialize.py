"""G2 gate — LLM-facing serialization (spec §6.2).

Integrity checks:
  1. depth=1 token budget ≤ 80 on the spec node, a hub (s 8-1) and a leaf
  2. depth=2 token budget ≤ 400 on the same nodes
  3. round-trip: serialize → parse → counts equal direct SQL COUNT per edge
     type (level 1), and level-2 counts equal the SQL aggregation over the
     documented frontier (top-20 neighbours by degree, excluding level-1 rows)
  4. level-2 exemplars are deduped against level-1 exemplars
  5. unknown keys → None, never crash
  6. budget enforcement drops lines (never the header) to fit a hard cap
  7. /api/graph/serialize endpoint: 200 + budget + 404 for unknown key
"""
from __future__ import annotations

import os
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from dotenv import load_dotenv

load_dotenv()
os.environ.pop("AZURE_CLIENT_ID", None)
os.environ.pop("LEGISLATION_BEARER_TOKEN", None)

from backend.main import app  # noqa: E402
from backend.tests.helpers import admin_token  # noqa: E402
from backend.services.graph_serialize import (  # noqa: E402
    D1_TOP,
    D2_TOP,
    GRAPH_DB,
    L2_FRONTIER,
    parse_block,
    serialize,
)

SPEC_NODE = "section:itaa-1997:118-110"
HUB = "section:itaa-1997:8-1"
LEAF = "private_ruling:EV/1011261243735"

client = TestClient(app)
client.cookies.set("session", admin_token())


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True, timeout=10)


def _node_id(conn, key: str) -> int:
    return conn.execute("SELECT id FROM nodes WHERE key=?", (key,)).fetchone()[0]


def _sql_counts(conn, nid: int) -> dict[str, int]:
    """Independent SQL reimplementation of the level-1 count rule.

    Counts DISTINCT neighbours per edge type under the serving rules:
    public-ruling `applies` edges duplicate `interpreted_by` and are dropped,
    and Product Rulings are not a section's interpreters. When an edge type
    reaches more than one neighbour node type, the dominant type wins (the
    serializer's count line and exemplars must describe one population).
    """
    ctype = conn.execute("SELECT node_type FROM nodes WHERE id=?", (nid,)).fetchone()[0]
    rows = conn.execute(
        "SELECT DISTINCT e.edge_type, n.id, n.label, n.node_type "
        "FROM graph_edges e "
        "JOIN nodes n ON n.id = CASE WHEN e.source_id=? THEN e.target_id ELSE e.source_id END "
        "WHERE e.source_id=? OR e.target_id=?",
        (nid, nid, nid),
    ).fetchall()
    by_type: dict[str, dict[str, int]] = {}
    for et, _nb_id, nb_label, nb_type in rows:
        if et == "applies" and nb_type == "public_ruling":
            continue
        if et == "interpreted_by" and ctype == "section" and re.match(r"^PR[\s_]*\d", nb_label or ""):
            continue
        by_type.setdefault(et, {})
        by_type[et][nb_type] = by_type[et].get(nb_type, 0) + 1
    return {et: max(types.values()) for et, types in by_type.items()}


def _expected_level2(conn, nid: int) -> dict[str, int]:
    """Independent SQL reimplementation of the documented level-2 frontier."""
    nbr_ids = [r[0] for r in conn.execute(
        "SELECT node_id FROM ("
        " SELECT source_id node_id FROM graph_edges WHERE target_id=?"
        " UNION SELECT target_id FROM graph_edges WHERE source_id=?)",
        (nid, nid),
    )]
    if not nbr_ids:
        return {}
    ph = ",".join("?" * len(nbr_ids))
    deg = dict(conn.execute(
        "SELECT node_id, COUNT(*) FROM ("
        " SELECT source_id node_id FROM graph_edges"
        " UNION ALL SELECT target_id FROM graph_edges)"
        f" WHERE node_id IN ({ph}) GROUP BY node_id",
        nbr_ids,
    ).fetchall())
    frontier = sorted(nbr_ids, key=lambda i: deg.get(i, 0), reverse=True)[:L2_FRONTIER]
    phf = ",".join("?" * len(frontier))
    rows = conn.execute(
        f"SELECT id, source_id, target_id, edge_type FROM graph_edges "
        f"WHERE source_id IN ({phf}) OR target_id IN ({phf})",
        frontier + frontier,
    ).fetchall()
    l1_ids = {r[0] for r in conn.execute(
        "SELECT id FROM graph_edges WHERE source_id=? OR target_id=?", (nid, nid))}
    counts: dict[str, int] = {}
    seen: set[int] = set()
    for rid, s, t, et in rows:
        if rid in seen or rid in l1_ids:
            continue
        seen.add(rid)
        counts[et] = counts.get(et, 0) + 1
    return counts


def test_d1_budget_spec_node():
    out = serialize(SPEC_NODE, depth=1)
    assert out is not None
    assert out["tokens"] <= 80
    assert out["text"].startswith("## ")


def test_d1_budget_hub_and_leaf():
    hub = serialize(HUB, depth=1)
    leaf = serialize(LEAF, depth=1)
    assert hub is not None and hub["tokens"] <= 80
    assert leaf is not None and leaf["tokens"] <= 80
    # the dominant relation must survive budget truncation on the hub
    assert "APPLIES:" in hub["text"]


def test_d2_budget_hub_and_leaf():
    hub = serialize(HUB, depth=2)
    leaf = serialize(LEAF, depth=2)
    assert hub is not None and hub["tokens"] <= 400
    assert leaf is not None and leaf["tokens"] <= 400
    assert "LEVEL 2:" in hub["text"]
    assert "LEVEL 2:" in leaf["text"]


def test_d1_roundtrip_counts_match_sql():
    conn = _conn()
    try:
        nid = _node_id(conn, SPEC_NODE)
        sql_counts = _sql_counts(conn, nid)
        text = serialize(SPEC_NODE, depth=1, max_tokens=2000)["text"]
        parsed = parse_block(text)
        assert set(parsed["edges"]) == set(sql_counts), (
            f"edge types differ: parsed={set(parsed['edges'])} sql={set(sql_counts)}")
        for et, cnt in sql_counts.items():
            assert parsed["edges"][et]["count"] == cnt, f"{et}: parsed={parsed['edges'][et]['count']} sql={cnt}"
            assert len(parsed["edges"][et]["top"]) <= D1_TOP
    finally:
        conn.close()


def test_d1_roundtrip_counts_match_sql_leaf():
    conn = _conn()
    try:
        nid = _node_id(conn, LEAF)
        sql_counts = _sql_counts(conn, nid)
        text = serialize(LEAF, depth=1, max_tokens=2000)["text"]
        parsed = parse_block(text)
        assert set(parsed["edges"]) == set(sql_counts)
        for et, cnt in sql_counts.items():
            assert parsed["edges"][et]["count"] == cnt, f"{et}: parsed={parsed['edges'][et]['count']} sql={cnt}"
    finally:
        conn.close()


def test_d2_roundtrip_level2_counts_match_sql():
    conn = _conn()
    try:
        nid = _node_id(conn, SPEC_NODE)
        text = serialize(SPEC_NODE, depth=2, max_tokens=5000)["text"]
        parsed = parse_block(text)
        expected = _expected_level2(conn, nid)
        assert parsed["level2"] and expected
        for et, cnt in expected.items():
            assert parsed["level2"][et]["count"] == cnt, (
                f"L2 {et}: parsed={parsed['level2'][et]['count']} sql={cnt}")
            assert len(parsed["level2"][et]["top"]) <= D2_TOP
    finally:
        conn.close()


def test_level2_exemplars_deduped_against_level1():
    out = serialize(LEAF, depth=2, max_tokens=5000)
    assert out is not None
    parsed = parse_block(out["text"])
    l1_labels = {lab for info in parsed["edges"].values() for lab in info["top"]}
    l2_labels = {lab for info in parsed["level2"].values() for lab in info["top"]}
    assert l1_labels.isdisjoint(l2_labels), f"overlap: {l1_labels & l2_labels}"


def test_unknown_key_returns_none():
    assert serialize("bogus:not-a-key") is None


def test_budget_enforcement_drops_lines_not_header():
    out = serialize(HUB, depth=2, max_tokens=100)
    assert out is not None
    assert out["tokens"] <= 100
    assert out["text"].startswith("## ")
    assert len(out["text"].splitlines()) >= 1


def test_endpoint_serialize():
    r = client.get("/api/graph/serialize", params={"key": HUB, "depth": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["key"] == HUB
    assert body["tokens"] <= 400
    assert "LEVEL 2:" in body["text"]

    r404 = client.get("/api/graph/serialize", params={"key": "bogus:key"})
    assert r404.status_code == 404


def test_endpoint_serialize_depth_validation():
    r = client.get("/api/graph/serialize", params={"key": HUB, "depth": 5})
    assert r.status_code == 422
