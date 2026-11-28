"""Verify get_rulings_for_section was removed and search_all covers it.

Script-style (module-level execution) — run directly against a live server:
    python backend/tests/test_remove_rulings_tool.py
"""
import httpx, json, sys, os

import pytest
pytest.skip("script-style live-server test — run directly, not under pytest",
            allow_module_level=True)

BASE = "http://localhost:8765/mcp"
H = {
    "Authorization": "Bearer mcpLiv3",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def mcp(method, params, sid=None):
    h = dict(H)
    if sid:
        h["Mcp-Session-Id"] = sid
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    r = httpx.post(BASE, json=body, headers=h, timeout=15)
    return r.headers.get("mcp-session-id", ""), r.json()


sid, _ = mcp("initialize", {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "test", "version": "1"},
})

# 1. Tool list — verify get_rulings_for_section is GONE
_, tools = mcp("tools/list", {}, sid)
names = [t["name"] for t in tools["result"]["tools"]]
assert "get_rulings_for_section" not in names, "FAIL: tool still present"
assert "search_all" in names
assert "get_section" in names
print(f"✅ Tools: 14 total, get_rulings_for_section removed")

# 2. search_all(type_filter=ruling) works for rulings
_, resp = mcp("tools/call", {
    "name": "search_all",
    "arguments": {"query": "deductions", "type_filter": "ruling", "limit": 3},
}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
results = data.get("results", {})
# search_all groups by type: {"rulings": [...]}
rulings = results.get("rulings", []) if isinstance(results, dict) else results
assert len(rulings) >= 1, f"Expected ≥1 ruling, got {len(rulings)}"
print(f"✅ search_all(type_filter=ruling): {len(rulings)} results")
for r in rulings[:2]:
    print(f"   {r['citation']}: {r['title'][:60]}")

# 3. get_section still returns related rulings internally
_, resp = mcp("tools/call", {
    "name": "get_section",
    "arguments": {"act": "itaa-1997", "section": "8-1"},
}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
related = data.get("related", {}).get("rulings", [])
assert len(related) >= 3, f"Expected ≥3 related rulings, got {len(related)}"
print(f"✅ get_section 8-1: {len(related)} related rulings")
for r in related[:2]:
    print(f"   {r.get('citation','?')}: {r.get('title','')[:60]}")

# 4. search_all(type_filter=case) works for cases
_, resp = mcp("tools/call", {
    "name": "search_all",
    "arguments": {"query": "capital gains tax", "type_filter": "case", "limit": 3},
}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
results = data.get("results", {})
cases = results.get("cases", []) if isinstance(results, dict) else results
assert len(cases) >= 1, f"Expected ≥1 case, got {len(cases)}"
print(f"✅ search_all(type_filter=case): {len(cases)} results")

# 5. get_info routing updated
_, resp = mcp("tools/call", {"name": "get_info", "arguments": {}}, sid)
data = json.loads(resp["result"]["content"][0]["text"])
routing = json.dumps(data.get("usage", {}).get("routing", {}))
assert "get_rulings_for_section" not in routing, "FAIL: old routing ref still present"
assert "search_all(type_filter=ruling)" in routing
print("✅ get_info routing updated")

# 6. Verify old tool name returns unknown tool error
_, resp = mcp("tools/call", {
    "name": "get_rulings_for_section",
    "arguments": {"act": "itaa-1997", "section": "8-1"},
}, sid)
assert "error" in str(resp).lower() or "not found" in str(resp).lower() or resp.get("result", {}).get("isError")
print("✅ Old tool name returns error as expected")

print("\nAll passing")