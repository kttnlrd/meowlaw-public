"""G1 gate — graph neighbourhood enrichment (spec §6.1).

Integrity checks:
  1. counts in the neighbourhood map equal direct SQL COUNT per edge type
  2. top-3 exemplars equal global-degree ranking (ORDER BY degree DESC LIMIT 3)
  3. unknown keys map to None, never crash
  4. /api/search results carry a well-formed graph field
  5. batch of 10 typical sections enriches in < 200 ms (warm cache)
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from dotenv import load_dotenv

load_dotenv()
os.environ.pop("AZURE_CLIENT_ID", None)
os.environ.pop("LEGISLATION_BEARER_TOKEN", None)

from backend.main import app  # noqa: E402
from backend.tests.helpers import session_token  # noqa: E402
from backend.services.graph_neighborhood import (  # noqa: E402
    GRAPH_DB,
    neighborhoods,
    _global_degrees,
)

client = TestClient(app)
client.cookies.set("session", session_token())

# Typical, non-hub sections across acts (hub s 8-1 excluded from latency test)
TYPICAL_KEYS = [
    "section:itaa-1997:6-5",
    "section:itaa-1997:8-1",
    "section:itaa-1997:118-110",
    "section:itaa-1997:40-25",
    "section:itaa-1936:109ba",
    "section:gst-1999:9-5",
    "section:gst-1999:11-5",
    "section:fbt-1986:7",
    "section:taa-1953:14ZZO",
    "section:corporations-act-2001:180",
]


def _conn():
    return sqlite3.connect(f"file:{GRAPH_DB}?mode=ro", uri=True)


def test_unknown_key_returns_none():
    assert neighborhoods(["section:nope-9999:1-1"])["section:nope-9999:1-1"] is None
    assert neighborhoods([]) == {}


def test_counts_match_direct_sql():
    neigh = neighborhoods(TYPICAL_KEYS)
    conn = _conn()
    try:
        ids = {r[1].lower(): r[0] for r in conn.execute(
            "SELECT id, key FROM nodes WHERE lower(key) IN (%s)"
            % ",".join("?" * len(TYPICAL_KEYS)), [k.lower() for k in TYPICAL_KEYS])}
        for key in TYPICAL_KEYS:
            g = neigh[key]
            assert g is not None, key
            nid = ids[key.lower()]
            rows = conn.execute(
                "SELECT edge_type, COUNT(*) FROM graph_edges "
                "WHERE source_id = ? OR target_id = ? GROUP BY edge_type",
                (nid, nid)).fetchall()
            expected = {et: cnt for et, cnt in rows}
            assert set(g["edges"].keys()) == set(expected.keys()), key
            for et, cnt in expected.items():
                assert g["edges"][et]["count"] == cnt, f"{key} {et}"
    finally:
        conn.close()


def test_top3_matches_global_degree_ranking():
    neigh = neighborhoods(TYPICAL_KEYS)
    conn = _conn()
    try:
        degree = _global_degrees(conn)
        ids = {r[1].lower(): r[0] for r in conn.execute(
            "SELECT id, key FROM nodes WHERE lower(key) IN (%s)"
            % ",".join("?" * len(TYPICAL_KEYS)), [k.lower() for k in TYPICAL_KEYS])}
        for key in TYPICAL_KEYS:
            g = neigh[key]
            nid = ids[key.lower()]
            for et, info in g["edges"].items():
                # neighbour ids of this type, ranked by global degree
                rows = conn.execute(
                    "SELECT CASE WHEN source_id = ? THEN target_id ELSE source_id END AS nid "
                    "FROM graph_edges WHERE (source_id = ? OR target_id = ?) AND edge_type = ?",
                    (nid, nid, nid, et)).fetchall()
                neighbour_degree = {r[0]: degree.get(r[0], 0) for r in rows}
                ranked = sorted(neighbour_degree.items(), key=lambda kv: -kv[1])[:3]
                ranked_ids = [n for n, _ in ranked]
                expected_degrees = sorted((degree.get(i, 0) for i in ranked_ids), reverse=True)
                # map returned labels back to ids
                if not ranked_ids:
                    assert info["top"] == [], f"{key} {et}"
                    continue
                ph3 = ",".join("?" * len(info["top"]))
                lab = {r[1]: r[0] for r in conn.execute(
                    f"SELECT id, label FROM nodes WHERE label IN ({ph3})", info["top"])}
                got_ids = [lab.get(l) for l in info["top"]]
                assert all(gid is not None for gid in got_ids), f"{key} {et}: labels not found {info['top']}"
                got_degrees = sorted((degree.get(gid, 0) for gid in got_ids), reverse=True)
                # tie-tolerant: same degree profile, not necessarily same ordering
                assert got_degrees == expected_degrees, (
                    f"{key} {et}: degrees {got_degrees} != {expected_degrees}")
    finally:
        conn.close()


def test_search_results_carry_graph_field():
    r = client.get("/api/search", params={"q": "deductions", "limit": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["results"]
    found = 0
    for item in data["results"]:
        assert "act" in item and "section" in item
        if "graph" in item:
            g = item["graph"]
            assert g["node"] == f"section:{item['act']}:{item['section']}"
            assert g["label"]
            assert isinstance(g["edges"], dict)
            for et, info in g["edges"].items():
                assert isinstance(info["count"], int) and info["count"] > 0
                assert isinstance(info["top"], list) and len(info["top"]) <= 3
            found += 1
    assert found > 0, "no results carried a graph field"


def test_batch_latency_warm():
    # warm the materialised neighbourhood index
    neighborhoods(TYPICAL_KEYS)
    t0 = time.perf_counter()
    neighborhoods(TYPICAL_KEYS)
    dt = (time.perf_counter() - t0) * 1000
    # 200 ms: this gate exists to catch on-the-fly hub-node computation
    # (s 8-1 has ~18k edges) and N+1 regressions, not to microbenchmark a fixed
    # latency. Each call opens a fresh read-only sqlite connection and re-checks
    # the index fingerprint, so ~50-70 ms is the warm baseline on a shared box.
    assert dt < 200, f"10-key batch took {dt:.1f} ms (> 200 ms)"
