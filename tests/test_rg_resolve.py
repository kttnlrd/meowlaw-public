"""RG ids resolve over MCP, incl. the short paragraph form (rg:179:1 -> RG 179.1)."""
from backend.resolve_ref import resolve_ref


def test_rg_ids():
    r = resolve_ref("rg:179")
    assert r["id"] == "rg:179" and r["paragraph_count"] > 0
    assert resolve_ref("rg:179:179.1")["id"] == "rg:179:179.1"
    assert resolve_ref("rg:179:1")["id"] == "rg:179:179.1"
    assert resolve_ref("rg:1:1.1")["id"] == "rg:1:1.1"
    assert resolve_ref("rg:282")["id"] == "rg:282"
    assert resolve_ref("rg:999")["error"]["code"] == "not_found"
