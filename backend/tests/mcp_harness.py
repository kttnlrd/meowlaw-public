"""In-process MCP harness: the real backend.main:app, MCPAuthMiddleware and SDK, no Postgres.

Token resolution, rate limits, content grants and the usage-log writer are stubbed, so the
same requests can be replayed under any mcp SDK version and compared field by field.

Golden snapshot (captured from mcp 1.26 before the 2.x swap):
    python -m backend.tests.mcp_harness --write
writes backend/tests/fixtures/mcp_golden.json (every case in CASES) and mcp_tools_list.json
(the tools/list result alone); test_mcp_golden.py replays both. The golden includes document
text, so a data or tool-description change means re-running --write and reviewing the diff.
"""
from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

for _k in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "DEV_MODE"):
    os.environ.pop(_k, None)
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ.setdefault("OPENAI_API_KEY", "sk-dummy")
os.environ.setdefault("ADMIN_EMAILS", "admin@example.com")

import pytest  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "mcp_golden.json"
TOOLS_SNAPSHOT = Path(__file__).parent / "fixtures" / "mcp_tools_list.json"
SDK_VERSION = importlib.metadata.version("mcp")
SDK_MAJOR = int(SDK_VERSION.split(".")[0])
ACCEPT = "application/json, text/event-stream"
BASE_URL = "http://127.0.0.1:8000"

# token -> owner email. "tok-operator" is matched by MCP_AUTH_TOKEN, not by the token table.
OWNERS = {
    "tok-admin": "admin@example.com",
    "tok-std": "std@example.invalid",
    "tok-granted": "granted@example.invalid",
    "tok-capped": "capped@example.invalid",
    "tok-noacct": "noacct@example.invalid",
}
OAUTH_TOKENS = {"tok-oauth": "oauth@example.invalid"}
OPERATOR_TOKEN = "tok-operator"
# Per-owner content grants (the Postgres user_content_grants table). Admin-only types are
# commentary, insolvency and nz-mtg; everyone else gets the user-auth types.
GRANTS = {"granted@example.invalid": {"commentary"}}
USAGE = {"used": 7, "cap": 500, "reset_in": 3600}

MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "harness", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {}}


def _check_rate_limit(token, owner="", tier="power"):
    if owner == OWNERS["tok-capped"]:
        return False, "Daily MCP limit reached (500/day)"
    if owner == OWNERS["tok-noacct"]:
        return False, "upgrade_required"
    return True, ""


@contextlib.contextmanager
def stubbed(mp: pytest.MonkeyPatch):
    """Patch every Postgres-backed dependency of the /mcp path. Yields the usage-event list."""
    from backend import fastmcp_server as fs
    from backend.mcp_token_manager import token_manager
    from backend.middleware import ratelimit
    from backend.oauth_provider import provider as oauth_provider
    from backend.services import content_access, usage_log

    events: list[dict] = []
    # The HTTP limiter is configured at import of backend.main, possibly by another test module.
    mp.setattr(ratelimit, "_find_limit", lambda path: (60, 10**9, "all"))
    mp.setattr(token_manager, "owner_of", lambda t: OWNERS.get(t))
    mp.setattr(token_manager, "tier_for_email", lambda e: "power")
    mp.setattr(token_manager, "check_rate_limit", _check_rate_limit)
    mp.setattr(token_manager, "daily_usage", lambda owner, tier="trial": dict(USAGE))
    mp.setattr(oauth_provider, "load_access_token",
               lambda t: {"user_email": OAUTH_TOKENS[t]} if t in OAUTH_TOKENS else None)
    mp.setattr(usage_log, "record", lambda ev: events.append(dict(ev)))
    mp.setattr(content_access, "_settings", lambda: {})
    mp.setattr(content_access, "_grants", lambda email: set(GRANTS.get(email, set())))
    mp.setenv("MCP_AUTH_TOKEN", OPERATOR_TOKEN)
    mp.setattr(fs, "_failed_auth", {})
    yield events


def set_modern_era(mp: pytest.MonkeyPatch, on: bool) -> bool:
    """Force the MCP_MODERN_ERA flag. Returns False when this SDK has no modern era."""
    from backend import fastmcp_server as fs
    if not hasattr(fs, "MCP_MODERN_ERA"):
        return not on
    mp.setattr(fs, "MCP_MODERN_ERA", on)
    return True


