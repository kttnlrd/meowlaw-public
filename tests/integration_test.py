"""Layer 2: Integration tests against the live dev server on port 8765."""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import sys

BASE = "http://127.0.0.1:8765"
results = []
failures = []


def api_get(path, params=None):
    url = BASE + path
    if params:
        parts = []
        for k, v in params.items():
            parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
        url += "?" + "&".join(parts)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
        return {"status": status, "data": json.loads(body)}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return {"status": e.code, "data": None, "raw": body[:300]}
    except (json.JSONDecodeError, Exception) as e:
        return {"status": -1, "data": None, "error": str(e)}


def test(name, path, params=None, check_fn=None):
    resp = api_get(path, params)
    if resp["status"] == -1:
        failures.append(f"  FAIL {name}: connection error — {resp.get('error', '')}")
        return
    if check_fn:
        msg = check_fn(resp)
        if msg:
            failures.append(f"  FAIL {name}: {msg} (HTTP {resp['status']})")
            return
    results.append(f"  PASS {name} (HTTP {resp['status']})")


# ── Check functions ───────────────────────────────────────────────────────────

def health_ok(resp):
    return None if resp["data"] == {"status": "ok"} else f"unexpected: {resp['data']}"

def acts_list(resp):
    d = resp["data"]
    if not isinstance(d, list) or len(d) == 0:
        return "empty or not a list"
    ids = [a.get("id") for a in d]
    if "itaa-1997" not in ids:
        return f"missing itaa-1997 in {ids}"
    return None

def section_ok(resp):
    d = resp["data"]
    if "frontmatter" not in d or "body" not in d:
        return f"missing keys: {list(d.keys())[:5]}"
    return None

def has_results_total(resp):
    d = resp["data"]
    if "results" not in d or "total" not in d:
        return f"missing results/total: {list(d.keys())}"
    return None

def has_results(resp):
    d = resp["data"]
    if "results" not in d:
        return f"missing results: {list(d.keys())}"
    return None

def tree_ok(resp):
    d = resp["data"]
    if "act" not in d or "parts" not in d:
        return f"missing act/parts: {list(d.keys())}"
    return None

def has_terms(resp):
    d = resp["data"]
    if "terms" not in d or not d["terms"]:
        return "missing or empty terms"
    return None

def dict_with_parts(resp):
    d = resp["data"]
    if "parts" not in d or not d["parts"]:
        return "missing or empty parts"
    return None

def def_lookup_ok(resp):
    d = resp["data"]
    if isinstance(d, dict) and ("term" in d or "text" in d or "detail" in d):
        return None
    return f"unexpected shape: {type(d).__name__}"

def has_commentary(resp):
    d = resp["data"]
    if "commentary" not in d:
        return f"missing commentary: {list(d.keys())}"
    return None

def has_nodes_edges(resp):
    d = resp["data"]
    if "nodes" not in d or "edges" not in d:
        return f"missing nodes/edges: {list(d.keys())}"
    return None

def has_links(resp):
    d = resp["data"]
    if "links" not in d:
        return f"missing links: {list(d.keys())}"
    return None

def has_sections(resp):
    d = resp["data"]
    if "sections" not in d:
        return f"missing sections: {list(d.keys())}"
    return None

def status_404(resp):
    return None if resp["status"] == 404 else f"expected 404, got {resp['status']}"

def status_200(resp):
    return None if resp["status"] == 200 else f"expected 200, got {resp['status']}"

def nonempty_dict(resp):
    d = resp["data"]
    if not isinstance(d, dict) or len(d) == 0:
        return "empty or not a dict"
    return None


# ── Run tests ─────────────────────────────────────────────────────────────────

print("=== Layer 2: Integration Tests (live dev server :8765) ===\n")

print("  ── Health & Metadata ──")
test("Health endpoint", "/health", check_fn=health_ok)
test("Acts list", "/api/acts", check_fn=acts_list)
test("Data version", "/api/data-version", check_fn=status_200)
test("Info endpoint", "/api/info", check_fn=status_200)

print("\n  ── Sections & Tree ──")
test("Section 6-5 content", "/api/section/itaa-1997/6-5", check_fn=section_ok)
test("Section 404", "/api/section/itaa-1997/nonexistent", check_fn=status_404)
test("Tree itaa-1997", "/api/tree/itaa-1997", check_fn=tree_ok)

print("\n  ── Search ──")
test("Search 'CGT'", "/api/search", {"q": "CGT", "limit": 3}, check_fn=has_results_total)
test("Empty query", "/api/search", {"q": "", "limit": 3}, check_fn=has_results_total)
test("Hybrid search", "/api/search/hybrid", {"q": "capital gains", "limit": 3}, check_fn=has_results)
test("Flat search", "/api/search/flat", {"q": "CGT", "limit": 3}, check_fn=has_results)

print("\n  ── Definitions ──")
test("Definitions list", "/api/definitions/itaa-1997", check_fn=has_terms)
test("Definition lookup", "/api/definition/itaa-1997/assessable%20income", check_fn=def_lookup_ok)
test("Definition text", "/api/definition-text/itaa-1997/assessable%20income", check_fn=def_lookup_ok)
test("Definition 404", "/api/definition/itaa-1997/nonexistentterm", check_fn=status_404)

print("\n  ── Rulings ──")
test("Rulings list", "/api/rulings-list", check_fn=dict_with_parts)
test("Ruling by citation", "/api/ruling/TR%202025%2F1", check_fn=status_200)
test("Ruling 404", "/api/ruling/NONEXISTENT", check_fn=status_404)

print("\n  ── Tax Cases ──")
test("Tax cases search", "/api/tax-cases/search", {"q": "FCT"}, check_fn=has_results)
test("Tax cases HCA", "/api/tax-cases/hca", check_fn=nonempty_dict)
test("Tax cases sidebar", "/api/tax-cases/sidebar", check_fn=status_200)
test("Section tax cases 6-5", "/api/section-tax-cases/itaa-1997/6-5", check_fn=status_200)

print("\n  ── Commentary & References ──")
test("Commentary s6-5", "/api/commentary/itaa-1997/6-5", check_fn=has_commentary)
test("Smart links s6-5", "/api/smart-links/section/itaa-1997%2F6-5", check_fn=has_links)
test("Section refs s6-5", "/api/section-refs/itaa-1997/6-5", check_fn=has_sections)

print("\n  ── Graph & MCP ──")
test("Graph section data", "/api/graph/data", {"type": "section", "act": "itaa-1997", "section": "6-5"}, check_fn=has_nodes_edges)
test("MCP Hall of Fame", "/api/mcp-hall-of-fame", check_fn=status_200)

print("\n  ── Cases ──")
test("Cases list", "/api/cases", check_fn=nonempty_dict)
test("Cases for s6-5", "/api/cases/itaa-1997/6-5", check_fn=status_200)

# ── Performance ──
print("\n  ── Performance ──")
for label, path, params in [
    ("Search", "/api/search", {"q": "income", "limit": 5}),
    ("Graph", "/api/graph/data", {"type": "section", "act": "itaa-1997", "section": "6-5"}),
    ("Rulings list", "/api/rulings-list", None),
    ("Tax cases HCA", "/api/tax-cases/hca", None),
]:
    t0 = time.time()
    api_get(path, params)
    elapsed = int((time.time() - t0) * 1000)
    tag = " (WARN >2s)" if elapsed > 2000 else ""
    results.append(f"  PASS {label}: {elapsed}ms{tag}")

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