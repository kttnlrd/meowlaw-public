# Self-hosting LawKitty

This guide takes you from a fresh clone to a running instance. Every statement here is taken from the code. Where the code does not settle something, the guide says so rather than guessing (see "Open points" at the end).

**The corpus is not distributed.** This repository contains no legislation, rulings, cases, commentary, search index or embeddings. You must supply your own data in the layout described in section 4, or the app will start with empty content types. How the hosted service obtained and prepared its corpus is not documented in this repository.

## 1. Requirements

| Requirement | Notes |
|---|---|
| Python | 3.12. This is the version the project's own virtual environment uses. The pinned packages (for example `numpy==2.4.2`) need a recent Python; other versions are untested. |
| Node.js and npm | For the frontend build (Vite 5, React 18, TypeScript 5). No Node version is pinned in `frontend/package.json`; use a current LTS release. |
| PostgreSQL | Stores user data only: users, magic-link tokens, display preferences, MCP tokens and their call log, an issue log, and the content-type switches and per-user grants. It does not store the corpus. Tables are created by the app on startup (section 5). |
| Corpus | Files on disk under the paths in `data_sources.yaml` (section 4). Not supplied. |
| Embeddings API key | The app builds an OpenAI client at import time, so `OPENAI_API_KEY` or `EMBED_API_KEY` must be set to some non-empty value or the process will not start. Semantic search calls an OpenAI-compatible embeddings endpoint (model `text-embedding-3-small`, 1536 dimensions). Stored vectors were made with that model, so a different model will not work against a corpus embedded with it. |
| Rust toolchain | Optional. Only needed to build `searchd`, the faster vector and keyword retrieval service. Without it the app uses its in-process search. |

## 2. Install

```bash
git clone https://github.com/kttnlrd/meowlaw-public.git lawkitty
cd lawkitty

python3.12 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt

cd frontend
npm ci
npm run build        # runs tsc then vite build; output in frontend/dist
cd ..
```

The FastAPI app serves the built frontend from `frontend/dist` under `/app/` if that directory exists. For frontend development use `npm run dev` instead (section 6).

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env`. **The backend does not load `.env` itself** (nothing in `backend/` calls `load_dotenv`). Load it in one of these ways:

- `uvicorn ... --env-file .env` (uvicorn reads it using `python-dotenv`, which is in the requirements)
- `set -a; . ./.env; set +a` before starting
- `EnvironmentFile=` in a systemd unit (section 9)

Use plain `KEY=value` lines, with no quotes or trailing comments, so that all three methods agree.

Never commit `.env`. All values below are placeholders; generate your own secrets (for example `openssl rand -hex 32`).

### Core

| Variable | Default | Purpose |
|---|---|---|
| `APP_BASE_URL` | `https://lawkitty.app` in most code paths; empty when deciding "production" | Public base URL of your instance, no trailing slash. It sets magic-link and SSO redirect hosts, and it decides whether the instance counts as production: production means an `https://` URL whose host is not localhost. **Set this explicitly.** For local use set `http://localhost:8765`. |
| `SESSION_SECRET` | none | Signs session cookies and consent state. At least 32 characters. If it is missing or shorter and the instance is production, the app refuses to start. Otherwise it uses a random per-process secret and sessions do not survive a restart. |
| `ALLOWED_ORIGINS` | `https://lawkitty.app,https://www.lawkitty.app,http://localhost:5173,http://localhost:3000` | Comma-separated CORS origins. Localhost origins are dropped unless `APP_BASE_URL` is itself a localhost URL. Set this to your own origin; the defaults name the hosted service. |
| `DEV_MODE` | unset | `true`, `1` or `yes` disables sign-in (`AUTH_DISABLED`): the web data API and the MCP endpoint are usable with no session, as a synthetic admin-level identity. **It is ignored when the instance is production**, so it cannot be left on by accident. Only for local development. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `RATE_LIMIT_ENABLED` | `true` | Set to `false` to disable the HTTP rate-limit middleware. |
| `LEGISLATION_PUBLIC_BASE` | `https://lawkitty.app` | Public base URL used for absolute links in MCP tool output. Set it to your own URL, or links will point at the hosted service. |

