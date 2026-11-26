# LawKitty MCP — User Guide

LawKitty exposes Australian tax law as a **Model Context Protocol (MCP) server**: a
single search surface over legislation, case law, and public ATO rulings that any MCP
client (Claude Desktop, Cursor, a custom agent, etc.) can call. There are **11 tools**.

---

## 1. Getting started

### 1.1 Connect

The MCP endpoint is **Streamable HTTP**:

```
https://lawkitty.app/mcp
```

Everything below assumes that URL. `/mcp` is the only MCP path (a `/{path}` catch-all
handles sub-paths like `/mcp/sse`). The old WAF-bypass aliases are removed.

### 1.2 Authenticate

lawkitty authenticates through the token in the URL — there is no API key or OAuth flow to
fill in.

1. Open `https://lawkitty.app`, sign in with a **magic link** (email).
2. Create a token — **Account → MCP connector → Generate token** (or `POST /api/mcp-token`
   with `{"name": "claude-desktop"}`).
3. Send the token to the MCP server one of these ways (all equivalent):
   - `Authorization: Bearer <token>`
   - `X-API-Key: <token>`
   - `?token=<token>` query param
   - `https://lawkitty.app/mcp/<token>` path segment

Leave any OAuth fields blank in your client. The token in the URL is all the authentication
you need.

### 1.3 First call

Call `get_info` before anything else. It returns the current server version, citation
conventions, and coverage counts. Call it **again** whenever a tool fails or returns an
empty result — the failure is usually a query-format or citation-format problem documented
there.

---

## 2. Tool reference

### `get_info`
Server version, usage conventions, tool descriptions, coverage counts. **Call first, and
re-call on any failure.**

### `search(query, types, act, status, limit, cursor)`
Hybrid keyword search across the corpus. Returns document IDs — then `fetch` to read.

- `types` is a subset of `legislation | case | ruling | rg | afsa | treaty | private_ruling`
  (default `legislation, case, ruling`).
- `private_ruling` uses **semantic fact-pattern matching** (not keywords); the rest are
  keyword/FTS.
- `act` scopes legislation results to one act slug (e.g. `itaa-1997`).

### `fetch(ref, chunk)`
Return one document by ID or human citation. Large documents are split into ~8,000-char
chunks; the response carries `chunk`, `total_chunks`, `next_chunk` — pass `chunk` to page.

Ref forms: `leg:itaa-1997:8-1`, `s 8-1 ITAA 1997`, `TR 2024/1`, `[2024] HCA 1`,
`rg:1`, `rg:103:5.15` (single paragraph), `dta:usa:7`, `afsa:<slug>`, `pbr:1051234567890`.

### `outline(id, depth, cursor)`
Structure of a collection or document.

- `id=None` → lists collections (acts + treaty countries).
- `id=<act-slug>` → the act tree (`depth`: `parts | divisions | sections`).
- `id=<country>` → treaty articles.

### `resolve_alias(reference)`
Resolve a shorthand to act + section. Handles `s 100A`, `Div 7A`, `Part IVA`, `Subdiv 115-C`,
`8-1`, `Div 152`, bare section numbers (`109Y`), etc.

### `get_definition(act, term)`
Look up a term definition, searched across all acts. The requested act is preferred; matches
in other acts come back under `also_defined_in`; cross-reference definitions carry `cross_reference`.

### `list_rulings(type, year, limit, offset, counts_only)`
All ATO rulings grouped by year and type, with ATO.gov.au and AustLII links.

### `related(id, direction, types, limit, cursor)`
Deterministic citation links for one document, one consistent schema for every type:

```
{id, type, direction, limit,
 cites:    [{id, type, citation, title}],   # forward citations
 cited_by: [{id, type, citation, title}],   # reverse citations
 cites_total, cited_by_total, next_cursor, truncated}
```

`id` is a document ID (`case:<year>-<court>-<n>`, `rg:<n>`, `afsa:<slug>`, `leg:<act>:<section>`).
`cited_by` for legislation comes from the graph; the case reverse index is a deferred build
and returns `[]` for now.

