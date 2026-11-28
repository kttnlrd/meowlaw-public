"""Extensive stress tests for the MCP graph tools (Phase 4).

Covers, beyond test_mcp_graph_tools.py:
  1. every node type serializes at depth 1 and 2 within token caps
  2. hub nodes (s 8-1, s 995-1, TR 2025/1) serialize quickly and correctly
  3. random node sample (n=200): serialize never crashes, token caps hold
  4. path: same-node, adjacent pair, multi-hop, unreachable pair
  5. resolve_alias: >=100 mapped refs resolve to keys that exist in graph.db,
     and regex rules still beat the alias map
  6. neighbourhood edge-type completeness: a node with known neighbours
     reports them with correct counts
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import time

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.pop("AZURE_CLIENT_ID", None)
os.environ.pop("LEGISLATION_BEARER_TOKEN", None)

from backend.fastmcp_server import mcp  # noqa: E402
from backend.services.graph_neighborhood import GRAPH_DB  # noqa: E402

_TOOLS = {t.name: t for t in mcp._tool_manager._tools.values()}
_conn = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True, timeout=30)
    return _conn


def _call(name: str, **kw) -> dict:
    fn = _TOOLS[name].fn
    if asyncio.iscoroutinefunction(fn):
        result = asyncio.run(fn(**kw))
    else:
        result = fn(**kw)
    return json.loads(result)


def _sample_keys(prefix: str, n: int) -> list[str]:
    rows = _db().execute(
        "SELECT key FROM nodes WHERE key LIKE ? ORDER BY RANDOM() LIMIT ?",
        (prefix + ":%", n)).fetchall()
    return [r[0] for r in rows]


@pytest.fixture(scope="module", autouse=True)
def _require_graph():
    if not os.path.exists(GRAPH_DB):
        pytest.skip("graph.db not built")


# ---------------------------------------------------------------- node types

NODE_TYPES = ["section", "public_ruling", "private_ruling", "case", "commentary", "definition"]


@pytest.mark.parametrize("node_type", NODE_TYPES)
def test_every_node_type_serializes(node_type: str):
    keys = _sample_keys(node_type, 3)
    assert keys, f"no {node_type} nodes in graph"
    for key in keys:
        out = _call("graph_neighbourhood", key=key, depth=1)
        assert out.get("key") == key, f"neighbourhood failed for {key}: {out}"
        assert out["tokens"] <= 80
        assert out["text"].startswith("## ")
        out2 = _call("graph_neighbourhood", key=key, depth=2)
        assert out2["tokens"] <= 400


# ------------------------------------------------------------------ hubs

HUB_KEYS = [
    "section:itaa-1997:8-1",
    "section:itaa-1997:995-1",
    "section:itaa-1997:118-110",
    "section:itaa-1936:100A",
    "public_ruling:TR 2025/1",
    "case:[1904] HCA 29",
]


@pytest.mark.parametrize("key", HUB_KEYS)
def test_hub_nodes_fast_and_valid(key: str):
    t0 = time.monotonic()
    out = _call("graph_neighbourhood", key=key, depth=1)
    elapsed = time.monotonic() - t0
    assert out.get("key") == key
    assert elapsed < 5.0, f"hub {key} too slow: {elapsed:.2f}s"
    assert out["tokens"] <= 80


def test_hub_path_speed():
    t0 = time.monotonic()
    out = _call("graph_path", from_key="section:itaa-1997:8-1", to_key="section:itaa-1997:995-1")
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"hub path too slow: {elapsed:.2f}s"
    assert out["path"] is not None


# ------------------------------------------------------------ random sample

def test_random_sample_200_never_crashes():
    all_keys = [r[0] for r in _db().execute("SELECT key FROM nodes ORDER BY RANDOM() LIMIT 200")]
    for key in all_keys:
        out = _call("graph_neighbourhood", key=key, depth=1)
        assert "error" not in out, f"unexpected error for {key}: {out}"
        assert out["tokens"] <= 80, f"{key}: depth1 {out['tokens']} tokens ({out['text'][:100]!r})"
        out2 = _call("graph_neighbourhood", key=key, depth=2)
        assert out2["tokens"] <= 400


# ---------------------------------------------------------------- paths

def test_path_same_node():
    out = _call("graph_path", from_key=HUB_KEYS[0], to_key=HUB_KEYS[0])
    assert out["hops"] == 0
    assert out["path"] == []


def test_path_adjacent_pair():
    # s 995-1 is a hub; find a directly-connected neighbour
    conn = _db()
    nid = conn.execute("SELECT id FROM nodes WHERE key=?", ("section:itaa-1997:995-1",)).fetchone()[0]
    nbr = conn.execute(
        "SELECT n.key FROM graph_edges e JOIN nodes n ON n.id = CASE WHEN e.source_id=? THEN e.target_id ELSE e.source_id END WHERE e.source_id=? OR e.target_id=? LIMIT 1",
        (nid, nid, nid)).fetchone()
    if nbr:
        out = _call("graph_path", from_key="section:itaa-1997:995-1", to_key=nbr[0])
        assert out["hops"] == 1
        assert len(out["edges"]) == 1


def test_path_unreachable_is_graceful():
    # definition nodes are leaves (defines->section); a definition->definition
    # path should be null or a valid path, never an exception
    def_keys = _sample_keys("definition", 2)
    out = _call("graph_path", from_key=def_keys[0], to_key=def_keys[1])
    assert "path" in out  # either a path or path: null with reason
    if out["path"] is None:
        assert out.get("reason")


def test_path_hop_clamp():
    out = _call("graph_path", from_key=HUB_KEYS[0], to_key=HUB_KEYS[1], max_hops=0)
    # clamped to 1; still a valid response
    assert "path" in out or "error" in out


# ---------------------------------------------------------- resolve_alias

def test_alias_sample_resolves_to_real_keys():
    mapping_path = "data/entity_alias_map.json"
    if not os.path.exists(mapping_path):
        pytest.skip("entity_alias_map.json not built")
    mapping = json.loads(open(mapping_path).read())
    mapped = [(ref, v["key"]) for ref, v in mapping.items()
              if v.get("status") == "mapped" and v.get("key")]
    assert len(mapped) >= 100
    sample = random.sample(mapped, min(150, len(mapped)))
    keys = {r[0] for r in _db().execute("SELECT key FROM nodes")}
    resolved = 0
    for ref, expected_key in sample:
        out = _call("resolve_alias", reference=ref)
        if out.get("resolved_by") == "entity_alias_map":
            resolved += 1
            assert out["graph_key"] == expected_key, f"{ref}: {out.get('graph_key')} != {expected_key}"
            assert out["graph_key"] in keys, f"phantom key {out['graph_key']} for {ref}"
    # the alias map should resolve the vast majority of its own mapped refs
    assert resolved >= len(sample) * 0.9, f"only {resolved}/{len(sample)} resolved via alias map"


def test_regex_rules_beat_alias_map():
    # these should all resolve by rule, not alias map
    for ref, by in [("s 100A", "exact_rule"), ("Div 7A", "exact_rule"),
                    ("8-1", "hyphenated_section_pattern"), ("Part IVA", "part_pattern")]:
        out = _call("resolve_alias", reference=ref)
        assert out.get("resolved_by") == by, f"{ref}: {out.get('resolved_by')}"


def test_alias_garbage_never_crashes():
    for ref in ["", "zzz qqq xxx", "!!!###", "s ", "FBTAA section 9999"]:
        out = _call("resolve_alias", reference=ref)
        assert isinstance(out, dict)


# -------------------------------------------- neighbourhood count integrity

def test_long_label_header_truncated_to_budget():
    """Regression: AML/CTF Rules headings are full legislative sentences;
    the header used to blow the 80-token cap (up to 245 tokens). The header
    must now be truncated so the block fits the documented hard cap."""
    from backend.services.graph_serialize import serialize as _serialize

    out = _serialize("section:aml-ctf-rules-2007:4.1.1", depth=1)
    assert out is not None
    assert out["tokens"] <= 80, f"long-label header still over cap: {out['tokens']}"
    assert out["text"].startswith("## ")
    assert "…" in out["text"]  # truncated with ellipsis


def test_neighbourhood_counts_match_edge_table():
    # serialize reports DISTINCT-neighbour counts (a relation derived from two
    # provenances counts once) and drops `applies` edges into public rulings
    # (they are the same section<->ruling pairs `interpreted_by` already
    # carries). Verify the block's counts match that distinct population, not
    # the raw (possibly provenance-duplicated) edge rows.
    conn = _db()
    key = "section:itaa-1997:50-50"
    out = _call("graph_neighbourhood", key=key, depth=1)
    assert out.get("key") == key
    nid = conn.execute("SELECT id FROM nodes WHERE key=?", (key,)).fetchone()[0]
    rows = conn.execute(
        "SELECT DISTINCT e.edge_type, n.id, n.node_type "
        "FROM graph_edges e "
        "JOIN nodes n ON n.id = CASE WHEN e.source_id=? THEN e.target_id ELSE e.source_id END "
        "WHERE e.source_id=? OR e.target_id=?",
        (nid, nid, nid)).fetchall()
    expected: dict[str, set[int]] = {}
    for et, nb_id, nb_type in rows:
        if et == "applies" and nb_type == "public_ruling":
            continue  # parallel to interpreted_by; not counted in APPLIES
        expected.setdefault(et, set()).add(nb_id)
    # text carries a count per edge type; parse and compare
    import re
    for et, ids in expected.items():
        label = et.upper()
        m = re.search(rf"{label}: (\d+)", out["text"])
        if m:
            assert int(m.group(1)) == len(ids), (
                f"{et}: block says {m.group(1)}, expected {len(ids)} distinct neighbours"
            )
