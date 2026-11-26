#!/usr/bin/env python3
"""Regression suite for spike 001 — hybrid search service.

Checks three things that have actually broken or been questioned:
  1. identifier queries resolve to the section they name (the 26AH defect)
  2. semantic queries surface the right act/section
  3. the slicers filter at retrieval time, don't leak, and stay usable
plus latency envelopes and determinism.

    python3 test_search.py [base_url]
"""
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"

# --- latency envelopes (ms) -------------------------------------------------
MAX_BM25_MS = 300.0
MAX_ANN_MS = 700.0
MAX_WALL_MS = 6000.0

# identifier queries: the section they name must come back first, and flagged exact
SECTION_REFS = ["26AH", "160AAB", "152-210", "328-10", "58GA"]

# semantic queries: (query, expected act, expected section or None)
SEMANTIC = [
    ("living away from home allowance", "fbt-1986", "30"),
    ("superannuation guarantee charge", "sis-1993", "46"),
    ("capital gains small business concession", "itaa-1997", None),
    ("division 7A loan to shareholder", "itaa-1936", None),
]

# act filters that must return hits and must not leak other acts
ACT_SLICES = ["itaa-1997", "fbt-1986", "gst-1999"]
TYPE_SLICES = ["section", "ruling", "case", "regulatory_guide"]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not ok:
        line += f"\n         {detail}"
    print(line, flush=True)


def api(q, **kw):
    params = {"q": q, "k": kw.pop("k", 20), **kw}
    url = f"{BASE}/api/search?" + urllib.parse.urlencode(params)
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read()[:400].decode("utf8", "replace")}
    except Exception as e:
        return {"_error": repr(e)}
    d["_wall_ms"] = (time.time() - t) * 1000
    return d


def top(d):
    return d.get("hits", [{}])[0] if d.get("hits") else {}


def no_error(d):
    return "_error" not in d and "_http_error" not in d


# --- 1. identifier queries --------------------------------------------------
print("\n1. identifier queries resolve to the section they name")
for ref in SECTION_REFS:
    d = api(ref)
    if not no_error(d) or not d.get("hits"):
        check(f"{ref!r} -> section", False, f"no hits: {d.get('_error') or d.get('error') or d}")
        continue
    h = top(d)
    check(
        f"{ref!r} -> section",
        h.get("section", "").lower() == ref.lower() and h.get("exact") is True,
        f"got {h.get('act')}/{h.get('section')} exact={h.get('exact')}",
    )

for ref in ["s " + SECTION_REFS[0], "section " + SECTION_REFS[1]]:
    d = api(ref)
    h = top(d)
    want = ref.split()[-1]
    check(
        f"{ref!r} (prefixed) -> section",
        no_error(d) and h.get("section", "").lower() == want.lower(),
        f"got {h.get('act')}/{h.get('section')}",
    )

# --- 2. semantic queries ----------------------------------------------------
print("\n2. semantic queries surface the right act/section")
for q, want_act, want_sec in SEMANTIC:
    d = api(q, **({"type": "section"} if want_act.startswith(("itaa", "fbt", "sis")) else {}))
    if not no_error(d) or not d.get("hits"):
        check(f"{q!r} -> {want_act}", False, f"no hits: {d.get('_error') or d.get('error')}")
        continue
    top5 = d["hits"][:5]
    if want_sec:
        hit = any(h["act"] == want_act and h["section"] == want_sec for h in top5)
        detail = f"top5: " + ", ".join(f"{h['act']}/{h['section']}" for h in top5)
    else:
        hit = top5[0]["act"] == want_act
        detail = f"top: {top5[0]['act']}/{top5[0]['section']}"
    check(f"{q!r} -> {want_act}" + (f" s{want_sec}" if want_sec else ""), hit, detail)

# --- 3. slicers -------------------------------------------------------------
print("\n3. slicers filter at retrieval time")
for t in TYPE_SLICES:
    d = api("capital gains small business concession", type=t)
    hits = d.get("hits", [])
    check(
        f"type={t} -> hits, no leak",
        no_error(d) and len(hits) > 0 and all(h["kind"] == t for h in hits),
        f"n={len(hits)} kinds={sorted({h['kind'] for h in hits})}",
    )

for a in ACT_SLICES:
    d = api("trust streaming capital gains", act=a)
    hits = d.get("hits", [])
    check(
        f"act={a} -> hits, no leak",
        no_error(d) and len(hits) > 0 and all(h["act"] == a for h in hits),
        f"n={len(hits)} acts={sorted({h['act'] for h in hits})}",
    )

d = api("capital gains small business concession", type="section", act="itaa-1997")
hits = d.get("hits", [])
check(
    "type+act combined",
    no_error(d) and len(hits) > 0 and all(h["kind"] == "section" and h["act"] == "itaa-1997" for h in hits),
    f"n={len(hits)} got={sorted({(h['kind'], h['act']) for h in hits})}",
)

