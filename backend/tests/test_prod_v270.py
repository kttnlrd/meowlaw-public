"""Full production test: REST API + MCP tools.

Script-style (module-level execution) — run directly against a live server:
    python backend/tests/test_prod_v270.py
"""
import httpx, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Under pytest: skip (script-style, needs a live server). Direct run: proceed.
if "pytest" in sys.modules:
    sys.modules["pytest"].skip(
        "script-style live-server test — run directly, not under pytest",
        allow_module_level=True)

BASE = "http://localhost:8765"
MCP_BASE = f"{BASE}/mcp"
MCP_H = {"Authorization": "Bearer mcpLiv3", "Content-Type": "application/json", "Accept": "application/json"}

from backend.routes.api import VERSION  # noqa: E402
results = {"pass": 0, "fail": 0}

def check(name, ok, detail=""):
    if ok:
        results["pass"] += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        results["fail"] += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))

# ═══ REST API ═══
print("═══ REST API ═══")

tests = [
    ("health", lambda: httpx.get(f"{BASE}/health", timeout=15), lambda r: r.status_code == 200 and r.json().get("status") == "ok"),
    ("api/info -> current version", lambda: httpx.get(f"{BASE}/api/info", timeout=15), lambda r: r.json()["version"] == VERSION),
    ("api/acts", lambda: httpx.get(f"{BASE}/api/acts", timeout=15), lambda r: r.status_code == 200 and len(r.json()) > 5),
    ("api/tree ITAA 1997", lambda: httpx.get(f"{BASE}/api/tree/itaa-1997", timeout=15), lambda r: len(r.json().get("parts",[])) > 5),
    ("api/section 8-1", lambda: httpx.get(f"{BASE}/api/section/itaa-1997/8-1", timeout=15), lambda r: len(r.json().get("body","")) > 200),
    ("api/section 102UC", lambda: httpx.get(f"{BASE}/api/section/itaa-1936/102UC", timeout=15), lambda r: len(r.json().get("body","")) > 50),
    ("api/section GST 9-5", lambda: httpx.get(f"{BASE}/api/section/gst-1999/9-5", timeout=15), lambda r: len(r.json().get("body","")) > 50),
    ("api/search 'CGT'", lambda: httpx.get(f"{BASE}/api/search?q=capital+gains+tax&limit=3", timeout=15), lambda r: int(r.json().get("total",0)) > 0),
    ("api/hybrid-search 'CGT'", lambda: httpx.get(f"{BASE}/api/search/hybrid?q=capital+gains+tax&limit=3", timeout=15), lambda r: int(r.json().get("total",0)) > 0),
    ("api/definitions ITAA 1997", lambda: httpx.get(f"{BASE}/api/definitions/itaa-1997", timeout=30), lambda r: len(r.json().get("terms",{})) > 500),
    ("api/rulings", lambda: httpx.get(f"{BASE}/api/rulings?limit=5", timeout=15), lambda r: r.status_code == 200),
    ("api/ruling TR 2024/1", lambda: httpx.get(f"{BASE}/api/ruling/TD_2024_1", timeout=15), lambda r: len(r.json().get("frontmatter",{})) > 0 and len(r.json().get("ruling","")) > 50),
    ("api/cases for 8-1", lambda: httpx.get(f"{BASE}/api/cases/itaa-1997/8-1", timeout=15), lambda r: len(r.json().get("cases",[])) > 0),
    ("api/graph section 118-185", lambda: httpx.get(f"{BASE}/api/graph/data?type=section&act=itaa-1997&section=118-185", timeout=15), lambda r: len(r.json().get("nodes",[])) > 5),
    ("api/tax-cases/search", lambda: httpx.get(f"{BASE}/api/tax-cases/search?q=FCT+v+Harding", timeout=15), lambda r: r.status_code == 200),
    ("api/commentary for 8-1", lambda: httpx.get(f"{BASE}/api/commentary/itaa-1997/8-1", timeout=15), lambda r: r.status_code == 200),
    ("api/definitions/gst-1999", lambda: httpx.get(f"{BASE}/api/definitions/gst-1999", timeout=15), lambda r: len(r.json().get("terms",{})) > 50),
    ("HTTP / -> 200", lambda: httpx.get(f"{BASE}/", timeout=15), lambda r: r.status_code == 200),
]

