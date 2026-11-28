#!/usr/bin/env bash
# Setup Legislation Explorer MCP for Claude Desktop
# Usage: curl -sL https://lawkitty.app/mcp-setup.sh | bash -s -- YOUR_TOKEN
set -euo pipefail

TOKEN="${1:-}"
BRIDGE_URL="https://lawkitty.app/static/mcp-bridge.py"
SCRIPT_NAME="legislation-mcp-bridge.py"

if [ -z "$TOKEN" ]; then
    echo "Usage: curl -sL https://lawkitty.app/mcp-setup.sh | bash -s -- YOUR_TOKEN"
    echo ""
    echo "Get your token at: https://lawkitty.app/settings"
    exit 1
fi

echo "=== Legislation Explorer MCP — Setup ==="

# Detect OS
OS="$(uname -s)"
case "$OS" in
    Darwin) CONFIG_DIR="$HOME/Library/Application Support/Claude" ;;
    Linux)  CONFIG_DIR="$HOME/.config/Claude" ;;
    MINGW*|MSYS*) CONFIG_DIR="$APPDATA/Claude" ;;
    *)
        echo "Unsupported OS: $OS"
        echo "Supported: macOS, Linux, Windows"
        exit 1
        ;;
esac
CONFIG_FILE="$CONFIG_DIR/claude_desktop_config.json"

# 1. Check Python
echo "Checking Python..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found. Install it from https://python.org"
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  Python $PY_VER found"

# 2. Install mcp package
echo "Installing MCP SDK..."
python3 -m pip install mcp -q 2>&1 | tail -1 || python3 -m pip install mcp --user -q 2>&1 | tail -1
echo "  MCP SDK installed"

# 3. Download bridge script
echo "Downloading bridge..."
BRIDGE_PATH="$HOME/.local/bin/$SCRIPT_NAME"
mkdir -p "$HOME/.local/bin"
curl -sL -o "$BRIDGE_PATH" "$BRIDGE_URL" 2>/dev/null || {
    # Fallback: embed the script inline
    cat > "$BRIDGE_PATH" << 'BRIDGE_EOF'
#!/usr/bin/env python3
"""Legislation Explorer MCP Bridge — Stdio ↔ SSE"""
import sys, json, argparse, asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--url", default="https://lawkitty.app")
    args = parser.parse_args()
    sse_url = f"{args.url}/mcp/sse?token={args.token}"
    async with sse_client(url=sse_url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Connected: {len(tools.tools)} tools", file=sys.stderr)
            while True:
                line = sys.stdin.readline()
                if not line: break
                line = line.strip()
                if not line: continue
                try: request = json.loads(line)
                except json.JSONDecodeError: continue
                method, req_id, params = request.get("method"), request.get("id"), request.get("params", {})
                try:
                    if method == "tools/list":
                        r = await session.list_tools()
                        response = {"jsonrpc":"2.0","id":req_id,"result":{"tools":[{"name":t.name,"description":t.description,"inputSchema":t.inputSchema} for t in r.tools]}}
                    elif method == "tools/call":
                        r = await session.call_tool(params["name"], params.get("arguments",{}))
                        content = []
                        for c in r.content:
                            if hasattr(c, "text") and c.text: content.append({"type":"text","text":c.text})
                        response = {"jsonrpc":"2.0","id":req_id,"result":{"content":content,"isError":getattr(r,"isError",False)}}
                    elif method in ("initialize",):
                        response = {"jsonrpc":"2.0","id":req_id,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{},"resources":{},"prompts":{}},"serverInfo":{"name":"legislation-explorer","version":"2.4.0"}}}
                    elif method == "notifications/initialized": continue
                    else: response = {"jsonrpc":"2.0","id":req_id,"error":{"code":-32601,"message":f"Method not found: {method}"}}
                except Exception as e: response = {"jsonrpc":"2.0","id":req_id,"error":{"code":-32603,"message":str(e)}}
                sys.stdout.write(json.dumps(response)+"\n"); sys.stdout.flush()
asyncio.run(main())
BRIDGE_EOF
}
chmod +x "$BRIDGE_PATH"
echo "  Bridge installed at $BRIDGE_PATH"

# 4. Write/update Claude Desktop config
echo "Configuring Claude Desktop..."
mkdir -p "$CONFIG_DIR"

# If config exists, merge; otherwise create
if [ -f "$CONFIG_FILE" ]; then
    python3 -c "
import json
with open('$CONFIG_FILE') as f: cfg = json.load(f)
if 'mcpServers' not in cfg: cfg['mcpServers'] = {}
cfg['mcpServers']['legislation-explorer'] = {
    'command': 'python3',
    'args': ['$BRIDGE_PATH', '--token', '$TOKEN'],
}
with open('$CONFIG_FILE', 'w') as f: json.dump(cfg, f, indent=2)
print('  Config merged')
" 2>&1
else
    cat > "$CONFIG_FILE" << CONFIG_EOF
{
  "mcpServers": {
    "legislation-explorer": {
      "command": "python3",
      "args": ["$BRIDGE_PATH", "--token", "$TOKEN"]
    }
  }
}
CONFIG_EOF
    echo "  Config created"
fi

echo ""
echo "=== Setup complete! ==="
echo "1. Restart Claude Desktop"
echo "2. Check Settings → Developer → legislation-explorer should show ✓ Connected"
echo "3. Ask Claude: 'Search legislation for section 8-1'"
echo ""
echo "To remove: claude mcp remove legislation-explorer"