"""G3 gate — path queries (spec §6.3).

Integrity checks:
  1. known pairs resolve to sane paths with correct endpoints
  2. every hop in a returned path is a real graph edge of the recorded type
  3. unreachable pair (different connected components) → path: null, fast
  4. from == to → empty path, 0 hops
  5. hub-to-hub resolves in < 2s
  6. unknown keys → 404; max_hops bounds → 422
"""
from __future__ import annotations

import os
import sqlite3
import time
from functools import lru_cache

from fastapi.testclient import TestClient

from dotenv import load_dotenv

load_dotenv()
os.environ.pop("AZURE_CLIENT_ID", None)
os.environ.pop("LEGISLATION_BEARER_TOKEN", None)

from backend.main import app  # noqa: E402
from backend.services.graph_neighborhood import GRAPH_DB  # noqa: E402
from backend.tests.helpers import session_token  # noqa: E402

client = TestClient(app)
client.cookies.set("session", session_token())

FROM_SECTION = "section:itaa-1997:118-110"
TO_RULING = "public_ruling:TR 2025/1"
EV = "private_ruling:EV/1011261243735"
S117 = "section:itaa-1936:117"
HUB_A = "section:itaa-1997:8-1"
HUB_B = "section:itaa-1997:995-1"


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True, timeout=10)


@lru_cache(maxsize=1)
def _unreachable_pair() -> tuple[str, str]:
    """Two keys in different connected components (undirected)."""
    conn = _conn()
    try:
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for nid, in conn.execute("SELECT id FROM nodes"):
            parent[nid] = nid
        for s, t in conn.execute("SELECT source_id, target_id FROM graph_edges"):
            union(s, t)

        comp: dict[int, int] = {}
        for nid in parent:
            r = find(nid)
            comp[r] = comp.get(r, 0) + 1
        giant = max(comp, key=comp.get)

        small_root = next(r for r in comp if r != giant)
        big_key = conn.execute(
            "SELECT key FROM nodes WHERE id=? LIMIT 1", (next(n for n in parent if find(n) == giant),)
        ).fetchone()[0]
        small_key = conn.execute(
            "SELECT key FROM nodes WHERE id=? LIMIT 1", (next(n for n in parent if find(n) == small_root),)
        ).fetchone()[0]
        return big_key, small_key
    finally:
        conn.close()


def _assert_valid_path(body: dict) -> None:
    """Every hop must be a real edge of the recorded type (either direction)."""
    assert body["hops"] == len(body["path"]) - 1
    keys = [p["key"] for p in body["path"]]
    conn = _conn()
    try:
        ids = {k: conn.execute("SELECT id FROM nodes WHERE key=?", (k,)).fetchone()[0] for k in keys}
        for e in body["edges"]:
            a, b = ids[keys[e["from"]]], ids[keys[e["to"]]]
            row = conn.execute(
                "SELECT 1 FROM graph_edges WHERE edge_type=? AND "
                "((source_id=? AND target_id=?) OR (source_id=? AND target_id=?)) LIMIT 1",
                (e["type"], a, b, b, a),
            ).fetchone()
            assert row is not None, f"no real edge {keys[e['from']]} -{e['type']}- {keys[e['to']]}"
    finally:
        conn.close()


def test_known_pair_section_to_ruling():
    r = client.get("/api/graph/path", params={"from": FROM_SECTION, "to": TO_RULING})
    assert r.status_code == 200
    body = r.json()
    assert body["path"][0]["key"] == FROM_SECTION
    assert body["path"][-1]["key"] == TO_RULING
    assert body["hops"] >= 1
    _assert_valid_path(body)


def test_private_ruling_to_section_one_hop():
    body = client.get("/api/graph/path", params={"from": EV, "to": S117}).json()
    assert body["hops"] == 1
    assert body["path"][0]["key"] == EV
    assert body["path"][-1]["key"] == S117
    assert body["edges"][0]["type"] == "applies"
    _assert_valid_path(body)


def test_from_equals_to():
    body = client.get("/api/graph/path", params={"from": HUB_A, "to": HUB_A}).json()
    assert body["path"] == []
    assert body["hops"] == 0


def test_unreachable_pair_returns_null_fast():
    big, small = _unreachable_pair()
    t0 = time.time()
    body = client.get("/api/graph/path", params={"from": big, "to": small}).json()
    dt = time.time() - t0
    assert body["path"] is None
    assert body["reason"]
    assert dt < 2.0, f"unreachable pair took {dt:.2f}s"


def test_hub_to_hub_under_2s():
    t0 = time.time()
    body = client.get("/api/graph/path", params={"from": HUB_A, "to": HUB_B}).json()
    dt = time.time() - t0
    assert body["path"] is not None
    assert body["hops"] >= 1
    assert dt < 2.0, f"hub-to-hub took {dt:.2f}s"
    _assert_valid_path(body)


def test_unknown_key_404():
    r = client.get("/api/graph/path", params={"from": "bogus:key", "to": HUB_A})
    assert r.status_code == 404
    r2 = client.get("/api/graph/path", params={"from": HUB_A, "to": "bogus:key"})
    assert r2.status_code == 404


def test_max_hops_validation():
    assert client.get("/api/graph/path", params={"from": HUB_A, "to": HUB_B, "max_hops": 0}).status_code == 422
    assert client.get("/api/graph/path", params={"from": HUB_A, "to": HUB_B, "max_hops": 21}).status_code == 422


def test_max_hops_respected():
    # 118-110 → TR 2025/1 is 3 hops; max_hops=1 must return null
    body = client.get("/api/graph/path", params={"from": FROM_SECTION, "to": TO_RULING, "max_hops": 1}).json()
    assert body["path"] is None