@contextlib.contextmanager
def live_client(mp: pytest.MonkeyPatch):
    """TestClient with the app lifespan running (the MCP session manager needs it)."""
    from fastapi.testclient import TestClient
    from backend.main import app, fastmcp
    try:
        with stubbed(mp) as events, TestClient(app, base_url=BASE_URL) as client:
            yield client, events
    finally:
        # The SDK session manager refuses a second run() per instance, and every module that
        # enters the app lifespan (this harness, test_usage_api) shares the one in backend.main.
        fastmcp.session_manager._has_started = False


def headers_for(token: str | None = "tok-admin", carrier: str = "bearer", pv: str | None = None,
                extra: dict | None = None) -> dict:
    h = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if token and carrier == "bearer":
        h["Authorization"] = f"Bearer {token}"
    elif token and carrier == "x-api-key":
        h["X-API-Key"] = token
    elif token and carrier == "x-session-id":
        h["X-Session-Id"] = token
    elif token and carrier == "cookie":
        h["Cookie"] = f"token={token}"
    if pv:
        h["MCP-Protocol-Version"] = pv
    h.update(extra or {})
    return h


def rpc(client, method: str, params: dict | None = None, *, token: str | None = "tok-admin",
        carrier: str = "bearer", pv: str | None = None, extra: dict | None = None,
        path: str = "/mcp", rid=1, http_method: str = "POST"):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    url = path
    if token and carrier == "query":
        url = f"{path}?token={token}"
    elif token and carrier == "path":
        url = f"{path}/{token}"
    elif token and carrier == "body":
        body.setdefault("params", {})["_auth"] = token
    hdr = headers_for(token, carrier, pv, extra)
    if http_method == "POST":
        return client.post(url, headers=hdr, content=json.dumps(body))
    return client.request(http_method, url, headers=hdr)


def call(client, name: str, arguments: dict | None = None, **kw):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kw)


def modern(client, method: str, params: dict | None = None, *, name: str | None = None, **kw):
    """A 2026-07-28 request: _meta envelope plus Mcp-Method / Mcp-Name headers."""
    p = dict(params or {})
    p["_meta"] = META
    extra = {"Mcp-Method": method, **({"Mcp-Name": name} if name else {}), **kw.pop("extra", {})}
    return rpc(client, method, p, pv=MODERN, extra=extra, **kw)


def tool_text(resp) -> str:
    return resp.json()["result"]["content"][0]["text"]


def tool_error(resp) -> dict:
    """Parse 'Error executing tool <name>: {json}' into the structured error dict."""
    res = resp.json()["result"]
    assert res["isError"] is True
    text = res["content"][0]["text"]
    prefix, _, payload = text.partition(": ")
    assert prefix.startswith("Error executing tool "), text
    return json.loads(payload)


# --- golden snapshot -----------------------------------------------------------------------

