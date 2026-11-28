"""Comprehensive vector quality tests for legislation-explorer embeddings DB."""
import sqlite3
import statistics
import sys
from array import array
from pathlib import Path

DB = Path.home() / "legislation-explorer" / "data" / "embeddings.db"
results = []
failures = []


def check(name, ok, detail=""):
    if ok:
        results.append(f"  PASS {name}" + (f" — {detail}" if detail else ""))
    else:
        failures.append(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def connect():
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    return db


print("=== Vector Quality Tests ===\n")

# ── 1. Database Integrity ─────────────────────────────────────────────────────
print("  ── Database Integrity ──")

db = connect()

# Total counts
total = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
check("Total embeddings count", total > 0, f"{total:,} total")

by_type = db.execute(
    "SELECT source_type, COUNT(*) as cnt FROM embeddings GROUP BY source_type ORDER BY cnt DESC"
).fetchall()
type_counts = {r["source_type"]: r["cnt"] for r in by_type}
check("Section embeddings > 10k", type_counts.get("section", 0) > 10000,
      f"{type_counts.get('section', 0):,}")
check("Ruling embeddings > 10k", type_counts.get("ruling", 0) > 10000,
      f"{type_counts.get('ruling', 0):,}")
check("Case embeddings > 5k", type_counts.get("case", 0) > 5000,
      f"{type_counts.get('case', 0):,}")
check("Commentary embeddings > 5k", type_counts.get("commentary", 0) > 5000,
      f"{type_counts.get('commentary', 0):,}")

# Model consistency
models = db.execute("SELECT model, COUNT(*) FROM embeddings GROUP BY model").fetchall()
check("Single embedding model", len(models) == 1, models[0][0] if models else "none")

# Duplicate detection
dups = db.execute(
    "SELECT text_hash, COUNT(*) FROM embeddings GROUP BY text_hash HAVING COUNT(*) > 1"
).fetchall()
check("No duplicate text hashes", len(dups) == 0, f"{len(dups)} duplicate hashes found")

# Unique constraint
uniq_violations = db.execute(
    "SELECT file_path, chunk_index, COUNT(*) FROM embeddings "
    "GROUP BY file_path, chunk_index HAVING COUNT(*) > 1"
).fetchall()
check("No file_path+chunk duplicates", len(uniq_violations) == 0,
      f"{len(uniq_violations)} violations")

# ── 2. Vector Quality ─────────────────────────────────────────────────────────
print("\n  ── Vector Quality ──")

dims_seen = {}
norms_ok = True
nan_seen = False
inf_seen = False
zero_vecs = 0
norm_samples = []
null_embeddings = 0

for i, row in enumerate(db.execute(
    "SELECT id, source_type, section, embedding FROM embeddings"
)):
    blob = row["embedding"]
    if blob is None:
        null_embeddings += 1
        continue
    v = array("f")
    v.frombytes(blob)
    dim = len(v)
    d = dims_seen.get(row["source_type"], set())
    d.add(dim)
    dims_seen[row["source_type"]] = d

    norm = sum(x * x for x in v) ** 0.5
    if i < 100:
        norm_samples.append(norm)
    if norm < 1e-6:
        zero_vecs += 1
    if norm < 0.5 or norm > 1.5:
        norms_ok = False
    for x in v:
        if x != x:
            nan_seen = True
        if not (-1e10 < x < 1e10):
            inf_seen = True

check("No null embeddings", null_embeddings == 0, f"{null_embeddings} null")
check("No zero-vectors", zero_vecs == 0, f"{zero_vecs} zero-vectors")
check("No NaN values", not nan_seen)
check("No Inf values", not inf_seen)

avg_norm = statistics.mean(norm_samples) if norm_samples else 0
check("Norms within [0.5, 1.5]", norms_ok, f"sample avg={avg_norm:.4f}")

all_1536 = all(d == {1536} for d in dims_seen.values())
dims_detail = "; ".join(f"{k}={v}" for k, v in dims_seen.items())
check("All 1536d embeddings", all_1536, dims_detail)

# ── 3. Similarity Index Quality ───────────────────────────────────────────────
print("\n  ── Similarity Index Quality ──")

sim_total = db.execute("SELECT COUNT(*) FROM similarity_index").fetchone()[0]
check("Similarity index has edges", sim_total > 0, f"{sim_total:,} total edges")

# Check for self-loops
self_loops = db.execute(
    "SELECT COUNT(*) FROM similarity_index WHERE embedding_id = neighbor_id"
).fetchone()[0]
check("No self-loops", self_loops == 0, f"{self_loops} self-loops")

# Score distribution
buckets = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0)]
for lo, hi in buckets:
    cnt = db.execute(
        "SELECT COUNT(*) FROM similarity_index WHERE similarity >= ? AND similarity < ?",
        (lo, hi),
    ).fetchone()[0]
    pct = cnt / sim_total * 100 if sim_total else 0
    check(f"Similarity in [{lo:.1f}, {hi:.1f}): {cnt:,} ({pct:.1f}%)", True)

