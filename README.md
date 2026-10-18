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

Needs Rust 1.88 or later ([rustup](https://rustup.rs)) and, for PDF sources
only, `pdftotext` from poppler. Everything else is built in: no database
server, no Node, no Python.

```sh
scripts/demo.sh        # build, load the Australian tax sources, serve on http://127.0.0.1:8080
```

That loads the Income Tax Assessment Acts 1997 and 1936, TR 2018/5 and a
summary of *Bendel* from `examples/au-tax` (real sources; licences in
`examples/au-tax/sources/SOURCES.md`). `scripts/demo.sh --fictional` loads the
small invented samples used to exercise the parsers instead.

Or step by step. Run these from the repository root: `meowlaw.toml` points at
`profiles/` and `ui/` relative to itself.

```sh
cargo install --path crates/meowlaw-cli --locked   # puts `meowlaw` on your PATH
meowlaw init                                       # writes meowlaw.toml, profiles/, data/
meowlaw sync examples/au-tax/sources.toml          # the Acts, the ruling and the case

meowlaw search "general deductions"
meowlaw search "s 8-1 ITAA 1997"        # citation resolution
meowlaw show itaa-1997:s8-1
meowlaw unresolved                      # data-quality report
meowlaw serve                           # http://127.0.0.1:8080 (UI, /api, /mcp)
```

Search mode (enriched or basic) is a server setting, `[search] mode` in
`meowlaw.toml`; the UI and MCP clients use it unless a call names one.

## Docker

The image builds the binary from source (`cargo build --release --locked`)
and runs `meowlaw serve` on `0.0.0.0:8080` with `poppler-utils` installed for
PDF ingestion. It is serve-only: `examples/` is not copied in, and the store
lives in a volume you mount.

Compose: `MEOWLAW_TOKEN=$(openssl rand -hex 32) docker compose up --build`
(see `compose.yaml`). By hand:

```sh
docker build -t meowlaw .
mkdir -p data          # create it yourself, so it is owned by you, not root
docker run --rm -p 8080:8080 \
  -e MEOWLAW_TOKEN=$(openssl rand -hex 32) \
  -v "$PWD/data:/srv/meowlaw/data" meowlaw
```

- The container runs as an unprivileged user with UID 1000. If your user has
  a different UID, add `--user "$(id -u)"` so the server can write to `data/`
  and the files it writes stay yours.
- `MEOWLAW_TOKEN` is optional. Without it `/mcp` and the API are open to
  anyone who can reach the port, so set it for anything but a laptop. The
  ingest page (`allow_ingest = true`) always needs the token on a non-loopback
  bind.
- Ingest on the host with the same `data/` directory (`meowlaw ingest ...`
  with `data_dir = "data"`), or inside the container against a mounted file:
  `docker exec <id> meowlaw ingest /srv/meowlaw/data/in/act.md --kind legislation`.
  A running server picks up new corpora without a restart.
- To change settings such as `allow_ingest` or `server.allowed_origins` (add
  the public origin when a reverse proxy fronts the UI, or browser requests
  get 403), mount your own config over the image's:
  `-v "$PWD/meowlaw.toml:/srv/meowlaw/meowlaw.toml:ro"`.
  A config copied from `meowlaw.example.toml` keeps `bind = "127.0.0.1:8080"`,
  which inside the container listens only on its own loopback: the container
  starts, the port mapping is there, and every request from the host fails to
  connect. Set `bind = "0.0.0.0:8080"` when you mount your own config.
- Static UI files are served with `Cache-Control: no-cache`, so a browser or a
  CDN in front revalidates them and an upgrade never mixes a new `index.html`
  with an old `app.js`. A proxy that caches `.js`/`.css` by extension should
  still honour that header or be purged when the UI ships.
- A `.dockerignore` keeps `target/` and `data/` out of the build context.

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
examples/          worked examples: fictional parser samples and real
                   Australian tax sources in examples/au-tax
```

## Content and licensing

MeowLaw ships **no legal content of its own**. The one worked example that
carries third-party material is `examples/au-tax`, whose sources keep their own
terms (see `examples/au-tax/sources/SOURCES.md`). Whoever runs an instance
decides what to ingest and is responsible for having the right to store and
republish it.
Record the licence and as-at date on each corpus (`licence`, `as_at`); both are
shown in the UI and returned by every MCP `fetch`.

## Licence

Free software under the [GNU Affero General Public License, version 3 or later](LICENSE.md)
(AGPL-3.0-or-later). Use it, modify it and self-host it, including inside a
business. If you run a modified version as a service for others, you must
publish your modified source under the same licence.

A commercial licence is available from the copyright holder for anyone who
needs to modify MeowLaw without the AGPL's source obligations, or to embed it
in a closed product. See [NOTICE](NOTICE) for contact.

Keep the `Required Notice` line in [NOTICE](NOTICE) with every copy.