_INIT = {"capabilities": {}, "clientInfo": {"name": "golden", "version": "1"}}
_LIST = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# (name, kind, spec). kind: rpc | call | http | raw. Every case is legacy-shaped or a header that the
# middleware down-negotiates, so with MCP_MODERN_ERA off each must answer exactly as on 1.26.
CASES: list[tuple[str, str, dict]] = [
    ("tools_list", "rpc", {"method": "tools/list", "params": {}}),
    *[(f"initialize_{v}", "rpc", {"method": "initialize", "params": {**_INIT, "protocolVersion": v}})
      for v in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", MODERN, "2099-01-01")],
    (f"initialize_{MODERN}_header", "rpc",
     {"method": "initialize", "params": {**_INIT, "protocolVersion": MODERN}, "pv": MODERN}),
    ("server_discover", "rpc", {"method": "server/discover", "params": {}}),
    ("server_discover_rid_str", "rpc", {"method": "server/discover", "params": {}, "rid": "d-1"}),
    ("server_discover_header_modern", "rpc", {"method": "server/discover", "params": {}, "pv": MODERN}),
    ("server_discover_envelope", "rpc",
     {"method": "server/discover", "params": {"_meta": META}, "pv": MODERN,
      "extra": {"Mcp-Method": "server/discover"}}),
    *[(f"tools_list_header_{v}", "rpc", {"method": "tools/list", "params": {}, "pv": v})
      for v in ("2024-11-05", "2025-11-25", MODERN, "2099-01-01")],
    ("tools_list_envelope", "rpc",
     {"method": "tools/list", "params": {"_meta": META}, "pv": MODERN, "extra": {"Mcp-Method": "tools/list"}}),
    ("unknown_method", "rpc", {"method": "nope/nothing", "params": {}}),
    ("ping", "rpc", {"method": "ping"}),
    # Methods clients send after initialize because the capabilities advertise them.
    ("resources_list", "rpc", {"method": "resources/list", "params": {}}),
    ("resources_templates_list", "rpc", {"method": "resources/templates/list", "params": {}}),
    ("prompts_list", "rpc", {"method": "prompts/list", "params": {}}),
    ("resources_read_unknown", "rpc", {"method": "resources/read", "params": {"uri": "file:///nope"}}),
    ("prompts_get_unknown", "rpc", {"method": "prompts/get", "params": {"name": "nope"}}),
    ("logging_set_level", "rpc", {"method": "logging/setLevel", "params": {"level": "info"}}),
    ("tools_list_cursor", "rpc", {"method": "tools/list", "params": {"cursor": "abc"}}),
    ("call_no_arguments_key", "rpc", {"method": "tools/call", "params": {"name": "list_content_types"}}),
    # tool calls
    ("call_fetch_ok", "call", {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}}),
    ("call_fetch_ok_header_modern", "call",
     {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}, "pv": MODERN}),
    ("call_fetch_ok_envelope", "call",
     {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}, "pv": MODERN, "meta": True,
      "extra": {"Mcp-Method": "tools/call", "Mcp-Name": "fetch"}}),
    ("call_fetch_citation_chunk_range", "call",
     {"name": "fetch", "arguments": {"ref": "s 8-1 ITAA 1997", "chunk": "1-2"}}),
    ("call_fetch_empty_ref", "call", {"name": "fetch", "arguments": {}}),
    ("call_fetch_not_found", "call", {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:zz-999"}}),
    ("call_fetch_unknown_ref", "call", {"name": "fetch", "arguments": {"ref": "qqqq zzzz nonsense"}}),
    ("call_fetch_bad_chunk", "call",
     {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1", "chunk": "banana"}}),
    ("call_fetch_refs_validation", "call", {"name": "fetch", "arguments": {"refs": "{\"a\": 1}"}}),
    # Uncaught (non-ToolError) exceptions: 1.x sends the text, 2.x a generic message (owner decision).
    ("crash_fetch_refs_not_json", "call", {"name": "fetch", "arguments": {"refs": "not json"}}),
    ("call_fetch_batch_mixed", "call",
     {"name": "fetch", "arguments": {"refs": ["leg:itaa-1997:8-1", "leg:itaa-1997:zz-999", ""]}}),
    ("call_fetch_gated_std", "call",
     {"name": "fetch", "arguments": {"ref": "leg:master-tax-guide:1"}, "token": "tok-std"}),
    ("call_fetch_gated_admin", "call",
     {"name": "fetch", "arguments": {"ref": "leg:master-tax-guide:1"}, "token": "tok-admin"}),
    ("call_search_bad_type", "call", {"name": "search", "arguments": {"query": "x", "types": ["bogus"]}}),
    ("call_search_gated_std", "call",
     {"name": "search", "arguments": {"query": "x", "types": ["commentary"]}, "token": "tok-std"}),
    ("call_search_gated_granted", "call",
     {"name": "search", "arguments": {"query": "x", "types": ["commentary"]}, "token": "tok-granted"}),
    ("call_search_bad_limit", "call", {"name": "search", "arguments": {"query": "x", "limit": "abc"}}),
    ("call_search_missing_query", "call", {"name": "search", "arguments": {}}),
    ("call_unknown_tool", "call", {"name": "nope", "arguments": {}}),
    ("call_get_info", "call", {"name": "get_info", "arguments": {}}),
    ("call_list_content_types_std", "call", {"name": "list_content_types", "arguments": {}, "token": "tok-std"}),
    ("call_outline_bad", "call", {"name": "outline", "arguments": {"id": "nonsense-act"}}),
    ("call_notice", "call", {"name": "notice", "arguments": {"ref": "leg:itaa-1997:8-1"}}),
    ("call_notice_empty", "call", {"name": "notice", "arguments": {"ref": ""}}),
    ("call_get_definition", "call", {"name": "get_definition", "arguments": {"act": "itaa-1997", "term": "entity"}}),
    ("call_report_bad_recall", "call",
     {"name": "report_bad_recall", "arguments": {"query": "CR 2002/71", "expected": "CR 2002/71"}}),
    ("call_fetch_operator", "call",
     {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}, "token": OPERATOR_TOKEN}),
    ("call_fetch_oauth", "call",
     {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}, "token": "tok-oauth"}),
    # HTTP-level paths owned by the middleware or the transport
    ("auth_missing", "call", {"name": "fetch", "arguments": {"ref": "x"}, "token": None}),
    ("auth_invalid", "call", {"name": "fetch", "arguments": {"ref": "x"}, "token": "tok-bogus"}),
    ("auth_capped", "call", {"name": "fetch", "arguments": {"ref": "x"}, "token": "tok-capped"}),
    ("auth_no_account", "call", {"name": "fetch", "arguments": {"ref": "x"}, "token": "tok-noacct"}),
    ("http_delete", "http", {"http_method": "DELETE"}),
    ("http_bad_host", "rpc", {"method": "tools/list", "params": {}, "extra": {"Host": "evil.example"}}),
    ("http_bad_json", "raw", {"content": b"{not json"}),
    ("http_no_accept", "raw", {"content": b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
                               "headers": {"Accept": "text/html"}}),
    ("notification_initialized", "raw", {"content": b'{"jsonrpc":"2.0","method":"notifications/initialized"}'}),
    ("notification_initialized_header", "raw",
     {"content": b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
      "headers": {"MCP-Protocol-Version": "2025-06-18"}}),
    ("notification_cancelled", "raw",
     {"content": b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}'}),
    ("jsonrpc_batch", "raw",
     {"content": b'[{"jsonrpc":"2.0","id":1,"method":"ping"},{"jsonrpc":"2.0","id":2,"method":"ping"}]'}),
    ("jsonrpc_response_posted", "raw", {"content": b'{"jsonrpc":"2.0","id":1,"result":{}}'}),
    ("http_json_only_accept", "raw", {"content": b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
                                      "headers": {"Accept": "application/json"}}),
    ("http_accept_any", "raw", {"content": _LIST, "headers": {"Accept": "*/*"}}),
    # Bodies the 1.x json.loads parser accepted (2.x's pydantic_core parser does not).
    ("body_utf8_bom", "raw", {"content": b"\xef\xbb\xbf" + _LIST}),
    ("body_utf16", "raw", {"content": _LIST.decode().encode("utf-16")}),
    # A JS client that cuts a string inside an emoji writes the unpaired half as an escape.
    ("call_search_lone_surrogate", "raw",
     {"content": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search",'
                 b'"arguments":{"query":"capital gains \\ud83d","limit":1}}}'}),
    ("body_nested_300", "raw",
     {"content": b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"x":'
                 + b"[" * 300 + b"]" * 300 + b"}}}"}),
    # 1.x had no body-size limit (2.x defaults to 4 MiB). Built on demand: it is 4 MiB.
    ("body_over_4_mib", "raw",
     {"content": lambda: json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
         "name": "get_info", "arguments": {}, "pad": "x" * (4 * 1024 * 1024 + 10)}}).encode()}),
    # "_meta": {"progressToken": null}: a serialiser without omit-empty, on every method.
    *[(f"{n}_progress_token_null{'_header_modern' if pv else ''}", "rpc",
       {"method": m, "params": {**p, "_meta": {"progressToken": None}}, "pv": pv})
      for n, m, p in (("initialize", "initialize", {**_INIT, "protocolVersion": "2025-06-18"}),
                      ("tools_list", "tools/list", {}),
                      ("call_fetch", "tools/call", {"name": "fetch", "arguments": {"ref": "leg:itaa-1997:8-1"}}),
                      ("ping", "ping", {}))
      for pv in (None, MODERN)],
    ("initialize_protocol_version_int", "rpc",
     {"method": "initialize", "params": {**_INIT, "protocolVersion": 20250618}}),
    ("initialize_header_empty", "rpc",
     {"method": "initialize", "params": {**_INIT, "protocolVersion": "2025-06-18"},
      "extra": {"MCP-Protocol-Version": ""}}),
    ("tools_list_header_empty", "rpc", {"method": "tools/list", "params": {}, "extra": {"MCP-Protocol-Version": ""}}),
]