### `graph_neighbourhood(key, depth)`
The compact graph context for one node — per-edge-type counts + top exemplars — without
fetching full text.

- `key`: canonical graph key — `section:itaa-1997:118-110`, `public_ruling:TR 2025/1`,
  `case:[2015] HCA 48`, `private_ruling:EV/1011261243735`, `commentary:MTG:ch12`. Accepts the
  `resolved_by` graph key from `resolve_alias`.
- `depth`: `1` = node neighbourhood (~80 tokens); `2` = + aggregated neighbourhood-of-neighbourhood.

### `graph_path(from_key, to_key, max_hops)`
Shortest path between two graph nodes — "how does this connect to that?" (e.g. the chain of
rulings/cases linking a private ruling to a High Court case). Returns ordered node list + typed edges.

### `report_issue(category, tool, params, expected, actual, note)`
Report a bug or bad output. Categories: `bad_data`, `missing_content`, `stale_compilation`,
`wrong_result`, `tool_error`, `suggestion`, `bug`. Duplicates are auto-detected and return the
existing ticket.

---

## 3. Ways of working with LawKitty data

### 3.1 Save fetched documents locally for research files

The corpus is your source of truth; a research file is your working copy. The loop:

1. **Find the ID** — `search` for the topic, note the document IDs it returns.
2. **Read it** — `fetch` the document; page with `chunk` until `next_chunk` is null.
3. **Save it** — write each document to a local markdown/JSON file keyed by citation, so you
   can cite it and diff it against future changes.

```
research/
  8-1-general-deductions.md      # fetch leg:itaa-1997:8-1, full text
  tr-2004-4.md                   # fetch TR 2004/4
  [2024] HCA 1.md                # fetch [2024] HCA 1
```

Always record the **canonical ID** (e.g. `leg:itaa-1997:8-1`) at the top of the file — that's
the stable handle for `fetch`, `related`, and `graph_neighbourhood` later. The citation alone
isn't enough; the ID is what the other tools consume.

### 3.2 Save the graph context for finding related content

The graph is the fastest way to see what a provision is connected to. Pull the connection
data once and keep it as a local index, so related-content lookup is instant and repeatable:

1. **Neighbourhood** — `graph_neighbourhood(key, depth=2)` returns a compact block like
   `INTERPRETED_BY: 260 … APPLIES: 10329 private rulings`. Save that block alongside the
   document.
2. **Deterministic citations** — `related(id)` gives the forward (`cites`) and reverse
   (`cited_by`) links with fetchable IDs. Save both lists.
3. **Connection chains** — `graph_path(from_key, to_key)` gives the ruling→case→legislation
   chain linking two documents. Save the path when a specific connection matters.

A saved neighbourhood + `related` pair is a local "graph vector" for that node — you can
navigate related content offline and re-derive the map without re-querying the server.

```
research/
  8-1-general-deductions.md        # the fetched text (3.1)
  8-1.neighbourhood.md             # graph_neighbourhood output
  8-1.citations.json               # related() cites + cited_by
```

**Suggested order for a research task:**

1. `get_info` — confirm coverage + conventions.
2. `resolve_alias` — turn a shorthand ("s 8-1") into a canonical act+section.
3. `search` — surface candidate documents.
4. `fetch` — read the ones that matter; page through chunks.
5. `related` + `graph_neighbourhood` — map citations and connections; save them locally.
6. `graph_path` — trace a specific "how does A reach B" chain when you need it.
7. `get_definition` — pull any term definitions the provision relies on.
8. `report_issue` — file anything that looks wrong so the corpus gets fixed.

---

## 4. Conventions

- **Canonical IDs win over citations.** `fetch`, `related`, and the graph tools consume IDs
  (`leg:itaa-1997:8-1`, `case:[2024] HCA 1`). Keep the ID with any saved text.
- **Search first, fetch second.** `search` returns IDs cheaply; `fetch` is the expensive read.
- **Page large documents** — don't drop chunks; a truncated read is a wrong read.
- **`get_info` on failure** — most "empty result" surprises are a query-format issue it documents.