### Database

| Variable | Default | Purpose |
|---|---|---|
| `LAWKITTY_DATABASE_URL` | none | Postgres connection URL, for example `postgresql://lawkitty:CHANGE_ME@127.0.0.1:5432/lawkitty`. Without it the app logs a warning and user data (accounts, tokens, grants) is unavailable. |
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` | `127.0.0.1`, `5432`, `postgres`, empty, `lawkitty_cases` | Read only by `backend/services/tax_case_sql.py`, a separate case-metadata database. Not needed for the core app unless you use that module. Which features depend on it is not established here. |

### Sign-in and email

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | none | Enable Google sign-in when both are set. |
| `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` | none | Enable Microsoft sign-in when both are set. |
| `MICROSOFT_TENANT` | `common` | Entra tenant (a tenant GUID, a domain, or `common`). |
| `RESEND_API_KEY` | none | Sends magic-link emails through Resend. If unset, the magic link is written to the application log instead of emailed (useful locally). |
| `RESEND_FROM` | `meow@lawkitty.app` | Sender address for magic-link emails. Set it to an address on a domain you have verified with Resend. |

Magic-link sign-in needs no provider credentials, only `LAWKITTY_DATABASE_URL`.

### Admin and access

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_EMAILS` | empty | Comma-separated admin email addresses (case-insensitive). Empty means nobody is an admin. Admin status follows the signed-in email. |

### MCP

