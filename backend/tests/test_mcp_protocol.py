"""MCP wire behaviour through the real app: protocol versions, server/discover, error shapes,
auth carriers, per-owner gating and usage logging (docs/plans/mcp-2x-upgrade.md, section 4).

Runs under mcp 1.x and 2.x. MCP_MODERN_ERA-on cases need the 2.x SDK and skip on 1.x.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.tests.mcp_harness import (
    MODERN, OPERATOR_TOKEN, OWNERS, SDK_MAJOR, USAGE, call, live_client, modern, rpc,
    set_modern_era, tool_error, tool_text,
)

HANDSHAKE = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_HANDSHAKE = "2025-11-25"
HEADERS = (None, *HANDSHAKE, MODERN, "2099-01-01")
INSTR_START = "lawkitty returns Australian and New Zealand tax and corporate law"


@pytest.fixture(scope="module")
def live():
    with pytest.MonkeyPatch.context() as mp, live_client(mp) as (client, events):
        yield client, events


@pytest.fixture
def client(live):
    live[1].clear()
    return live[0]


@pytest.fixture
def events(live):
    return live[1]


@pytest.fixture(params=["off", "on"])
def era(request, monkeypatch):
    on = request.param == "on"
    if not set_modern_era(monkeypatch, on):
        pytest.skip("mcp 1.x has no 2026-07-28 era")
    return on


def _init(client, pv, **kw):
    return rpc(client, "initialize", {"protocolVersion": pv or LATEST_HANDSHAKE, "capabilities": {},
                                      "clientInfo": {"name": "pytest", "version": "1"}}, pv=pv, **kw)


# --- protocol-version matrix ---------------------------------------------------------------

@pytest.mark.parametrize("pv", HEADERS)
def test_legacy_requests_served_for_every_header(client, era, pv):
    """Legacy-shaped requests (no _meta envelope). Flag off: every header is served, unknown and
    2026-07-28 down-negotiated to 2025-11-25. Flag on: 2026-07-28 goes to the modern path, which
    rejects a request without the envelope; unknown values are still down-negotiated."""
    rejected = era and pv == MODERN
    r_init, r_list = _init(client, pv), rpc(client, "tools/list", {}, pv=pv)
    r_call = call(client, "fetch", {"ref": "leg:itaa-1997:8-1"}, pv=pv)
    if rejected:
        for r in (r_list, r_call):
            assert r.status_code == 400
            assert r.json()["error"]["code"] == -32602
        return
    for r in (r_init, r_list, r_call):
        assert r.status_code == 200, (pv, r.text[:300])
        assert "mcp-session-id" not in r.headers
    assert r_init.json()["result"]["protocolVersion"] == (pv if pv in HANDSHAKE else LATEST_HANDSHAKE)
    assert len(r_list.json()["result"]["tools"]) == 14
    assert json.loads(tool_text(r_call))["id"] == "leg:itaa-1997:8-1"


@pytest.mark.parametrize("offered", [*HANDSHAKE, MODERN, "2099-01-01"])
def test_initialize_counter_offers_latest_handshake(client, era, offered):
    r = rpc(client, "initialize", {"protocolVersion": offered, "capabilities": {},
                                   "clientInfo": {"name": "pytest", "version": "1"}})
    res = r.json()["result"]
    assert res["protocolVersion"] == (offered if offered in HANDSHAKE else LATEST_HANDSHAKE)
    assert res["serverInfo"]["name"] == "lawkitty"
    assert res["instructions"].startswith(INSTR_START)


@pytest.mark.parametrize("pv", [MODERN, "2099-01-01"])
def test_downgrade_is_recorded(client, events, era, pv):
    rpc(client, "tools/list", {}, pv=pv)
    (ev,) = events
    if era and pv == MODERN:
        assert "protocol_version_requested" not in (ev.get("params") or "")
    else:
        assert json.loads(ev["params"]) == {"protocol_version_requested": pv, "used": LATEST_HANDSHAKE}


def test_modern_envelope_request(client, events, era):
    """A 2026-07-28 client (envelope + Mcp-Method/Mcp-Name). Flag off: served on the legacy path
    (header down-negotiated, envelope ignored). Flag on: served natively on the modern path."""
    r_list = modern(client, "tools/list", {})
    r_call = modern(client, "tools/call", {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}},
                    name="fetch")
    for r in (r_list, r_call):
        assert r.status_code == 200, r.text[:300]
        assert "mcp-session-id" not in r.headers
    assert len(r_list.json()["result"]["tools"]) == 14
    assert json.loads(tool_text(r_call))["id"] == "leg:itaa-1997:8-1"
    assert r_call.headers["x-ratelimit-limit"] == str(USAGE["cap"])
    # tools/call rows carry the call arguments in params, so only the tools/list row shows it.
    downgraded = [e["tool"] for e in events if "protocol_version_requested" in (e.get("params") or "")]
    assert downgraded == ([] if era else ["rpc:tools/list"])


def test_modern_header_mismatch_is_rejected(client, era):
    """Flag on: the modern path checks Mcp-Method against the body (-32020 on mismatch)."""
    r = modern(client, "tools/list", {}, extra={"Mcp-Method": "tools/call"})
    if era:
        assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    else:
        assert r.status_code == 200


# --- server/discover -----------------------------------------------------------------------

_SHIM = {"code": -32601, "message": "Method not found: server/discover"}


@pytest.mark.parametrize("shape", ["bare", "header", "envelope"])
def test_server_discover(client, events, era, shape):
    if shape == "bare":
        r = rpc(client, "server/discover", {}, rid=9)
    elif shape == "header":
        r = rpc(client, "server/discover", {}, pv=MODERN, rid=9)
    else:
        r = modern(client, "server/discover", {}, rid=9)
    if not era:  # the middleware shim, exactly as on mcp 1.26
        assert r.status_code == 200
        assert r.json() == {"jsonrpc": "2.0", "id": 9, "error": _SHIM}
        assert events[-1]["status"] == "ok" and events[-1]["tool"] == "rpc:server/discover"
        return
    if shape == "bare":  # legacy-era probe: native -32601, client falls back to initialize
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32601
    elif shape == "header":  # modern header without the envelope
        assert r.status_code == 400 and r.json()["error"]["code"] == -32602
    else:
        assert r.status_code == 200, r.text[:300]
        res = r.json()["result"]
        assert res["supportedVersions"] == [MODERN]
        assert res["ttlMs"] == 0
        assert INSTR_START in json.dumps(res)


# --- structured error shapes ---------------------------------------------------------------

@pytest.mark.parametrize("name,args,code", [
    ("fetch", {}, "empty_ref"),
    ("fetch", {"ref": "leg:itaa-1997:zz-999"}, "not_found"),
    ("fetch", {"ref": "leg:itaa-1997:8-1", "chunk": "banana"}, "chunk_out_of_range"),
    ("search", {"query": "x", "types": ["bogus"]}, "invalid_param"),
    ("notice", {"ref": ""}, "invalid_param"),
])
def test_tool_error_shape(client, events, era, name, args, code):
    r = call(client, name, args)
    assert r.status_code == 200
    err = tool_error(r)
    assert err["code"] == code and isinstance(err["message"], str) and isinstance(err["suggestions"], list)
    assert r.json()["result"]["content"][0]["text"].startswith(f"Error executing tool {name}: {{")
    (ev,) = events
    assert ev["status"] == "error" and ev["error_kind"] == "RuntimeError"  # as on mcp 1.x
    assert json.loads(ev["error_detail"])["code"] == code


def test_unknown_tool_and_validation_errors(client, events, era):
    r = call(client, "nope", {})
    assert r.json()["result"] == {"content": [{"type": "text", "text": "Unknown tool: nope"}], "isError": True}
    assert events[-1]["error_kind"] == "UnknownTool"
    r = call(client, "search", {"query": "x", "limit": "abc"})
    text = r.json()["result"]["content"][0]["text"]
    assert r.json()["result"]["isError"] is True
    assert text.startswith("Error executing tool search: 1 validation error for searchArguments\nlimit\n")
    assert events[-1]["error_kind"] == "ValidationError"


def test_uncaught_exception(client, events, era):
    """Owner decision: 1.x passed the exception text through; 2.x sends a generic message and
    logs the traceback. The usage row keeps the real exception either way."""
    r = call(client, "fetch", {"refs": "not json"})
    text = r.json()["result"]["content"][0]["text"]
    assert r.json()["result"]["isError"] is True
    if SDK_MAJOR >= 2:
        assert text == "Error executing tool fetch"
    else:
        assert text == "Error executing tool fetch: Expecting value: line 1 column 1 (char 0)"
    assert events[-1]["error_kind"] == "JSONDecodeError"


def test_batch_item_errors_keep_their_label(client, era):
    out = json.loads(tool_text(call(client, "fetch", {"refs": ["leg:itaa-1997:zz-999"]})))
    assert out["items"][0]["error"].startswith('RuntimeError: {"code": "not_found"')


# --- auth carriers -------------------------------------------------------------------------

CARRIERS = ["bearer", "x-api-key", "query", "path", "body", "cookie", "x-session-id"]


@pytest.mark.parametrize("carrier", CARRIERS)
def test_auth_carrier_reaches_tool(client, events, era, carrier):
    r = call(client, "fetch", {"ref": "leg:itaa-1997:8-1"}, token="tok-std", carrier=carrier)
    assert r.status_code == 200, r.text[:300]
    assert json.loads(tool_text(r))["id"] == "leg:itaa-1997:8-1"
    (ev,) = events
    assert ev["user_email"] == OWNERS["tok-std"] and ev.get("_tool_ran") is True
    assert ev["status"] == "ok" and ev["tool"] == "fetch"


def test_auth_carrier_modern_path(client, events, era):
    r = modern(client, "tools/call", {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}},
               name="fetch", token="tok-std", carrier="path")
    assert r.status_code == 200 and json.loads(tool_text(r))["id"] == "leg:itaa-1997:8-1"
    assert events[-1]["user_email"] == OWNERS["tok-std"] and events[-1].get("_tool_ran") is True


@pytest.mark.parametrize("token,owner", [(OPERATOR_TOKEN, "operator"), ("tok-oauth", "oauth@example.invalid")])
def test_operator_and_oauth_tokens(client, events, era, token, owner):
    r = call(client, "fetch", {"ref": "leg:itaa-1997:8-1"}, token=token)
    assert r.status_code == 200 and events[-1]["user_email"] == owner


@pytest.mark.parametrize("token,status,kind", [
    (None, 401, "auth_missing"), ("tok-bogus", 403, "auth_invalid"),
    ("tok-capped", 429, "daily_cap"), ("tok-noacct", 403, "no_account"),
])
def test_auth_rejections(client, events, era, token, status, kind):
    r = call(client, "fetch", {"ref": "leg:itaa-1997:8-1"}, token=token)
    assert r.status_code == status
    (ev,) = events
    assert ev["status"] == "error" and ev["error_kind"] == kind and not ev.get("_tool_ran")


def test_rate_limit_headers_only_on_tool_calls(client, era):
    r = call(client, "fetch", {"ref": "leg:itaa-1997:8-1"})
    assert (r.headers["x-ratelimit-limit"], r.headers["x-ratelimit-remaining"], r.headers["x-ratelimit-reset"]) \
        == (str(USAGE["cap"]), str(USAGE["cap"] - USAGE["used"]), str(USAGE["reset_in"]))
    assert "x-ratelimit-limit" not in rpc(client, "tools/list", {}).headers


@pytest.mark.parametrize("host,status", [("evil.example", 421), ("lawkitty.app", 200), ("localhost:9999", 200)])
def test_host_allow_list(client, era, host, status):
    assert rpc(client, "tools/list", {}, extra={"Host": host}).status_code == status
    r = modern(client, "tools/list", {}, extra={"Host": host})
    assert r.status_code == status


# --- per-owner gating ----------------------------------------------------------------------

def _gated_search(client, token, use_modern):
    args = {"name": "search", "arguments": {"query": "x", "types": ["commentary"]}}
    if use_modern:
        return token, modern(client, "tools/call", args, name="search", token=token)
    return token, rpc(client, "tools/call", args, token=token)


@pytest.mark.parametrize("use_modern", [False, True], ids=["legacy", "modern"])
def test_gating_per_owner_concurrently(client, events, era, use_modern):
    """Two owners with different grants, interleaved on worker threads: each sees only its own
    availability, and each usage row names its own owner."""
    tokens = ["tok-std", "tok-granted"] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: _gated_search(client, t, use_modern), tokens))
    # a private type reads exactly as unindexed to a caller without a grant (no "not_available" tell)
    want = {"tok-std": "not_indexed", "tok-granted": "not_indexed"}
    for token, r in results:
        assert r.status_code == 200, r.text[:300]
        assert tool_error(r)["code"] == want[token], token
    by_owner = {}
    for ev in events:
        assert ev["tool"] == "search" and ev["error_kind"] == "RuntimeError"  # _tool_error, as on 1.x
        by_owner.setdefault(ev["user_email"], []).append(json.loads(ev["error_detail"])["code"])
    assert by_owner == {OWNERS["tok-std"]: ["not_indexed"] * 4, OWNERS["tok-granted"]: ["not_indexed"] * 4}


def test_gated_fetch_per_owner(client, era):
    std = call(client, "fetch", {"ref": "leg:master-tax-guide:1"}, token="tok-std")
    admin = call(client, "fetch", {"ref": "leg:master-tax-guide:1"}, token="tok-admin")
    # denied reads exactly as missing
    assert tool_error(std)["code"] == "not_found"
    assert tool_error(admin)["code"] == "not_found"


# --- usage logging -------------------------------------------------------------------------

def test_usage_rows(client, events, era):
    call(client, "fetch", {"ref": "s 8-1 ITAA 1997"}, token="tok-std")
    rpc(client, "tools/list", {}, token="tok-std")
    call(client, "fetch", {"ref": "x"}, token=None)
    call(client, "fetch", {"ref": "leg:itaa-1997:zz-999"}, token="tok-std")
    assert [(e["tool"], e["status"], e.get("error_kind")) for e in events] == [
        ("fetch", "ok", None), ("rpc:tools/list", "ok", None), ("fetch", "error", "auth_missing"),
        ("fetch", "error", "RuntimeError")]
    fetch_ev = events[0]
    assert fetch_ev["content_ref"] == "s 8-1 ITAA 1997" and fetch_ev["result_count"] == 1
    assert fetch_ev["transport"] == "mcp" and fetch_ev["response_bytes"] > 0
    assert fetch_ev["latency_ms"] >= 0 and fetch_ev["token_hashed"] is True
    assert fetch_ev["token"] != "tok-std" and len(fetch_ev["token"]) == 64
    assert events[2].get("user_email") is None and not events[2].get("_tool_ran")
    assert events[3]["user_email"] == OWNERS["tok-std"] and events[3]["content_ref"] == "s zz-999 ITAA 1997"
    assert all("tok-std" not in json.dumps(e, default=str) for e in events)


# --- web playground (shares the SDK's private tool registry) -------------------------------

def test_playground_lists_and_runs_tools(client):
    from backend.auth import create_session_token
    from backend.tests.mcp_harness import TOOLS_SNAPSHOT
    cookie = {"Cookie": f"session={create_session_token({'email': 'admin@example.com', 'sub': 'admin@example.com'})}"}
    r = client.get("/api/playground/tools", headers=cookie)
    assert r.status_code == 200, r.text[:300]
    snapshot = {t["name"]: t["inputSchema"] for t in json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))}
    assert {t["name"]: t["parameters"] for t in r.json()["tools"]} == snapshot
    ok = client.post("/api/playground", headers=cookie, json={"tool": "fetch", "args": {"ref": "leg:itaa-1997:8-1"}})
    assert ok.status_code == 200 and ok.json()["result"]["id"] == "leg:itaa-1997:8-1"
    bad = client.post("/api/playground", headers=cookie, json={"tool": "fetch", "args": {"ref": ""}})
    assert bad.status_code == 400
    assert bad.json()["error"].startswith('RuntimeError: {"code": "empty_ref"')


def test_modern_era_flag_defaults_off():
    import os
    from backend import fastmcp_server as fs
    if not hasattr(fs, "MCP_MODERN_ERA"):
        pytest.skip("mcp 1.x has no 2026-07-28 era")
    assert "MCP_MODERN_ERA" not in os.environ, "run the suite without MCP_MODERN_ERA set"
    assert fs.MCP_MODERN_ERA is False
    assert MODERN not in fs._accepted_versions()