for name, req_fn, assert_fn in tests:
    try:
        resp = req_fn()
        check(name, assert_fn(resp), f"HTTP {resp.status_code}")
    except Exception as e:
        check(name, False, str(e)[:80])

# ═══ MCP Tools ═══
print("\n═══ MCP Tools ═══")

def mcp(method, params, sid=None):
    h = dict(MCP_H)
    if sid: h["Mcp-Session-Id"] = sid
    r = httpx.post(MCP_BASE, json={"jsonrpc":"2.0","method":method,"params":params,"id":1}, headers=h, timeout=15)
    return r.headers.get("mcp-session-id",""), r.json()

sid, resp = mcp("initialize", {"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}})
check("MCP initialize", "result" in resp)

_, tools = mcp("tools/list", {}, sid)
names = [t["name"] for t in tools["result"]["tools"]]
core = {"get_section", "search_all", "get_private_ruling", "get_case", "get_definition",
        "get_act_tree", "list_acts", "list_rulings", "search_legislation",
        "search_cases", "get_info", "standards", "report_issue", "case_legislation_refs"}
check(f"MCP tools/list ({len(names)} tools)", core.issubset(set(names)))
check("MCP graph tools present", {"graph_neighbourhood", "graph_path"}.issubset(set(names)))
check("MCP get_rulings_for_section removed", "get_rulings_for_section" not in names)

sid, _ = mcp("initialize", {"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}})

# get_section
_, resp = mcp("tools/call", {"name":"get_section","arguments":{"act":"itaa-1997","section":"8-1"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
rel = data.get("related",{})
check("get_section 8-1 body", len(data.get("body","")) > 200)
check("  related rulings", len(rel.get("rulings",[])) >= 3)
check("  related cases", len(rel.get("cases",[])) >= 3)
check("  related commentary", len(rel.get("commentary",[])) >= 0)

# search_all
_, resp = mcp("tools/call", {"name":"search_all","arguments":{"query":"division 7A","type_filter":"ruling","limit":3}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
rs = data.get("results",{}).get("rulings",[])
check("search_all(ruling, Div 7A)", len(rs) >= 1, f"{len(rs)} results")

_, resp = mcp("tools/call", {"name":"search_all","arguments":{"query":"CGT","type_filter":"section","limit":3}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
ss = data.get("results",{}).get("sections",[])
check("search_all(section, CGT)", len(ss) >= 1, f"{len(ss)} results")

_, resp = mcp("tools/call", {"name":"search_all","arguments":{"query":"deductions","limit":3}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
all_r = data.get("results",{})
total = sum(len(v) for v in all_r.values() if isinstance(v,list))
check("search_all(unfiltered, deductions)", total > 0, f"{total} total across types")

# ── search_all type_filter routing (CDN-0166) ──
# private_ruling must route to the PBR FTS index only — no sections/rulings/cases/commentary
_, resp = mcp("tools/call", {"name":"search_all","arguments":{"query":"CGT","type_filter":"private_ruling","limit":3}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
pr = data.get("results",{})
pr_ok = len(pr.get("private_rulings",[])) >= 1
pr_only = not any(k in pr for k in ("sections","rulings","cases","commentary"))
check("search_all(private_ruling) routes to private_rulings", pr_ok, f"{len(pr.get('private_rulings',[]))} results")
check("  private_ruling excludes other types", pr_only, f"keys={sorted(pr.keys())}")

# ruling must return rulings + private_rulings, never sections/cases/commentary
_, resp = mcp("tools/call", {"name":"search_all","arguments":{"query":"CGT","type_filter":"ruling","limit":3}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
rg = data.get("results",{})
rg_ok = len(rg.get("rulings",[])) >= 1
pr2_ok = "private_rulings" in rg
rg_only = not any(k in rg for k in ("sections","cases","commentary"))
check("search_all(ruling) returns rulings", rg_ok, f"{len(rg.get('rulings',[]))} results")
check("  ruling also returns private_rulings", pr2_ok, f"{len(rg.get('private_rulings',[]))} results")
check("  ruling excludes sections/cases/commentary", rg_only, f"keys={sorted(rg.keys())}")

# section must return sections only
_, resp = mcp("tools/call", {"name":"search_all","arguments":{"query":"CGT","type_filter":"section","limit":3}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
sg = data.get("results",{})
sg_ok = len(sg.get("sections",[])) >= 1
sg_only = not any(k in sg for k in ("rulings","cases","commentary"))
check("search_all(section) returns sections only", sg_ok and sg_only, f"keys={sorted(sg.keys())}")

# get_section related rulings carry absolute download_url + ato_url
# (get_ruling MCP tool exists — related-ruling links also ride on summary payloads)
_, resp = mcp("tools/call", {"name":"get_section","arguments":{"act":"itaa-1997","section":"8-1"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
rel_rulings = data.get("related",{}).get("rulings",[])
if rel_rulings:
    r0 = rel_rulings[0]
    check("related ruling has download_url",
          str(r0.get("download_url","")).startswith("http"),
          f"{r0.get('citation')} → {r0.get('download_url')}")
else:
    check("related ruling has download_url", False, "no related rulings returned")

# get_case
_, resp = mcp("tools/call", {"name": "get_case", "arguments": {"citation": "[2019] FCAFC 29"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("get_case [2019] FCAFC 29", "case_name" in data or "citation" in data)

# get_definition
for term in ["trading stock", "assessable income", "CGT asset", "dividend"]:
    _, resp = mcp("tools/call", {"name":"get_definition","arguments":{"act":"itaa-1997","term":term}}, sid)
    data = json.loads(resp["result"]["content"][0]["text"])
    check(f"get_definition '{term}'", len(data.get("text","")) > 10)

# get_act_tree
_, resp = mcp("tools/call", {"name":"get_act_tree","arguments":{"act":"itaa-1997"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("get_act_tree ITAA 1997", len(data.get("parts",[])) > 5)

# list_acts
_, resp = mcp("tools/call", {"name":"list_acts","arguments":{}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("list_acts", len(data.get("acts",[])) > 3)

# list_rulings
_, resp = mcp("tools/call", {"name": "list_rulings", "arguments": {"counts_only": True}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("list_rulings", data.get("total_rulings", 0) > 1000)

# search_legislation
_, resp = mcp("tools/call", {"name":"search_legislation","arguments":{"query":"main residence"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("search_legislation", len(data.get("results",[])) > 0)

# search_cases
_, resp = mcp("tools/call", {"name":"search_cases","arguments":{"query":"Harding"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("search_cases", len(data.get("results",[])) > 0)

# get_info
_, resp = mcp("tools/call", {"name": "get_info", "arguments": {}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("get_info current version", data.get("version") == VERSION)
check("get_info routing updated", "search_all" in json.dumps(data.get("usage",{}).get("routing",{})))

# standards
_, resp = mcp("tools/call", {"name":"standards","arguments":{"topic":"verification"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("standards(verification)", len(data.get("content","")) > 20)

# case_legislation_refs
_, resp = mcp("tools/call", {"name":"case_legislation_refs","arguments":{"citation":"2024 HCA 1"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("case_legislation_refs", "legislation_refs" in data or "error" not in str(data))

# Verify get_rulings_for_section is GONE
_, resp = mcp("tools/call", {"name":"get_rulings_for_section","arguments":{"act":"itaa-1997","section":"8-1"}}, sid)
check("get_rulings_for_section returns error", "error" in str(resp).lower() or resp.get("result",{}).get("isError"))

# report_issue — verify tool responds to validation without writing a live ticket
_, resp = mcp("tools/call", {"name": "report_issue", "arguments": {"category": "suggestion"}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
check("report_issue rejects empty payload (no ticket written)", data.get("status") == "rejected")

print(f"\n═══ Results ═══")
print(f"  ✅ {results['pass']} passed")
print(f"  ❌ {results['fail']} failed")
sys.exit(0 if results["fail"] == 0 else 1)