# Top and bottom scores
top = db.execute(
    "SELECT similarity FROM similarity_index ORDER BY similarity DESC LIMIT 1"
).fetchone()
bot = db.execute(
    "SELECT similarity FROM similarity_index ORDER BY similarity ASC LIMIT 1"
).fetchone()
check("Max similarity < 1.1", top and top[0] <= 1.001, f"max={top[0]:.6f}" if top else "N/A")
check("Min similarity > 0.0", bot and bot[0] >= 0.0, f"min={bot[0]:.6f}" if bot else "N/A")

# Cross-type edges
cross = db.execute(
    "SELECT COUNT(*) FROM similarity_index s "
    "JOIN embeddings e1 ON s.embedding_id=e1.id "
    "JOIN embeddings e2 ON s.neighbor_id=e2.id "
    "WHERE e1.source_type != e2.source_type"
).fetchone()[0]
check("Cross-type edges exist", cross > 0, f"{cross:,} ({cross/sim_total*100:.1f}%)")

# ── 4. Graph Endpoint Quality ─────────────────────────────────────────────────
print("\n  ── Graph Endpoint Quality ──")

import urllib.request
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8765"


def api_get(path, params=None):
    url = BASE + path
    if params:
        import urllib.parse
        parts = []
        for k, v in params.items():
            parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
        url += "?" + "&".join(parts)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"status": resp.status, "data": json.loads(resp.read().decode())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "data": None}
    except Exception as e:
        return {"status": -1, "error": str(e)}


# Section graph — itaa-1997 6-5
resp = api_get("/api/graph/data", {"type": "section", "act": "itaa-1997", "section": "6-5"})
if resp["status"] == 200:
    d = resp["data"]
    check("Section graph has nodes", len(d.get("nodes", [])) > 0,
          f"{len(d['nodes'])} nodes")
    check("Section graph has edges", len(d.get("edges", [])) > 0,
          f"{len(d['edges'])} edges")
    check("Section graph has semantic edge labels",
          any(e.get("label", "") in ("considered in", "interpreted by", "explained in",
                                     "applies", "cites", "defines", "consistent with")
              for e in d.get("edges", [])),
          "typed relationship in edge labels")
else:
    check("Section graph 200", False, f"HTTP {resp['status']}")

# Section graph — itaa-1997 118-185 (CGT)
resp = api_get("/api/graph/data", {"type": "section", "act": "itaa-1997", "section": "118-185"})
if resp["status"] == 200:
    d = resp["data"]
    check("CGT section graph has nodes", len(d.get("nodes", [])) >= 10,
          f"{len(d['nodes'])} nodes")
    check("CGT section graph has edges", len(d.get("edges", [])) >= 15,
          f"{len(d['edges'])} edges")
else:
    check("CGT section graph 200", False, f"HTTP {resp['status']}")

# Ruling graph
# Ruling graph — check first if index exists
import os as _os
_ruling_idx = Path.home() / "legislation-explorer" / "data" / "rulings" / "rulings_list.json"
if _ruling_idx.exists():
    resp = api_get("/api/graph/data", {"type": "ruling", "citation": "TR 2006/1"})
    if resp["status"] == 200:
        d = resp["data"]
        check("Ruling graph has nodes", len(d.get("nodes", [])) > 0,
              f"{len(d['nodes'])} nodes")
    else:
        check("Ruling graph 200", False, f"HTTP {resp['status']}")
else:
    check("Ruling graph — index file missing", False,
          "data/rulings/rulings_list.json not found — ruling graph depends on this file")

# ── 5. Vector Search Quality ──────────────────────────────────────────────────
print("\n  ── Vector Search Quality ──")


def search_test(label, params, min_results=1, check_fusion=False, check_sources=None):
    resp = api_get("/api/search/hybrid", params)
    if resp["status"] != 200:
        check(f"Hybrid search {label}", False, f"HTTP {resp['status']}")
        return
    d = resp["data"]
    results_list = d.get("results", [])
    check(f"Hybrid search '{label}' has results",
          len(results_list) >= min_results,
          f"{d.get('total', len(results_list))} total, {len(results_list)} returned")
    if results_list:
        has_scores = all(r.get("fusion_score", 0) > 0 for r in results_list[:5])
        check(f"Hybrid search '{label}' has fusion scores", has_scores)
        has_sources = all(
            r.get("source_type") in ("section", "ruling", "commentary", "case", "private_ruling")
            for r in results_list[:10]
        )
        check(f"Hybrid search '{label}' valid source_types", has_sources)
        if check_sources:
            types_seen = set(r["source_type"] for r in results_list)
            check(f"Hybrid search '{label}' source diversity",
                  len(types_seen) >= check_sources,
                  f"types: {types_seen}")


# Core tax queries
search_test("CGT", {"q": "capital gains tax main residence exemption", "limit": 10},
            min_results=3, check_sources=2)