# a section-ref query sliced to another source must not leak the section itself
d = api("26AH", type="case")
hits = d.get("hits", [])
check(
    "slicer excludes the exact-section hit it would otherwise boost",
    no_error(d) and all(h["kind"] == "case" for h in hits) and not any(h.get("exact") for h in hits),
    f"n={len(hits)} kinds={sorted({h['kind'] for h in hits})} exact={[h['section'] for h in hits if h.get('exact')]}",
)
check(
    "cases citing 26AH are still findable inside that slice",
    no_error(d) and len(hits) > 0,
    f"n={len(hits)}",
)

# facets must always offer the full source list, even when the pool holds none
d = api("capital gains small business concession")
ftypes = {f["value"]: f["count"] for f in d.get("facets", {}).get("types", [])}
check(
    "facets always list every source type",
    {"section", "ruling", "case", "regulatory_guide", "afsa_guide"} <= set(ftypes),
    f"got {sorted(ftypes)}",
)
check(
    "facets include the major acts for slicing",
    {"itaa-1997", "itaa-1936"} <= {f["value"] for f in d.get("facets", {}).get("acts", [])},
    f"got {sorted(f['value'] for f in d.get('facets', {}).get('acts', []))}",
)

# --- 4. robustness + latency ------------------------------------------------
print("\n4. robustness, determinism, latency")
d1 = api("capital gains small business")
d2 = api("capital gains small business")
check(
    "deterministic ordering",
    no_error(d1) and no_error(d2) and top(d1).get("row") == top(d2).get("row"),
    f"{top(d1).get('row')} vs {top(d2).get('row')}",
)

for q in ['trust (streaming) "capital"', "s 160AAB?", "GST & going concern", "a" * 300, "   "]:
    d = api(q)
    check(f"{q[:28]!r} neither 500s nor errors", no_error(d), f"{d.get('_error') or d.get('_http_error')}")

d = api("")
check("empty query -> empty result, no error", no_error(d) and d.get("hits") == [])

LATENCY_QUERIES = [
    "capital gains small business concession",
    "division 7A loan to shareholder",
    "living away from home allowance",
    "26AH",
]
print()
warm = []
for q in LATENCY_QUERIES:
    d = api(q)
    api(q)  # second pass is the warm number (page cache, connection reuse)
    d = api(q)
    warm.append((q, d["embed_ms"], d["ann_ms"], d["bm25_ms"], d["_wall_ms"]))
    check(
        f"latency {q[:34]!r}",
        d["bm25_ms"] < MAX_BM25_MS and d["ann_ms"] < MAX_ANN_MS and d["_wall_ms"] < MAX_WALL_MS,
        f"bm25={d['bm25_ms']:.1f} ann={d['ann_ms']:.1f} wall={d['_wall_ms']:.1f}",
    )

d = api("capital gains small business concession", type="section")
warm.append(("[type=section] capital gains…", d["embed_ms"], d["ann_ms"], d["bm25_ms"], d["_wall_ms"]))
check(
    "latency sliced type=section",
    d["ann_ms"] < MAX_ANN_MS,
    f"ann={d['ann_ms']:.1f} (exact scan of the slice)",
)

# --- 5. instant lexical paint + embed cache ---------------------------------
print("\n5. instant lexical paint + embed cache")
d = api("capital gains small business concession", mode="lex", k=10)
check(
    "mode=lex returns 10 hits with no embed at all",
    no_error(d) and d.get("lexical_only") is True and len(d.get("hits", [])) >= 5 and d["embed_ms"] == 0.0,
    f"lexical_only={d.get('lexical_only')} n={len(d.get('hits', []))} embed_ms={d.get('embed_ms')}",
)
check(
    "mode=lex is fast enough to run on every keystroke",
    no_error(d) and d["total_ms"] < 150,
    f"server {d.get('total_ms', -1):.1f} ms",
)

d = api("26AH", mode="lex")
check(
    "instant path still resolves identifiers exactly",
    no_error(d) and top(d).get("section", "").lower() == "26ah" and top(d).get("exact") is True,
    f"got {top(d).get('act')}/{top(d).get('section')}",
)

CACHE_Q = "capital gains small business concession"
api(CACHE_Q)
_ = api(CACHE_Q)
cached = api(CACHE_Q)
check(
    "a repeated hybrid query is served from the embed cache",
    no_error(cached) and cached["embed_ms"] < 5.0,
    f"embed_ms={cached.get('embed_ms')} (should be ~0 on a cache hit)",
)

# --- report -----------------------------------------------------------------
print("\nwarm timings (embed is the network call on the critical path)")
print(f"  {'query':<36} {'embed':>8} {'ANN':>8} {'BM25':>8} {'wall':>8}")
for q, e, a, b, w in warm:
    print(f"  {q[:35]:<36} {e:8.1f} {a:8.1f} {b:8.1f} {w:8.1f}")

anns = [a for *_, a, _ in ((x[0], x[1], x[2], x[3]) for x in warm)]
passed = sum(1 for _, ok, _ in results if ok)
failed = [n for n, ok, _ in results if not ok]
print(f"\n{passed}/{len(results)} passed")
if failed:
    print("failed: " + "; ".join(failed))
sys.exit(1 if failed else 0)
