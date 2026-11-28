"""Wire-level regression gate for the mcp SDK upgrade (docs/plans/mcp-2x-upgrade.md, section 4).

Every case in mcp_harness.CASES is replayed through the real app with MCP_MODERN_ERA off and
compared with the snapshot captured from mcp 1.26 (fixtures/mcp_golden.json). The only
differences allowed are the _SDK2_* tables and expected() below, each one reviewed and accepted.
"""
import copy
import json

import pytest

from backend.tests.mcp_harness import (
    CASES, GOLDEN, SDK_MAJOR, SDK_VERSION, TOOLS_SNAPSHOT, capture,
)

_GOLDEN = json.loads(GOLDEN.read_text(encoding="utf-8"))
_GOLDEN_MAJOR = int(_GOLDEN["sdk"].split(".")[0])


@pytest.fixture(scope="module")
def current() -> dict:
    with pytest.MonkeyPatch.context() as mp:
        return capture(mp)


def _server_version() -> str:
    """serverInfo.version: 1.x reported the SDK version; on 2.x we report the app VERSION."""
    if SDK_MAJOR < 2:
        return SDK_VERSION
    from backend.routes.api import VERSION
    return VERSION


# What mcp 2.x changes outside every tool result, in JSON-RPC errors and the initialize
# capabilities. Reviewed and accepted with the migration (none is sent to a client that is
# doing anything but probing or sending a malformed request); everything else must match 1.26.
_SDK2_JSON = {
    # JSON-RPC "method not found" is -32601 (1.x answered -32602 "Invalid request parameters").
    "unknown_method": {"error": {"code": -32601, "message": "Method not found", "data": "nope/nothing"}},
    "logging_set_level": {"error": {"data": "logging/setLevel"}},
    "resources_read_unknown": {"error": {"code": -32602, "data": {"uri": "file:///nope"}}},
    # Transport-level errors carry id null (1.x: "server-error"); the parse-error text changed.
    "http_bad_json": {"id": None, "error": {"message": "Parse error: key must be a string at line 1 column 2"}},
    "http_delete": {"id": None},
    "http_no_accept": {"id": None},
    "jsonrpc_batch": {"id": None},
}
# Compared as present-but-unequal-text. jsonrpc_batch: pydantic's union-validation dump differs in
# wording. call_search_lone_surrogate: a UTF-8 body cannot carry the unpaired surrogate 1.26 passed
# to the tool, so the middleware forwards it as U+FFFD; the tool echoes that in "query", and its
# search now finds hits (with the lone surrogate the tool's own search failed and returned none).
# test_mcp_compat_1x.py checks that text.
_SDK2_MASKED = {"jsonrpc_batch": ("error", "message"),
                "call_search_lone_surrogate": ("result", "content", 0, "text"),
                "body_nested_300": ("error", "message")}
# Requests 1.26 refused and 2.x serves, so the answer is that of the named plain request. An empty
# MCP-Protocol-Version header is down-negotiated like any unknown one (1.26 skipped the rewrite
# for it by accident and answered 400 -32600 to all but initialize); 2.x's Accept check takes */*.
_SDK2_SERVED = {"tools_list_header_empty": "tools_list", "http_accept_any": "tools_list"}
# JSON nested deeper than pydantic_core's recursion limit: 1.26 answered 200 -32602, 2.x refuses
# it at the transport. An error either way.
_SDK2_REPLACED = {
    "body_nested_300": {"status": 400, "headers": {"content-type": "application/json"}, "session_id": False,
                        "json": {"jsonrpc": "2.0", "id": None, "error": {
                            "code": -32700, "message": "Parse error: recursion limit exceeded at <position>"}}},
}


def _merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def _mask(name: str, snap: dict) -> dict:
    snap = copy.deepcopy(snap)
    if SDK_MAJOR >= 2 and name in _SDK2_MASKED:
        node = snap["json"]
        *path, leaf = _SDK2_MASKED[name]
        for k in path:
            node = node[k]
        assert isinstance(node.get(leaf), str) and node[leaf]
        node[leaf] = "<masked>"
    return snap


def expected(name: str, golden: dict) -> dict:
    """The golden response as the current SDK must send it."""
    if SDK_MAJOR >= 2 and _GOLDEN_MAJOR < 2:
        if name in _SDK2_REPLACED:
            return copy.deepcopy(_SDK2_REPLACED[name])
        if name in _SDK2_SERVED:
            ref = _SDK2_SERVED[name]
            return expected(ref, _GOLDEN["cases"][ref])
    exp = copy.deepcopy(golden)
    result = (exp.get("json") or {}).get("result") or {}
    if "serverInfo" in result:
        result["serverInfo"]["version"] = _server_version()
    if SDK_MAJOR < 2 or _GOLDEN_MAJOR >= 2:
        return exp
    if result.get("capabilities", {}).get("experimental") == {}:
        del result["capabilities"]["experimental"]  # 2.x omits the empty object
    if name.startswith("crash_"):
        # Owner decision: an uncaught (non-ToolError) exception reaches the client as the 2.x
        # generic message; the traceback stays in the server log.
        tool = dict(CASES_BY_NAME[name][2])["name"]
        result["content"][0]["text"] = f"Error executing tool {tool}"
    if name in _SDK2_JSON:
        _merge(exp["json"], _SDK2_JSON[name])
    return exp


CASES_BY_NAME = {c[0]: c for c in CASES}


def test_golden_covers_every_case():
    assert sorted(_GOLDEN["cases"]) == sorted(CASES_BY_NAME)


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_wire_matches_golden(current, name):
    got, want = _mask(name, current[name]), _mask(name, expected(name, _GOLDEN["cases"][name]))
    if got != want:
        pytest.fail(f"{name} differs from the mcp {_GOLDEN['sdk']} golden:\n"
                    f"want {json.dumps(want, sort_keys=True)[:3000]}\n"
                    f"got  {json.dumps(got, sort_keys=True)[:3000]}")


def test_tools_list_matches_snapshot(current):
    """Every tool's name, description and inputSchema, field by field."""
    want = {t["name"]: t for t in json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))}
    got = {t["name"]: t for t in current["tools_list"]["json"]["result"]["tools"]}
    assert sorted(got) == sorted(want)
    assert len(got) == 14
    diffs = [f"{n}.{field}" for n in want for field in sorted(set(want[n]) | set(got[n]))
             if json.dumps(want[n].get(field), sort_keys=True) != json.dumps(got[n].get(field), sort_keys=True)]
    assert not diffs, f"tool definitions changed: {diffs}"


def test_serverinfo_version(current):
    for v in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
        info = current[f"initialize_{v}"]["json"]["result"]["serverInfo"]
        assert info == {"name": "lawkitty", "version": _server_version()}