| Variable | Default | Purpose |
|---|---|---|
| `MCP_AUTH_TOKEN` | none | Operator token. A request bearing it is accepted as the operator with the unlimited tier and **bypasses content gating**. Treat it as a root credential: long, random, secret, and unset if you do not need it. |
| `LEGISLATION_BEARER_TOKEN` | none | Used as the operator token when `MCP_AUTH_TOKEN` is empty. `.env.example` ships `your_token_here` for this variable; replace it with a strong secret or delete the line, because whatever value is set here works as the operator token. |
| `MCP_EXTRA_ALLOWED_HOSTS` | empty | Comma-separated bare hostnames (for example `lawkitty.example.com`) accepted in the `Host` header. The MCP transport has DNS-rebinding protection and allows only localhost and `lawkitty.app` by default, so **your public hostname must be listed here** or MCP requests are refused (the code comments describe a 421). |
| `MCP_EXTRA_ALLOWED_ORIGINS` | empty | Comma-separated full origins (for example `https://lawkitty.example.com`) accepted in the `Origin` header. |
| `OAUTH_BASE_URL` | `https://lawkitty.app` | Base URL for the OAuth 2.1 flow that MCP connectors (such as Claude's) use. Set it to your own URL. |
| `OAUTH_DB_DIR` | repository root | Directory for `oauth_state.db` (SQLite). |

### Search and embeddings

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | none | Embeddings key. If unset, the app also looks for an `OPENAI_API_KEY=` line in `~/.lawkitty/.env`. |
| `EMBED_API_KEY` | none | Takes precedence over `OPENAI_API_KEY`. |
| `EMBED_BASE_URL` | empty | Base URL of an OpenAI-compatible gateway. If empty, the OpenAI default is used. |
| `EMBED_MODEL` | `text-embedding-3-small` | Model name sent to the endpoint. Keep it equivalent to the model that produced your stored vectors. |
| `EMBED_TIMEOUT` | `8` | Seconds before an embedding request times out. |
| `SEARCHD_URL` | `http://127.0.0.1:8791` | Where the app looks for the optional `searchd` service. If it is unavailable the app falls back to in-process search. |
| `GRAPH_DB` | `data/graph.db` | Citation graph database. |
| `GRAPH_BOOST`, `GRAPH_DEGREE_CAP`, `PRIVATE_RULING_WEIGHT`, `PRIVATE_RULING_CAP`, `PRIVATE_RULING_CAP_WINDOW`, `GUIDE_WEIGHT`, `REF_QUERY_VECTOR_WEIGHT` | see `backend/routes/search.py` | Ranking tuning. Leave at defaults unless you are tuning. |
| `LAWKITTY_AS_AT` | `2026-04-01` | Reference date used by `backend/resolve_ref.py`. |

### Content paths

Content locations are set in `data_sources.yaml`, not in `.env` (section 4). Each type can be overridden with `LAWKITTY_SOURCE_<KEY>`, where `<KEY>` is the type key in upper case with `-` as `_` (for example `LAWKITTY_SOURCE_ATO_PRIVATE_RULING`).

| Variable | Default | Purpose |
|---|---|---|
| `LAWKITTY_SOURCE_<KEY>` | none | Path override for one content type. |
| `CASE_SUMMARIES_DIR` | `scripts/cleaned/summaries` | Case summaries used for citation-to-name lookup. The app also serves `scripts/cleaned/summaries` under `/static/cleaned/summaries` if that directory exists. |
| `CASE_DIR`, `RULING_DIR`, `ATO_RULING_DIR`, `NZ_RULING_DIR`, `COMMENTARY_DIR`, `PRIVATE_RULINGS_ROOT` | none | Deprecated aliases for the per-type paths. Use `LAWKITTY_SOURCE_<KEY>`. |

### Optional features and operations

These are not needed to run the reader and MCP server.

| Variable | Purpose |
|---|---|
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_SOLO`, `STRIPE_PRICE_POWER`, `STRIPE_WEBHOOK_SECRET` | Billing code in `backend/billing.py`. Leave unset unless you intend to charge for access. |
| `TAX_API_URL`, `TAX_API_TOKEN` | Upstream service for interpretive-chain endpoints. Default URL `http://127.0.0.1:8793`. That service is not part of this repository. |
| `LEGISLATION_EXPLORER_DIR` | Directory used by the commentary bridge (`backend/routes/commentary.py`) and corpus scripts. |
| `OPENROUTER_API_KEY` | Used by the social-post routes (`backend/routes/social.py`). |
| `SYSTEMD_SERVICE_NAME` | Service name the admin health route reports on. Default `lawkitty`. |
| `VITE_ALLOWED_HOSTS` | Comma-separated hostnames the Vite dev server accepts (frontend development only). |
| `LEGISLATION_STAGING_DIR`, `LEGISLATION_EXPLORER_DB_DIR`, `KNOWLEDGE_MCP_DIR`, `LAWKITTY_HOME`, `LAWKITTY_ENV_FILE`, `BACKUP_DIR`, `DB_BACKUP_DIR`, `LOG_DIR`, `TABLE_REBUILD_OUT_DIR`, `EMBEDDINGS_BACKUP_DB`, `ASIC_CASES_DIR`, `ASIC_FINREG_DB`, `DEPLOY_HOST`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Listed in `.env.example` for the ingestion, backup, deploy and monitoring scripts. They are not read by the web application. See the individual scripts. |

## 4. Corpus and data layout

The app reads files from disk. Locations come from `data_sources.yaml` (committed defaults), optionally overlaid by a git-ignored `data_sources.local.yaml`, then by `LAWKITTY_SOURCE_<KEY>` environment variables. Relative paths resolve against the repository root. Inspect what the app resolved with:

```bash
python -m backend.data_sources --dump
```

Default paths in `data_sources.yaml`:

| Content type (key) | Default path | Layout noted in the code |
|---|---|---|
| `legislation` | `data/` | One directory per Act: `data/<act>/tree.json` (for example `data/itaa-1997/`) |
| `case` | `public-corpus/cases` | Case files; format defined by `content/case/loader.py` |
| `ruling` (ATO public rulings) | `data/rulings` | Subdirectories per series (`td`, `tr`, `pcg`, `ps_la`) |
| `ato-private-ruling` | `public-corpus/private_rulings/data/json` | One `<authnum>.json` per ruling |
| `nz-ruling` | `data/nz_rulings` | `<TYPE>_<YEAR>_<N>.txt` plus an `nz_rulings_list.json` manifest |
| `treaty` | `data/treaties` | Defined by `content/treaty/loader.py` |
| `regulatory-guide` | `data/regulatory-guides` | Defined by `content/regulatory-guide/loader.py` |
| `afsa-guide` | `data/afsa` | Defined by `content/afsa-guide/loader.py` |

Optional types (`commentary`, `insolvency`, `nz-mtg`) are licensed commentary. They are marked `default_configured: false` and `auth: admin`, and the committed manifest may never enable them. To use them, set `allow_optional_sources: true` and their paths in `data_sources.local.yaml`. The expected file markers for each are listed under `data_markers` in the type's `type.yaml`.

Behaviour to know about:

- A type with no path configured is simply empty.
- A type whose configured path does not exist is logged as an error. **In production (see `APP_BASE_URL`) a configured but missing source stops startup** with `data sources missing in production`. If you do not have data for a default type, unset it with `{path: null}` in `data_sources.local.yaml`.
- The search index (`search_index.db`, SQLite FTS5) is built from the data on startup if the file is missing or empty. This runs in the background and takes time on a large corpus.
- Vector search reads `data/embeddings.db` and builds a memory-mapped matrix snapshot (`data/embeddings_rows.npz` and related files) with `scripts/build_vector_matrix.py`. If `embeddings.db` has no `embeddings` table, vector search is disabled and the app logs that. How to populate `embeddings.db` from scratch is not documented in this repository.
- Other derived artefacts (the citation graph `data/graph.db`, citation and definition indexes) are produced by scripts in `pipeline/` and `scripts/`. No single documented command builds all of them. `docs/BUILD_PROCESS.md` describes the project's working process, and `rebuild.sh` is a full rebuild from source PDFs that are also not included.

Without data the app still starts (outside production). The UI and API simply have nothing to show.

## 5. Database

1. Create a role and database:

   ```bash
   sudo -u postgres createuser lawkitty
   sudo -u postgres createdb -O lawkitty lawkitty
   sudo -u postgres psql -c "ALTER ROLE lawkitty PASSWORD 'CHANGE_ME'"
   ```

2. Set `LAWKITTY_DATABASE_URL=postgresql://lawkitty:CHANGE_ME@127.0.0.1:5432/lawkitty`.

There is no separate migration step. `backend/db.py` runs `init_db()` when it is imported, executing a schema made of `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements. If Postgres is unreachable at startup the error is logged and the app continues without user data until it can connect. Look for `lawkitty init_db failed` in the log.

Tables created: `users`, `magic_tokens`, `user_prefs`, `mcp_tokens`, `mcp_call_log`, `issues`, `content_type_settings`, `user_content_grants`.

## 6. Run locally

Set at least these in `.env` (all placeholders):

```
APP_BASE_URL=http://localhost:8765
LAWKITTY_DATABASE_URL=postgresql://lawkitty:CHANGE_ME@127.0.0.1:5432/lawkitty
OPENAI_API_KEY=CHANGE_ME
ADMIN_EMAILS=you@example.com
```

Start the backend from the repository root. Port 8765 is the port used by the Vite proxy, the example systemd unit and `CONTRIBUTING.md`:

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8765 --env-file .env
```

Open http://localhost:8765/app/ for the reader (built frontend) and http://localhost:8765/ for the static site.

For frontend development, leave the backend running and start Vite:

```bash
cd frontend
npm run dev
```

Vite serves under `/app/` (so http://localhost:5173/app/, its default port) and proxies `/api` to `http://127.0.0.1:8765`. Only `/api` is proxied; `/auth` and `/mcp` are not, so sign in through the backend port. `CONTRIBUTING.md` mentions port 3000, but `vite.config.ts` does not set a port.

### Signing in locally

- Magic link: enter your email on the login page. With no `RESEND_API_KEY` the link and code are written to the backend log rather than emailed; copy the link from the log. Signing in creates the user row.
- Admin: use an address listed in `ADMIN_EMAILS`.
- No sign-in at all: set `DEV_MODE=true`. This works only while `APP_BASE_URL` is not a non-local `https://` URL.
- The session cookie is set with the `Secure` flag. Some browsers may not store Secure cookies from `http://localhost`. If sign-in appears to succeed but you stay signed out, that is a likely cause, and `DEV_MODE=true` is the simplest workaround. Behaviour across browsers has not been tested here.

## 7. Google and Microsoft sign-in

Both are optional. A provider is offered only when its client ID and secret are set.

Register this redirect URI with each provider:

- Google: `{APP_BASE_URL}/auth/callback/google`
- Microsoft: `{APP_BASE_URL}/auth/callback/microsoft`

Provider specifics:

- **Google:** publish the OAuth consent screen (move it out of Testing mode), or only listed test users can sign in. A sign-in is accepted only if Google reports `email_verified` as true.
- **Microsoft (Entra ID):** in the app registration, add the optional ID-token claims `email` and `xms_edov` (email domain owner verified), and set `requestedAccessTokenVersion` to 2 in the manifest. Unless `xms_edov` is true, a work or school sign-in is refused; personal Microsoft accounts (consumer tenant) are verified by Microsoft and do not need it. Set `MICROSOFT_TENANT` to your tenant to restrict sign-in to it; the default `common` accepts any tenant (the verified-email check still applies).

Accounts are keyed by email and admin rights follow the email, which is why the verified-email checks are mandatory. Step-by-step provider setup, and the exact rules for accepting a Google or Microsoft sign-in, are in `docs/auth-sso.md`.

## 8. MCP

- Endpoint: `{APP_BASE_URL}/mcp` (Streamable HTTP, stateless, JSON responses). The route also accepts `/mcp/<token>` and sub-paths.
- Add your public hostname to `MCP_EXTRA_ALLOWED_HOSTS` and your origin to `MCP_EXTRA_ALLOWED_ORIGINS`, as above.
- Users create tokens after signing in: Account, MCP connector, Generate token (or `POST /api/mcp-token` with `{"name": "..."}` while signed in). The token is shown once and only its hash is stored.
- A client presents the token as `Authorization: Bearer <token>`, `X-API-Key`, `?token=`, or in the path. The middleware accepts further variants; see `backend/fastmcp_server.py`.
- Accepted credentials: a per-user token, the operator token (`MCP_AUTH_TOKEN`), or an OAuth access token issued through `/oauth/*` for connectors that use OAuth.
- Content gating applies per token owner: an `auth: admin` content type is returned only if the owner is an admin or has been granted it. The operator token is not filtered.
- Connecting Claude and other clients: see `docs/mcp-guide.md`. Its URLs name the hosted service; substitute your own base URL. Tool details are in `MCP_REFERENCE.md`.
- Daily caps per tier are defined in `backend/mcp_token_manager.py`: 5,000 calls a day for `trial` and `solo`, unlimited for `power`. A new user starts on `trial`.

## 9. Production (generic)

Run uvicorn on loopback and put a TLS-terminating reverse proxy or tunnel in front of it. Do not bind it to a public interface. Use an `https://` `APP_BASE_URL`; this switches on the production checks (`SESSION_SECRET` required, `DEV_MODE` ignored, missing data sources fatal).

`searchd`, if you use it, also has no authentication. Keep it on `127.0.0.1` (its default) and never expose it.

Example systemd unit, `/etc/systemd/system/lawkitty.service`. Paths and user are placeholders:

```ini
[Unit]
Description=LawKitty
After=network.target postgresql.service

[Service]
Type=simple
User=lawkitty
WorkingDirectory=/opt/lawkitty
EnvironmentFile=/opt/lawkitty/.env
ExecStart=/opt/lawkitty/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Minimal Caddy configuration (Caddy obtains the certificate itself):

```
example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Minimal nginx equivalent (add your own TLS configuration):

```nginx
server {
    listen 443 ssl;
    server_name example.com;
    # ssl_certificate and ssl_certificate_key go here

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

With `example.com` as the host, set `APP_BASE_URL=https://example.com`, `LEGISLATION_PUBLIC_BASE=https://example.com`, `OAUTH_BASE_URL=https://example.com`, `ALLOWED_ORIGINS=https://example.com`, `MCP_EXTRA_ALLOWED_HOSTS=example.com` and `MCP_EXTRA_ALLOWED_ORIGINS=https://example.com`.

The deploy and corpus-push scripts the hosted service uses (`scripts/deploy.sh`, `scripts/update-corpus.sh` and similar) are not part of the public release. Updating is up to you: pull, `pip install -r backend/requirements.txt`, rebuild the frontend, restart the service.

Optional `searchd` is covered in `searchd/README.md`. It needs a Rust toolchain, and its index is built from the app's existing embeddings with `searchd/build_index.py`, `lexbuild` and `indexbuild`. That README's example commands use the maintainers' own paths and a systemd user unit, and no `searchd` unit file is in the public release. Choose the HNSW index file with `SEARCHD_INDEX` (default `idx_f32.usearch`). The `serve` binary (`searchd/src/bin/serve.rs`) takes the index directory and the port as its first two arguments, for example `./target/release/serve /path/to/searchd-data 8791`. Its default port is 8790, but the app looks for 8791 (`SEARCHD_URL`), so pass 8791 or change `SEARCHD_URL`.

## 10. Administration

- Admins are the addresses in `ADMIN_EMAILS`. Sign in with one and open `/admin` for the dashboard (tokens, health, users, logs).
- Content types: in `/admin`, "Content types" holds the global on/off switches and "Per-user content grants" grants `auth: admin` types to individual users.
- A type is available to a user when it is globally enabled and either it is `auth: user`, or the user is an admin, or the user holds a grant. A global off beats admin status and grants.
- The `maps`, `workflows` and `chains` features are admin-only in the same way.
- The same controls exist as API endpoints under `/api/admin/content-types` and `/api/admin/users/{email}/content-types`. See `docs/content-type-toggles.md` for the endpoint table and known gaps (for example, some per-type routes are not gated).
- If Postgres is unreachable, `auth: user` types stay on and every `auth: admin` type is off for everyone, admins and the MCP operator token included, so an outage never undoes a global off.

## 11. Tests

```bash
pytest backend/tests
```

Run from the repository root; `backend/pytest.ini` sets the import path, and the test configuration sets a default `ADMIN_EMAILS`. Many tests need a reachable Postgres (`LAWKITTY_DATABASE_URL`) and a populated corpus, so on a bare checkout expect failures that reflect missing data and services rather than code faults. There are also tests under `tests/` and `scripts/test_*.py`. Frontend tests use Vitest (`cd frontend && npx vitest`).

## Open points

Things this guide could not settle from the code:

- How to obtain or build the corpus, including `embeddings.db`, `data/graph.db` and the other derived indexes. No single documented command exists.
- The on-disk format for most content types beyond `legislation`, `ato-private-ruling` and `nz-ruling`; read `content/<type>/loader.py`.
- What the app does at query time when the embeddings key is set but invalid. The key must be set for the process to start.
- The full `searchd` build and run sequence on a new host (the README commands are the maintainers' own).
- Which features need the `PG*` case database used by `backend/services/tax_case_sql.py`.
- Whether the `Secure` session cookie works over `http://localhost` in every browser.
