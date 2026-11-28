# lawkitty MCP — Reference

## 1. What it is

A single Model Context Protocol server named **lawkitty** that exposes Australian/NZ tax + corporate-law content over the MCP **Streamable HTTP** transport. It is stateless (no per-session state) and returns plain JSON.

- **Endpoint:** `https://lawkitty.app/mcp`
- **Transport:** Streamable HTTP (`stateless_http=True`, `json_response=True`)
- **Tools:** 27
- **Auth:** bearer token (operator token, per-user MCP token, or OAuth access token)

## 2. Architecture

| File | Role |
|---|---|
| `backend/fastmcp_server.py` | The FastMCP app: 27 `@mcp.tool` functions, auth middleware, helpers |
| `backend/mcp_token_manager.py` | Token storage (Postgres), rate limiting, per-tier daily caps |
| `backend/routes/mcp.py` | REST API to create/list/revoke/rename tokens (the app's "MCP" panel) |
| `backend/main.py` | Mounts `fastmcp.streamable_http_app()` at `/mcp`, wraps it in `MCPAuthMiddleware` |

Mounting (`main.py`):

```
fastmcp_app = fastmcp.streamable_http_app()
_mcp_handler = MCPAuthMiddleware(fastmcp_app.routes[0].app)
# routed at /mcp only (single canonical path)
```

DNS-rebinding protection is on: only whitelisted hosts/origins (lawkitty.app, localhost; extra hosts via `MCP_EXTRA_ALLOWED_HOSTS` / `MCP_EXTRA_ALLOWED_ORIGINS`) may connect.

## 3. Auth flow (`MCPAuthMiddleware`)

Token is read from, in order:

1. `Authorization: Bearer <token>`
2. `X-API-Key: <token>`
3. `?token=`, `?v=`, `?_auth=`, `?x=` query params
4. Path segment `/mcp/<token>`
5. Body `params._auth` / `params.token`
6. Cookie `token`, or `X-Session-Id` header

Resolution:

1. `token_manager.owner_of(token)` → if found, owner's `users.tier` decides access.
2. Else fall back to static `MCP_AUTH_TOKEN` (operator, unlimited).
3. Else try OAuth access token.

Then `check_rate_limit()`:

- Tier not in `DAILY_CAPS` → **403** `upgrade_required`.
- Daily cap reached → **429**.
- Per-token rate limit (300 req/min) or global (3000/min) → **429**.

`DAILY_CAPS = {"trial": 500, "solo": 500, "power": None}` (free during beta).

## 4. Function export (27 tools)

### Search & discovery
- `search_legislation(query, act?, limit=20)` — keyword/section-number search, AND matching.
- `search_all(query, type_filter?, act?, limit=20)` — unified search across sections/cases/rulings/commentary.
- `search_cases(query, limit=20)` — case summaries + metadata by topic/name/citation.
- `find_similar_rulings(query, limit=10, outcome?, source?)` — semantic search over 57,608 private + 12k+ public rulings.
- `resolve_alias(reference)` — section alias/short-hand → act + section.
- `get_definition(act, term)` — term definition across acts.

### Acts & sections
- `list_acts()` — all acts + ATO rulings.
- `get_act_tree(act, depth="sections", part?, offset?)` — structure (parts/divisions/sections).
- `get_section(act, section, max_body_length=50000, include_commentary?)` — full section text + related cases/rulings/commentary.

### Treaties
- `list_treaty_articles(country)` — articles of a Double Tax Agreement.
- `get_treaty_article(country, article, max_body_length?)` — full article text.

### Cases
- `get_case(citation, search?, context?)` — metadata, AI summary, legislation refs, citations, judgment text.
- `case_legislation_refs(citation)` — legislation references + case citations for a case.

### Rulings & regulatory guides
- `list_rulings(type?, year?, limit?, offset?, counts_only?)` — ATO rulings grouped by year/type.
- `get_ruling(citation)` — public ruling (TR 2024/1, CR 2017/74, PR 2008/70).
- `get_private_ruling(authnum)` — single private ruling by authorisation number.
- `get_regulatory_guide(rg_number, max_body_length=8000)` — ASIC RG with structured summary.
- `get_rg_sections(rg_number)` — Corps Act sections cited by an RG.

### Graph
- `graph_neighbourhood(key, depth=1)` — graph context block for a node.
- `graph_path(from_key, to_key, max_hops=10)` — shortest path between nodes.

### Standards & quotes
- `standards(topic?)` — Firm standards for a topic, or topic list.
- `quote_info()` — all quotes + library style rules.
- `quote_fetch(keyword?, limit=10, offset=0)` — browse/search quotes.
- `quote_save(title, date, text, names?, tag?, cost?, currency?, terms?, alt?, anonymise?)` — save a quote (anonymised by default).

### Meta & issues
- `get_info()` — version, usage conventions, coverage counts, known issues. **Call first.**
- `report_issue(category, tool?, params?, expected?, actual?, note?)` — file a bug.
- `list_issues(status?, tool?, limit?)` — reported issues + status.

## 5. Standard responses

Every tool returns a JSON **string** (`json.dumps(..., indent=2)`), e.g.:

```json
{
  "query": "8-1",
  "total": 1,
  "results": [ ... ]
}
```

`get_info()` returns the canonical usage reference:

```json
{
  "name": "lawkitty",
  "version": "0.1 beta",
  "usage": { "act_ids": [...], "section_format": {...}, "citation_format": {...}, "query_format": {...} },
  "coverage": { "cases": 7375, "rulings": ..., "summaries": ... },
  "known_issues": [...]
}
```

Error responses (HTTP status at the transport level):

| Code | Meaning |
|---|---|
| 401 | `Missing token` |
| 403 | `Invalid or revoked token` / `upgrade_required` (tier has no MCP) |
| 429 | rate-limit or daily-cap message |
| 421 | request from a host/origin not in the allowlist |
