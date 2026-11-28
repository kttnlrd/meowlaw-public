# Content-type toggles

Runtime on/off switches for every content type, plus per-user grants. This sits on top of
source resolution (`backend/data_sources.py`); it does not change `default_configured`,
`data_markers` or `--assert-default-only`.

## Keys

The 11 registry types (`content/*/type.yaml`) plus three feature keys: `maps`, `workflows`, `chains`.

## Access rule (`backend/services/content_access.py`)

A key is available to a user iff:

1. it is globally enabled (`content_type_settings`; no row = enabled), **and**
2. the type is open to users (registry `auth: user`, or a feature key), **or** the user is an
   admin (`ADMIN_EMAILS`), **or** the user has a row in `user_content_grants`.

A global OFF beats admin status and grants, and hides the type from the MCP operator token and
stdio too (`content_access.enabled_keys()`), for every type. If Postgres is unreachable, the `auth: user`
types stay ON, every private type (`auth: admin`, which includes the features) is treated as OFF
for everyone, admins and the operator token included, and grants fail closed. An OFF that cannot
be read is never undone by an outage.

## Tables (created in `backend/db.py` `_SCHEMA`)

- `content_type_settings (key PK, enabled BOOLEAN)`
- `user_content_grants (email, content_key, PK(email, content_key))`

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/api/content-types` | current user: `[{key, label, auth, enabled, available}]` |
| GET | `/api/admin/content-types` | admin: same shape |
| PUT | `/api/admin/content-types/{key}` | `{enabled: bool}` |
| GET | `/api/admin/users/{email}/content-types` | `{email, granted: [keys]}` |
| PUT | `/api/admin/users/{email}/content-types` | `{keys: [...]}`, replaces the grant set |

## Enforcement

- Search: `/api/search`, `/api/search/flat`, `/api/search/hybrid`, `/api/unified-search` and the MCP
  `search` tool drop hits whose `content_type` is unavailable.
- MCP callers (`backend/fastmcp_server.py` `_mcp_owner`), failing closed:
  - the static operator token (`MCP_AUTH_TOKEN` / `LEGISLATION_BEARER_TOKEN`) and the stdio
    transport (no usage event, process not serving HTTP) are not restricted by grants;
  - a token, OAuth or session owner is gated as that user;
  - any other HTTP request (DEV_MODE, an empty owner, an owner whose email reads "operator", a
    tool call whose usage event was lost) gets the anonymous view.
  - Operator / stdio behaviour change (release 1.1): before, they ignored the global toggles. With
    every type ON (the golden tests' setting) their output is unchanged; with a type OFF they now
    get, for that type, what any denied caller gets (no `list_content_types` row, "not available"
    or, for a hidden type, "not found"; `graph_path` withholds a path through it).
- `maps` off: `/api/maps` returns `[]`, `/api/maps/{id}` 404.
- `workflows` off: every `/api/workflows*` route 404.
- `chains` off: `/api/tax/health` returns `ok: false`, `/api/tax/chains/...` returns an error body.
- Frontend: the sidebar hides rulings, private rulings, tax cases, treaties, maps, chains, AFSA and
  RG entries the user can't access. Legislation, search, account and admin always show.
- Admin UI: `/admin` → "Content types" (global toggles) and "Per-user content grants".

## Known gaps

- Search `total` is counted before filtering, so it can overstate when a type is off.
- `/api/search/facets` counts and the per-type routes (`/api/tax-cases/search`, `/api/treaties/search`,
  document fetch routes, other MCP tools) are not gated.
