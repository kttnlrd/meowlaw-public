# MeowLaw

Open-source engine for legal research over **any** body of law. Ingest
legislation, cases, court and conduct rules, treaties, regulator guidance,
textbooks and internal handbooks from any jurisdiction; MeowLaw builds the
structure, extracts cross-references and defined terms, links everything into
one graph, and serves it through a web UI, a REST API and an MCP server for AI
clients.

Written in Rust. One binary, one SQLite file, one search index. No country is
hard-coded: everything jurisdiction-specific lives in TOML
[profiles](docs/profiles.md).

```
             ┌──────────────┐   ┌───────────────┐   ┌──────────────┐
 md txt html │   ingest     │   │   extract     │   │   store      │
 xml docx pdf├─▶ blocks →    ├──▶│ references    ├──▶│ SQLite:      │
 json        │   tree       │   │ definitions   │   │ nodes, refs, │
             │ (per profile)│   │ term usage    │   │ defs, edges  │
             └──────────────┘   └───────────────┘   └──────┬───────┘
                                                           │
                       ┌───────────────────────────────────┼─────────────┐
                       ▼                                   ▼             ▼
               ┌──────────────┐                   ┌──────────────┐ ┌───────────┐
               │ search       │                   │ graph        │ │ vectors   │
               │ BM25 (basic) │◀── fusion (RRF) ──│ neighbourhood│ │ optional  │
               │ + enriched   │                   │ path, export │ │ embedder  │
               └──────┬───────┘                   └──────────────┘ └───────────┘
                      ▼
          ┌───────────────────────┐
          │ server: UI · REST · MCP│
          └───────────────────────┘
```

## Quick start

```sh
cargo build --release
cp meowlaw.example.toml meowlaw.toml        # or: ./target/release/meowlaw init

# Fictional sample sources (see examples/README.md)
meowlaw ingest examples/widget-act
meowlaw ingest examples/cases --kind case
meowlaw ingest examples/handbook/staff-handbook.md --kind secondary
meowlaw ingest examples/xml/model-code.xml --kind legislation --profile eu

meowlaw search "dealer must not sell"
meowlaw search "s 4 WA 2024"            # citation resolution
meowlaw show widget-act:s3
meowlaw unresolved                      # data-quality report
meowlaw serve                           # http://127.0.0.1:8080 (UI, /api, /mcp)
```

## What it does

| Capability | Where | Docs |
|---|---|---|
| Act / code / rule-set ingestion (Markdown, text, HTML, Akoma Ntoso, USLM, DOCX, PDF, JSON) | `meowlaw-ingest` | [ingestion](docs/ingestion.md) |
| Case ingestion (numbered paragraphs, citations from front matter or text) | `meowlaw-ingest` | [ingestion](docs/ingestion.md#cases) |
| General material (textbooks, handbooks, policies) chunked by heading | `meowlaw-ingest` | [ingestion](docs/ingestion.md#secondary-material) |
| Jurisdiction profiles: structure, citation and definition patterns | `profiles/*.toml` | [profiles](docs/profiles.md) |
| Reference extraction and resolution across corpora | `meowlaw-extract` | [extraction](docs/extraction.md) |
| Tables from every format, kept as pipe tables; "item 3 of the table in section X" links to the row | `meowlaw-ingest`, `meowlaw-extract` | [tables](docs/tables.md) |
| Definition extraction with scope, and defined-term usage links | `meowlaw-extract` | [extraction](docs/extraction.md#definitions) |
| Graph: neighbourhood, shortest path, GraphML/DOT/JSON export | `meowlaw-store` | [graph](docs/graph.md) |
| Basic (BM25) and enriched search | `meowlaw-search` | [search](docs/search.md) |
| Optional vector search via any OpenAI-compatible embeddings endpoint | `meowlaw-search` | [search](docs/search.md#vectors) |
| MCP server (stdio and Streamable HTTP) | `meowlaw-mcp` | [mcp](docs/mcp.md) |
| Per-corpus access control by token, enforced for API, UI and MCP | `meowlaw-mcp`, `meowlaw-server` | [access](docs/access.md) |
| Ingest page: upload, preview, check changes, ingest, remove | `meowlaw-server`, `ui/admin.html` | [ingestion](docs/ingestion.md#ingest-page) |
| Sync: fresh or update ingestion with change reports | `meowlaw-search` | [ingestion](docs/ingestion.md#updating-sources) |
| Source linter for PDF, DOCX, HTML, XML, text and Markdown, with safe fixes | `meowlaw-lint` | [lint](docs/lint.md) |
| Language versions and point-in-time compilations: linked provisions, current-version resolution, `as_at` search, compare view | `meowlaw-core`, `meowlaw-extract` | [versions](docs/versions.md) |
| Optional machine-generated summaries, labelled as such | `meowlaw-search` | [ingestion](docs/ingestion.md#machine-generated-summaries) |
| Web UI: source drawer (sources grouped by type, collapsible tree per source, filter), reader, related panel; config-driven logo, colours and fonts | `ui/`, `meowlaw-server` | [branding](docs/branding.md) |

What is still to build is in [ROADMAP.md](ROADMAP.md).

## Workspace

```
crates/
  meowlaw-core     data model, ID scheme, profile loader
  meowlaw-ingest   format readers and tree builders
  meowlaw-extract  citation parsing/resolution, definitions, term usage
  meowlaw-store    SQLite store and graph queries
  meowlaw-search   Tantivy BM25, enriched fusion, vectors
  meowlaw-mcp      MCP JSON-RPC handler, tools, stdio transport
  meowlaw-server   axum HTTP server, config, branding
  meowlaw-cli      the `meowlaw` binary
profiles/          built-in jurisdiction profiles
ui/                static web UI (no build step)
examples/          fictional sample sources
```

## Content and licensing

MeowLaw ships **no legal content**. Whoever runs an instance decides what to
ingest and is responsible for having the right to store and republish it.
Record the licence and as-at date on each corpus (`licence`, `as_at`); both are
shown in the UI and returned by every MCP `fetch`.

## Licence

Source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).
Free for personal use, research, study, and for charities, educational
institutions, public research bodies and government. Any commercial use,
including selling it, offering it as a paid service, or using it in the course
of a business, needs a separate licence: contact the copyright holder.

Keep the `Required Notice` line in [NOTICE](NOTICE) with every copy.
