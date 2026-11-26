#!/usr/bin/env python3
"""
Legislation Explorer MCP Bridge — Stdio ↔ SSE
For Claude Desktop compatibility. No npx, no Node.js, no mcp-remote.
"""
import sys
import json
import argparse
import asyncio
import urllib.parse

from mcp import ClientSession
from mcp.client.sse import sse_client


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="MCP access token")
    parser.add_argument("--url", default="https://lawkitty.app")
    args = parser.parse_args()

    sse_url = f"{args.url}/mcp/sse?token={args.token}"

    async with sse_client(url=sse_url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Connected: {len(tools.tools)} tools available", file=sys.stderr)

            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue

                method = request.get("method")
                req_id = request.get("id")
                params = request.get("params", {})

                try:
                    if method == "tools/list":
                        result = await session.list_tools()
                        response = {
                            "jsonrpc": "2.0", "id": req_id,
                            "result": {
                                "tools": [
                                    {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                                    for t in result.tools
                                ]
                            },
                        }
                    elif method == "tools/call":
                        result = await session.call_tool(params["name"], params.get("arguments", {}))
                        content = []
                        for c in result.content:
                            if hasattr(c, "text") and c.text:
                                content.append({"type": "text", "text": c.text})
                        response = {
                            "jsonrpc": "2.0", "id": req_id,
                            "result": {"content": content, "isError": getattr(result, "isError", False)},
                        }
                    elif method == "resources/list":
                        result = await session.list_resources() if hasattr(session, "list_resources") else type('obj', (object,), {"resources": []})()
                        response = {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
                    elif method == "prompts/list":
                        result = await session.list_prompts() if hasattr(session, "list_prompts") else type('obj', (object,), {"prompts": []})()
                        response = {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
                    elif method in ("initialize",):
                        response = {
                            "jsonrpc": "2.0", "id": req_id,
                            "result": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                                "serverInfo": {"name": "legislation-explorer", "version": "2.4.0"},
                            },
                        }
                    elif method == "notifications/initialized":
                        continue
                    else:
                        response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
                except Exception as e:
                    response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}

                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())