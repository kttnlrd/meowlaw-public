# MCP

MeowLaw speaks the Model Context Protocol over **stdio** (desktop and CLI
clients) and **Streamable HTTP** at `POST /mcp` (stateless, JSON responses).
Protocol revisions: 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05.

## Tools

| Tool | Purpose |
|---|---|
| `get_info` | Version, corpora with counts, ID grammar. Call first. |
| `list_corpora` | Filter by kind or jurisdiction. |
| `search` | Enriched or basic search with filters; results explain `matched_by`. |
| `fetch` | One node by ID or by citation text: text, path, children, references, definitions in scope, inbound citation count, corpus licence and as-at date. |
| `outline` | Table of contents under a corpus or node. |
| `resolve` | Resolve every citation in a passage. |
| `get_definition` | Definitions of a term with source, scope and pointer. |
| `related` | Cites / cited by / defines / uses terms / used by. |
| `graph_neighbourhood` | N-hop neighbourhood. |
| `graph_path` | Shortest chain between two nodes. |
| `unresolved_references` | Grouped dangling citations (quality report). |

All tools are read-only and annotated `readOnlyHint`. Results are returned as
`structuredContent` and as pretty-printed JSON text.

## Local (stdio)

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "meowlaw": {
      "command": "/path/to/meowlaw",
      "args": ["mcp", "--config", "/path/to/meowlaw.toml"]
    }
  }
}
```

Claude Code:

```sh
claude mcp add meowlaw -- /path/to/meowlaw mcp --config /path/to/meowlaw.toml
```

Logs go to stderr; stdout carries only JSON-RPC.

## Remote (HTTP)

```sh
export MEOWLAW_TOKEN=$(openssl rand -hex 32)
meowlaw serve --bind 0.0.0.0:8080        # put TLS in front (Caddy, nginx, a tunnel)
```

Clients send `Authorization: Bearer $MEOWLAW_TOKEN`.

```sh
claude mcp add --transport http meowlaw https://law.example.com/mcp \
  --header "Authorization: Bearer $MEOWLAW_TOKEN"
```

Security defaults:

- Without `MEOWLAW_TOKEN` the endpoint is open; the server warns if it is also
  bound to a non-loopback address.
- Browser requests must come from an origin in `server.allowed_origins`
  (DNS-rebinding protection). Requests without an `Origin` header are allowed.
- Token comparison is constant-time.
- Set `server.protect_api = true` to require the token for `/api/*` too.

Per-user tokens, OAuth 2.1 and rate limits are on the roadmap.

## Smoke test

```sh
curl -s localhost:8080/mcp -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $MEOWLAW_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_info","arguments":{}}}'
```
