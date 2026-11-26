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

A global OFF beats admin status and grants. If Postgres is unreachable, everything is available.

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
  `search_all` tool drop hits whose `content_type` is unavailable (MCP: the operator token and
  stdio calls are not filtered).
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
