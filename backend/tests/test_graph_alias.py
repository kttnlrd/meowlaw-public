"""G6 gate — entity-alias map wired into the live search/graph runtime.

Covers:
  1. real mapped refs (sample ≥100) resolve to keys that exist in graph.db
  2. the FBTAA and crosswalk case resolve to their canonical keys
  3. neutral case citations absent from graph.db translate via the crosswalk
  4. unknown/garbage refs -> None, never crash
  5. /api/search for a known alias ref surfaces the graph block
  6. /api/graph/data?ref=... resolves the canonical node
  7. missing map file -> service still works (no exception)
"""
from __future__ import annotations

import json
import os
import random

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()
os.environ.pop("AZURE_CLIENT_ID", None)
os.environ.pop("LEGISLATION_BEARER_TOKEN", None)

from backend.main import app  # noqa: E402
from backend import config  # noqa: E402
from backend.auth import require_user  # noqa: E402
import backend.services.graph_alias as ga  # noqa: E402

config.BEARER_TOKEN = None

client = TestClient(app)


@pytest.fixture(autouse=True)
def _mock_require_user():
    # Data routers bind Depends(require_user); in test mode (no AZURE_CLIENT_ID)
    # there is no SSO middleware to set request.state.user, so override it with
    # a mock user. SSO-only routes (admin/user/comment-write) call _require_user
    # inside the handler and are covered separately in test_api_auth.py.
    app.dependency_overrides[require_user] = (
        lambda: {"id": None, "email": "test@lawkitty.local", "tier": "trial"}
    )
    yield
    app.dependency_overrides.pop(require_user, None)


def _mapped_entries() -> list[tuple[str, str]]:
    if not ga.ALIAS_MAP_PATH.exists():
        raise AssertionError("entity_alias_map.json not built — run pipeline.entity_backstop")
    mapping = json.loads(ga.ALIAS_MAP_PATH.read_text())
    return [(ref, v["key"]) for ref, v in mapping.items()
            if v.get("status") == "mapped" and v.get("key")]


def test_lookup_case_and_whitespace_insensitive():
    assert ga.lookup("FBTAA section 49") == "section:fbt-1986:49"
    assert ga.lookup("  fbtAa  section   49  ") == "section:fbt-1986:49"
    assert ga.lookup("Glenn v Federal Commissioner of Land Tax") == "case:(1915) 20 CLR 490"


def test_mapped_refs_resolve_to_graph_keys():
    mapped = _mapped_entries()
    assert len(mapped) >= 100
    keys_in_graph = ga._graph_key_set()
    assert keys_in_graph, "graph key set empty — graph.db unavailable?"

    # spec examples resolve to their canonical keys
    assert ga.lookup("FBTAA section 49") == "section:fbt-1986:49"
    assert ga.lookup("Glenn v Federal Commissioner of Land Tax") == "case:(1915) 20 CLR 490"

    sample = random.Random(0).sample(mapped, 150)
    for ref, expected_key in sample:
        got = ga.lookup(ref)
        assert got is not None, f"mapped ref did not resolve: {ref!r} -> {expected_key!r}"
        assert got in keys_in_graph, f"resolved key not in graph.db: {got!r} (from {ref!r})"


def test_crosswalk_neutral_case_translation():
    # neutral citation absent from graph.db resolves via the crosswalk
    assert ga._verify_key("case:[1915] HCA 57") == "case:(1915) 20 CLR 490"
    # a neutral citation present directly stays as-is
    assert ga._verify_key("case:[1904] HCA 29") == "case:[1904] HCA 29"


def test_unknown_and_garbage_refs_return_none():
    for ref in ("zzz totally not a ref zzz", "", None, "FBTAA section 99999"):
        assert ga.lookup(ref) is None, ref


def test_search_alias_ref_surfaces_graph_block():
    r = client.get("/api/search", params={"q": "FBTAA section 49"})
    assert r.status_code == 200
    data = r.json()
    found = any(item.get("graph", {}).get("node") == "section:fbt-1986:49"
                for item in data["results"])
    for a in data.get("aliases", []):
        if a["key"] == "section:fbt-1986:49":
            assert a["graph"]["node"] == "section:fbt-1986:49"
            found = True
    assert found, "FBTAA section 49 alias did not surface a graph block"


def test_search_case_alias_surfaces_graph_block():
    r = client.get("/api/search", params={"q": "Glenn v Federal Commissioner of Land Tax"})
    assert r.status_code == 200
    data = r.json()
    found = False
    for a in data.get("aliases", []):
        if a["key"] == "case:(1915) 20 CLR 490":
            assert a["graph"]["node"] == "case:(1915) 20 CLR 490"
            found = True
    assert found, "Glenn alias did not surface a case graph block"


def test_graph_data_ref_resolves_section():
    r = client.get("/api/graph/data", params={"ref": "FBTAA section 49"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["type"] == "section"
    assert any(n["id"] == "section:fbt-1986:49" for n in body["nodes"])


def test_graph_data_ref_resolves_case():
    r = client.get("/api/graph/data", params={"ref": "Glenn v Federal Commissioner of Land Tax"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["type"] == "case"
    assert any(n["id"] == "case:(1915) 20 CLR 490" for n in body["nodes"])


def test_graph_data_ref_unknown_404():
    r = client.get("/api/graph/data", params={"ref": "zzz no such ref at all"})
    assert r.status_code == 404


def test_existing_type_params_still_work():
    r = client.get("/api/graph/data", params={"type": "section", "act": "itaa-1997", "section": "8-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["type"] == "section"
    assert any(n["id"] == "section:itaa-1997:8-1" for n in body["nodes"])


def test_missing_map_file_no_exception(monkeypatch):
    monkeypatch.setattr(ga, "ALIAS_MAP_PATH", ga.DATA_DIR / "nope_missing_map.json")
    monkeypatch.setattr(ga, "_alias_map", None)
    monkeypatch.setattr(ga, "_normalized_index", None)
    assert ga.lookup("FBTAA section 49") is None
    assert ga.lookup("anything else") is None
