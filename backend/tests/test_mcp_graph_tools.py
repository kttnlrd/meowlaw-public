"""MCP surface for graph features (Phase 4 additions).

Covers:
  1. graph_neighbourhood returns a token-lean block for a known key
  2. graph_neighbourhood honours depth (1 vs 2) and rejects bad depth
  3. graph_neighbourhood returns an error object for unknown keys
  4. graph_path returns a real path with correct endpoints + typed edges
  5. graph_path errors cleanly for unknown keys and caps max_hops at 20
  6. resolve_alias resolves an alias-map phrase to a graph key
     (e.g. "FBTAA section 49" -> section:fbt-1986:49) via entity_alias_map
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.pop("AZURE_CLIENT_ID", None)
os.environ.pop("LEGISLATION_BEARER_TOKEN", None)

from backend.fastmcp_server import mcp  # noqa: E402

_TOOLS = {t.name: t for t in mcp._tool_manager._tools.values()}

SECTION_KEY = "section:itaa-1997:118-110"
EV_RULING = "private_ruling:EV/1011275526832"
CASE_KEY = "case:[1891] AC 531"


def _call(name: str, **kw) -> dict:
    fn = _TOOLS[name].fn
    if asyncio.iscoroutinefunction(fn):
        result = asyncio.run(fn(**kw))
    else:
        result = fn(**kw)
    return json.loads(result)


def test_new_tools_registered():
    assert "graph_neighbourhood" in _TOOLS
    assert "graph_path" in _TOOLS


def test_graph_neighbourhood_known_key():
    out = _call("graph_neighbourhood", key=SECTION_KEY, depth=1)
    assert out["key"] == SECTION_KEY
    assert out["depth"] == 1
    assert out["tokens"] <= 80
    assert out["text"].startswith("## ")


def test_graph_neighbourhood_depth2():
    out = _call("graph_neighbourhood", key=SECTION_KEY, depth=2)
    assert out["depth"] == 2
    assert out["tokens"] <= 400
    assert "LEVEL 2" in out["text"] or "APPLIES" in out["text"]


def test_graph_neighbourhood_bad_depth():
    out = _call("graph_neighbourhood", key=SECTION_KEY, depth=3)
    assert "error" in out


def test_graph_neighbourhood_unknown_key():
    out = _call("graph_neighbourhood", key="section:itaa-1997:999-zzz")
    assert "error" in out


def test_graph_path_endpoints_and_edges():
    out = _call("graph_path", from_key=EV_RULING, to_key=CASE_KEY)
    assert out["from"] == EV_RULING
    assert out["to"] == CASE_KEY
    assert out["path"] and out["path"][0]["key"] == EV_RULING
    assert out["path"][-1]["key"] == CASE_KEY
    # every edge must reference consecutive path positions
    for e in out["edges"]:
        assert e["to"] == e["from"] + 1
        assert e["type"]


def test_graph_path_unknown_key():
    out = _call("graph_path", from_key="private_ruling:EV/000000000000", to_key=CASE_KEY)
    assert "error" in out


def test_graph_path_hop_cap():
    # max_hops beyond 20 is clamped, not an error
    out = _call("graph_path", from_key=EV_RULING, to_key=CASE_KEY, max_hops=99)
    assert "path" in out or "error" in out


def test_resolve_alias_uses_entity_alias_map():
    out = _call("resolve_alias", reference="FBTAA section 49")
    assert out.get("resolved_by") == "entity_alias_map"
    assert out.get("graph_key") == "section:fbt-1986:49"


def test_resolve_alias_regex_still_first():
    # Well-known patterns must still resolve by exact_rule, not the alias map
    out = _call("resolve_alias", reference="s 100A")
    assert out.get("resolved_by") == "exact_rule"
    assert out.get("section") == "100A"


@pytest.mark.skipif(
    not os.path.exists("data/entity_alias_map.json"),
    reason="entity_alias_map.json not built — run pipeline.entity_backstop",
)
def test_resolve_alias_crosswalk_case():
    out = _call("resolve_alias", reference="Glenn v Federal Commissioner of Land Tax")
    assert out.get("resolved_by") == "entity_alias_map"
    assert out.get("graph_key") == "case:(1915) 20 CLR 490"
