"""Requests mcp 1.26 served that the 2.x SDK refuses on its own, and the usage label 1.26 wrote.

MCPAuthMiddleware keeps them working (fastmcp_server._compat_body, the empty-header
down-negotiation, MCP_MAX_REQUEST_BODY_SIZE, usage_log.error_label). Each test comes from an
adversarial review of the 2.x branch; the 1.26 answers are also pinned in the golden snapshot
(test_mcp_golden.py). Unless a test says otherwise it runs with MCP_MODERN_ERA off and on: old
clients must keep working after the flag is turned on.
"""
import json

import pytest

from backend.tests.mcp_harness import ACCEPT, MODERN, call, headers_for, live_client, rpc, set_modern_era, tool_text

LIST = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "compat", "version": "1"}}


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
    set_modern_era(monkeypatch, on)
    return on


@pytest.fixture
def era_off(monkeypatch):
    set_modern_era(monkeypatch, False)


def post(client, content: bytes, **hdr):
    h = headers_for("tok-std")
    h.update(hdr)
    return client.post("/mcp", headers=h, content=content)


# --- "_meta": {"progressToken": null} -------------------------------------------------------

@pytest.mark.parametrize("method,params", [
    ("initialize", INIT),
    ("tools/list", {}),
    ("tools/call", {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}}),
    ("ping", {}),
])
@pytest.mark.parametrize("pv", [None, "2025-06-18", MODERN])
def test_null_progress_token_is_served(client, era, method, params, pv):
    """A serialiser that writes absent optionals as null. 2.x alone answers -32602, so such a
    client could not even initialize. (With the flag on, a 2026-07-28 header without the _meta
    envelope is the modern path's rejection, not this one.)"""
    if era and pv == MODERN:
        pytest.skip("flag on: a 2026-07-28 header needs the modern envelope")
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": {"progressToken": None}}}
    hdr = {"MCP-Protocol-Version": pv} if pv else {}
    r = post(client, json.dumps(body).encode(), **hdr)
    assert r.status_code == 200, r.text[:300]
    assert "error" not in r.json(), r.json()["error"]
    assert "result" in r.json()


def test_set_progress_token_is_left_alone():
    from backend.fastmcp_server import _compat_body
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"_meta": {"progressToken": "p1"}}}
    assert _compat_body(json.dumps(body).encode(), body) is None


# --- bodies the 1.x JSON parser (json.loads) accepted ----------------------------------------

def test_utf8_bom_body_is_served(client, era):
    r = post(client, b"\xef\xbb\xbf" + LIST)
    assert r.status_code == 200, r.text[:200]
    assert len(r.json()["result"]["tools"]) == 14


def test_utf16_body_is_served(client, era):
    r = post(client, LIST.decode().encode("utf-16"))
    assert r.status_code == 200, r.text[:200]
    assert len(r.json()["result"]["tools"]) == 14


def test_lone_surrogate_escape_in_arguments_is_served(client, events, era):
    """A JS client that cuts a string inside an emoji sends the unpaired half as an escape. 2.x
    alone refuses the whole call at transport level (400 -32700, id null)."""
    body = (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search",'
            b'"arguments":{"query":"capital gains \\ud83d","limit":1}}}')
    r = post(client, body)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["id"] == 1 and r.json()["result"].get("isError") is not True
    assert json.loads(tool_text(r))["query"] == "capital gains \ufffd"
    (ev,) = events
    assert ev["tool"] == "search" and ev["status"] == "ok" and ev.get("_tool_ran") is True


def test_well_formed_body_is_forwarded_unchanged():
    from backend.fastmcp_server import _compat_body
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "caf\u00e9 \U0001F600"}}}
    for raw in (json.dumps(body).encode(), json.dumps(body, ensure_ascii=False).encode()):
        assert _compat_body(raw, json.loads(raw)) is None


# --- request bodies over 4 MiB (2.x's default max_request_body_size) -------------------------

def test_body_over_4_mib_is_served(client, events, era):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "report_bad_recall", "arguments": {"query": "CR 2002/71", "note": "n" * (5 * 1024 * 1024)}}})
    r = post(client, body.encode())
    assert r.status_code == 200, (r.status_code, r.text[:100])
    assert r.json()["id"] == 1 and "result" in r.json()
    assert events[-1]["tool"] == "report_bad_recall" and events[-1].get("_tool_ran") is True


def test_sdk_body_limit_is_lifted():
    from backend.fastmcp_server import MCP_MAX_REQUEST_BODY_SIZE
    from backend.main import fastmcp
    assert fastmcp.session_manager.max_request_body_size == MCP_MAX_REQUEST_BODY_SIZE >= 2**62


# --- MCP-Protocol-Version header present but empty -------------------------------------------

def test_initialize_with_empty_header_is_served(client, era):
    r = rpc(client, "initialize", INIT, extra={"MCP-Protocol-Version": ""})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"


def test_empty_header_is_down_negotiated(client, events, era):
    """1.26 skipped the rewrite for an empty header by accident (400 -32600 on all but initialize);
    it is now treated as any unknown revision: served, and the downgrade recorded."""
    r = rpc(client, "tools/list", {}, extra={"MCP-Protocol-Version": ""})
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 14
    assert json.loads(events[-1]["params"]) == {"protocol_version_requested": "", "used": "2025-11-25"}


# --- initialize with a non-string protocolVersion ------------------------------------------

@pytest.mark.parametrize("offered", [20250618, 2025.0, True])
def test_initialize_numeric_protocol_version_gets_counter_offer(client, era, offered):
    """1.x typed protocolVersion as str | int and counter-offered its latest revision."""
    r = rpc(client, "initialize", {**INIT, "protocolVersion": offered})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_initialize_non_numeric_protocol_version_still_rejected(client, era):
    r = rpc(client, "initialize", {**INIT, "protocolVersion": 2025.06})
    assert r.json()["error"]["code"] == -32602  # as on 1.x


# --- usage rows ------------------------------------------------------------------------------

@pytest.mark.parametrize("args,code", [({}, "empty_ref"), ({"ref": "leg:itaa-1997:zz-999"}, "not_found")])
def test_usage_row_error_kind_for_structured_tool_error(client, events, era, args, code):
    """mcp_call_log.error_kind for _tool_error stays RuntimeError (the label written before 2.x,
    shown verbatim by the admin usage dashboard), although the exception is now ToolError."""
    r = call(client, "fetch", args, token="tok-std")
    assert f'"code": "{code}"' in tool_text(r)
    (ev,) = events
    assert ev["status"] == "error" and ev["error_kind"] == "RuntimeError"


# --- Accept header: an accepted difference ---------------------------------------------------

def test_accept_any_is_served_but_unacceptable_types_are_not(client, era_off):
    """2.x's Accept check takes */* (1.26 answered 406 'Client must accept application/json').
    Reviewed and accepted: it only serves clients that 1.26 turned away. A type that excludes
    JSON is still 406."""
    assert post(client, LIST, Accept="*/*").status_code == 200
    assert post(client, LIST, Accept=ACCEPT).status_code == 200
    assert post(client, LIST, Accept="text/html").status_code == 406