_KEEP_HEADERS = ("content-type", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                 "retry-after", "www-authenticate", "allow")


def run_case(client, kind: str, spec: dict):
    spec = dict(spec)
    if kind == "call":
        args = spec.pop("arguments")
        params = {"name": spec.pop("name"), "arguments": args}
        if spec.pop("meta", False):
            params["_meta"] = META
        return rpc(client, "tools/call", params, **spec)
    if kind == "rpc":
        return rpc(client, spec.pop("method"), spec.pop("params", None), **spec)
    if kind == "http":
        return rpc(client, "x", None, **spec)
    if kind == "raw":
        h = headers_for("tok-admin")
        h.update(spec.get("headers") or {})
        h = {k: v for k, v in h.items() if v is not None}  # None drops a default header
        content = spec["content"]
        return client.post("/mcp", headers=h, content=content() if callable(content) else content)
    raise ValueError(kind)


def snap(resp) -> dict:
    """The client-visible part of a response, normalised for comparison."""
    hdrs = {k: resp.headers[k] for k in _KEEP_HEADERS if k in resp.headers}
    if resp.status_code == 429 and hdrs.get("retry-after", "").isdigit() and hdrs["retry-after"] != "600":
        hdrs["retry-after"] = "<seconds to UTC midnight>"
    out = {"status": resp.status_code,
           "headers": hdrs,
           "session_id": "mcp-session-id" in resp.headers}
    try:
        out["json"] = resp.json()
    except Exception:
        out["text"] = resp.text
    return out


