# LawKitty (MeowLaw)

LawKitty is a reader, search engine and MCP server for Australian and New Zealand tax and corporate law. The hosted service is at https://lawkitty.app.

It covers:

- legislation (Commonwealth, New Zealand and selected State tax Acts), read section by section
- ATO public rulings and private rulings, and New Zealand IRD rulings
- cases
- ASIC regulatory guides and AFSA practice guidance
- tax treaties
- optional licensed commentary (CCH commentary, Keays insolvency, NZ Master Tax Guide), which is off by default and visible only to administrators and users they grant access to

This repository is the application source. It is published for transparency and so that others can self-host it. **The corpus is not included.** There is no legislation, ruling, case or commentary text in this repository, no search index and no embeddings. See [SETUP.md](SETUP.md) for how the app locates data and what you need to supply.

## Features

- Hybrid search: SQLite FTS5 keyword search combined with vector search over embeddings, with exact section references (for example `26AH`) ranked first. An optional Rust service, `searchd`, replaces the in-process vector scan with an HNSW index.
- Section reader with tree navigation, definitions and display rules per content type (`content/*/type.yaml`).
- Citation graph and related documents: legislation links to the cases, rulings and commentary that cite or interpret it.
- MCP server (Streamable HTTP at `/mcp`) so Claude and other MCP clients can search, fetch, outline and follow citations. See [MCP_REFERENCE.md](MCP_REFERENCE.md) and [docs/mcp-guide.md](docs/mcp-guide.md).
- Sign-in with Google, Microsoft or an emailed magic link. Google and Microsoft are offered only when their client credentials are configured. See [docs/auth-sso.md](docs/auth-sso.md).
- Per-content-type access control. Each content type declares `auth: user` or `auth: admin` in its `type.yaml`. An `auth: admin` type is visible only to admins (listed in `ADMIN_EMAILS`) and to users an admin has granted that type. Admins can also switch any type off globally at runtime. See [docs/content-type-toggles.md](docs/content-type-toggles.md).

## Content types

Each type is declared in `content/<type>/type.yaml`.

| Key | Content | Jurisdiction | Access |
|---|---|---|---|
| `legislation` | Acts, as section trees | AU | user |
| `case` | Cases | AU | user |
| `ruling` | ATO public rulings (TR, TD, GSTR and others) | AU | user |
| `ato-private-ruling` | ATO private rulings | AU | user |
| `regulatory-guide` | ASIC regulatory guides | AU | user |
| `afsa-guide` | AFSA practice guidance | AU | user |
| `treaty` | Tax treaties | AU | user |
| `nz-ruling` | IRD Tax Technical rulings | NZ | user |
| `commentary` | CCH commentary | AU | admin |
| `insolvency` | Keays insolvency | AU | admin |
| `nz-mtg` | NZ Master Tax Guide | NZ | admin |

"user" means every signed-in user. "admin" means admins and users an admin has granted the type. The three `admin` types are also marked `default_configured: false`, so the committed `data_sources.yaml` never points at them.

## How it fits together

- A single FastAPI process (`backend.main:app`) serves the JSON API under `/api`, sign-in under `/auth`, the MCP endpoint at `/mcp`, the React app under `/app/` and the static site at `/`.
- Content is read from files on disk through the loaders in `content/`. Search uses a SQLite FTS5 index (`search_index.db`) built from that data, plus a vector matrix built from `data/embeddings.db`.
- PostgreSQL holds user data only: accounts, magic-link tokens, preferences, MCP tokens and their call log, and the content-type switches and grants. The schema is created on startup.
- Bind the app to `127.0.0.1` and put a reverse proxy or tunnel in front of it. `searchd`, if used, must also stay on loopback.

## Repository layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI application: routes, services, auth, the MCP server (`fastmcp_server.py`), Postgres access (`db.py`), tests (`backend/tests/`) |
| `frontend/` | React and TypeScript single-page app (Vite), served under `/app/` |
| `site/` | Static pages served at `/` (landing page, login, admin, privacy) |
| `content/` | Content-type registry: one `type.yaml` and loader per content type |
| `pipeline/` | Ingestion and parsing scripts that turn source material into the data layout the app reads |
| `scripts/` | Index builders, validation, audit and maintenance scripts. Host-specific deploy and corpus-push scripts are not part of the public release |
| `searchd/` | Optional Rust hybrid retrieval service (tantivy BM25 and usearch HNSW). See `searchd/README.md` |
| `tests/` | Additional tests and fixtures at the repository root |
| `data_sources.yaml` | Manifest of where each content type's data lives |
| `.env.example` | Environment variable template |

## Quick start

[SETUP.md](SETUP.md) is a step-by-step self-hosting guide: requirements, configuration, database, running locally, sign-in, MCP and a generic production layout.

In outline, you need Python, Node, PostgreSQL, a corpus laid out as `data_sources.yaml` describes, and an embeddings key. The corpus is the part you must supply yourself.

## Documentation

- [SETUP.md](SETUP.md): self-hosting guide
- [MCP_REFERENCE.md](MCP_REFERENCE.md): MCP server reference
- [docs/mcp-guide.md](docs/mcp-guide.md): MCP user guide (connecting Claude and other clients)
- [docs/auth-sso.md](docs/auth-sso.md): sign-in setup and verification rules
- [docs/content-type-toggles.md](docs/content-type-toggles.md): content-type switches and per-user grants
- [CONTRIBUTING.md](CONTRIBUTING.md): contribution conventions
- [CHANGELOG.md](CHANGELOG.md): changes

Some of these documents were written for the hosted deployment and describe it (for example `lawkitty.app` URLs and the operator's workflow). Where a document and the code disagree, the code is authoritative.

## Licence

Copyright (c) 2026 Harrison Dell. Licensed under the GNU Affero General Public License, version 3 or (at your option) any later version: see [LICENSE.md](LICENSE.md) and [NOTICE](NOTICE). If you run a modified version as a network service, the AGPL requires you to offer its users the corresponding source. A commercial licence without that obligation is available from the copyright holder.

The corpus that the hosted service serves (legislation, rulings, cases and licensed commentary) comes from third-party sources with their own terms and is not covered by anything in this repository.
