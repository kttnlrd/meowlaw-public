"""server/discover probes get a JSON-RPC -32601 from the middleware, not the SDK."""
import asyncio
import json

from starlette.requests import Request

from backend.fastmcp_server import MCPAuthMiddleware


def _request(body: dict) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": []}, receive)


def test_server_discover_answers_method_not_found_without_calling_the_sdk():
    mw = MCPAuthMiddleware(app=None)
    called = []

    async def call_next(_req):
        called.append(True)

    ev: dict = {}
    req = _request({"jsonrpc": "2.0", "id": 7, "method": "server/discover", "params": {}})
    resp = asyncio.run(mw._forward(req, call_next, ev, "server/discover"))

    assert not called
    assert resp.status_code == 200 and resp.media_type == "application/json"
    assert json.loads(resp.body) == {"jsonrpc": "2.0", "id": 7,
                                     "error": {"code": -32601, "message": "Method not found: server/discover"}}
    assert ev["status"] == "ok"