def _without_hidden_types(s: dict) -> dict:
    """Drop the content_types rows of hidden (private) types from a tool result.

    Hidden types live only in the private repo, and the admin caller sees their rows, so
    without this the golden would differ between this repo and the public export (where the
    types do not exist). Private-type behaviour on the wire is tested with the private type.
    """
    from content.registry import REGISTRY

    def is_hidden(key: str) -> bool:  # as content_access.is_hidden
        return bool(REGISTRY.get(key, {}).get("hidden"))
    try:
        item = s["json"]["result"]["content"][0]
        text = item["text"]
        doc = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError):
        return s
    rows = doc.get("content_types") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not any(is_hidden(str(r.get("key"))) for r in rows):
        return s
    assert json.dumps(doc, indent=2) == text, "tool text is not plain json.dumps(indent=2)"
    doc["content_types"] = [r for r in rows if not is_hidden(str(r.get("key")))]
    item["text"] = json.dumps(doc, indent=2)
    return s


def capture(mp: pytest.MonkeyPatch) -> dict:
    set_modern_era(mp, False)
    with live_client(mp) as (client, _events):
        return {name: _without_hidden_types(snap(run_case(client, kind, spec)))
                for name, kind, spec in CASES}


NL = chr(10)


def dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + NL


def main(argv: list[str]) -> int:
    with pytest.MonkeyPatch.context() as mp:
        got = capture(mp)
    doc = {"sdk": SDK_VERSION, "cases": got}
    if "--write" in argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(dump(doc), encoding="utf-8", newline=NL)
        TOOLS_SNAPSHOT.write_text(dump(got["tools_list"]["json"]["result"]["tools"]),
                                  encoding="utf-8", newline=NL)
        print(f"wrote {GOLDEN} ({len(got)} cases, mcp {SDK_VERSION}) and {TOOLS_SNAPSHOT}")
    else:
        out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
        if out:
            out.write_text(dump(doc), encoding="utf-8", newline=NL)
        else:
            sys.stdout.write(dump(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
