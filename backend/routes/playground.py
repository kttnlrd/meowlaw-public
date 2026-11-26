"""Playground API — invoke LawKitty MCP tools directly from the web UI.

Two endpoints:
  GET  /api/playground/tools   -> [{name, description, parameters}] for the arg form
  POST /api/playground         -> {tool, args} -> normalised tool result

Session-authed via `require_user` (router dependency in main.py). Runs the tool
function in-process — no MCP client or token needed.
"""
from __future__ import annotations

import inspect
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.services import usage_log

router = APIRouter()


def _tool_index() -> dict[str, Any]:
    """name -> FastMCP Tool (lazy import avoids import-order issues)."""
    from backend.fastmcp_server import mcp

    return {t.name: t for t in mcp._tool_manager._tools.values()}


class PlaygroundCall(BaseModel):
    tool: str
    args: dict[str, Any] = {}


def _normalize(result: Any) -> Any:
    """Return a JSON-serialisable value. Tools return JSON strings; parse them."""
    if result is None or isinstance(result, (dict, list, int, float, bool)):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    try:
        return json.loads(json.dumps(result, default=str))
    except Exception:
        return str(result)


@router.get("/api/playground/tools")
def list_tools(request: Request):
    tools = []
    for t in sorted(_tool_index().values(), key=lambda t: t.name):
        params = t.parameters or {}
        tools.append(
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "parameters": params,
            }
        )
    return JSONResponse({"tools": tools})


@router.post("/api/playground")
async def run_tool(request: Request, body: PlaygroundCall):
    idx = _tool_index()
    if body.tool not in idx:
        return JSONResponse(
            {"error": f"unknown tool: {body.tool}", "tools": sorted(idx)},
            status_code=404,
        )
    fn = idx[body.tool].fn
    # MCP content gating keys off usage_log.current; without this every playground call
    # would run as the unrestricted operator/stdio caller.
    user = getattr(request.state, "user", None) or {}
    tok = usage_log.current.set({"user_email": user.get("email") or "anonymous"})
    try:
        if inspect.iscoroutinefunction(fn):
            result = await fn(**body.args)
        else:
            result = fn(**body.args)
    except Exception as e:  # noqa: BLE001 — surface the tool's own error to the caller
        return JSONResponse(
            {"error": f"{type(e).__name__}: {e}", "tool": body.tool},
            status_code=400,
        )
    finally:
        usage_log.current.reset(tok)
    return JSONResponse({"tool": body.tool, "result": _normalize(result)})