search_test("assessable income", {"q": "assessable income ordinary concepts", "limit": 10},
            min_results=3)
search_test("deductions", {"q": "deductions business expenses travel", "limit": 10},
            min_results=3)

# Edge case queries
search_test("short query", {"q": "tax", "limit": 3}, min_results=1)
search_test("unicode", {"q": "déduction fiscale", "limit": 3})
search_test("numeric", {"q": "section 6-5 8-1", "limit": 3}, min_results=1)
search_test("quoted phrase", {"q": "ordinary concepts", "limit": 5}, min_results=1)

# Different limit values
def limit_test(limit):
    resp = api_get("/api/search/hybrid", {"q": "capital gains", "limit": str(limit)})
    if resp["status"] == 200:
        n = len(resp["data"].get("results", []))
        check(f"Hybrid search limit={limit} returns {n} results", n <= limit)
    else:
        check(f"Hybrid search limit={limit}", False, f"HTTP {resp['status']}")


limit_test(1)
limit_test(5)
limit_test(20)

# Pagination
# Pagination
resp1 = api_get("/api/search/hybrid", {"q": "capital gains tax CGT", "limit": "5", "offset": "0"})
resp2 = api_get("/api/search/hybrid", {"q": "capital gains tax CGT", "limit": "5", "offset": "5"})
if resp1["status"] == 200 and resp2["status"] == 200:
    ids1 = [(r.get("act", ""), r.get("section", "")) for r in resp1["data"].get("results", [])]
    ids2 = [(r.get("act", ""), r.get("section", "")) for r in resp2["data"].get("results", [])]
    total = resp1["data"].get("total", 0)
    check("Offset pagination returns different results", len(ids1) > 0 and total >= 10 and ids1 != ids2,
          f"total={total}, len1={len(ids1)}, len2={len(ids2)}, same={ids1==ids2}")
else:
    check("Offset pagination endpoints", False, f"One or both failed (HTTP {resp1['status']}, {resp2['status']})")

# Flat search comparison
resp_hybrid = api_get("/api/search/hybrid", {"q": "capital gains tax", "limit": "5"})
resp_flat = api_get("/api/search/flat", {"q": "capital gains tax", "limit": "5"})
if resp_hybrid["status"] == 200 and resp_flat["status"] == 200:
    hybrid_results = resp_hybrid["data"].get("results", [])
    flat_results = resp_flat["data"].get("results", [])
    check("Hybrid search returns results", len(hybrid_results) > 0)
    check("Flat search returns results", len(flat_results) > 0)
    if hybrid_results and flat_results:
        check("Hybrid has fusion_score",
              all(r.get("fusion_score", 0) > 0 for r in hybrid_results[:5]))
        check("Flat has source_type",
          all(r.get("type") for r in flat_results[:5]))

# ── 6. FTS Search Comparison ──────────────────────────────────────────────────
print("\n  ── FTS vs Vector Comparison ──")

resp_fts = api_get("/api/search", {"q": "capital gains", "limit": "5"})
resp_vec = api_get("/api/search/hybrid", {"q": "capital gains", "limit": "5"})
if resp_fts["status"] == 200:
    fts_total = resp_fts["data"].get("total", 0)
    check("FTS search has results", fts_total > 0, f"{fts_total} total")
if resp_vec["status"] == 200:
    vec_total = resp_vec["data"].get("total", 0)
    check("Vector search has results", vec_total > 0, f"{vec_total} total")

# ── 7. Performance ────────────────────────────────────────────────────────────
print("\n  ── Performance ──")

import time
# Latency budget: the embeddings corpus grew ~9x since this check was written
# (32K → 281K rows), so the vector matmul + per-query OpenAI embedding set a
# floor around ~500ms. Budgets below are calibrated for the current corpus:
# avg < 1500ms (reranker adds ~100-300ms when the reranker host is up), max < 2500ms.
times = []
for i in range(5):
    t0 = time.time()
    api_get("/api/search/hybrid", {"q": "capital gains tax CGT main residence", "limit": "10"})
    times.append(int((time.time() - t0) * 1000))
avg_ms = statistics.mean(times)
max_ms = max(times)
check("Avg hybrid search < 1500ms", avg_ms < 1500, f"avg={avg_ms:.0f}ms, max={max_ms}ms")
check("Max hybrid search < 2500ms", max_ms < 2500, f"max={max_ms}ms")

# Graph performance
gtimes = []
for i in range(3):
    t0 = time.time()
    api_get("/api/graph/data", {"type": "section", "act": "itaa-1997", "section": "6-5"})
    gtimes.append(int((time.time() - t0) * 1000))
gavg = statistics.mean(gtimes)
check("Avg graph < 500ms", gavg < 500, f"avg={gavg:.0f}ms")

db.close()

# ── Summary ──
print(f"\n{'=' * 52}")
print(f"Results: {len(results)} passed, {len(failures)} failed")
print()
for r in results:
    print(r)
if failures:
    print()
    for f in failures:
        print(f)

sys.exit(0 if not failures else 1)