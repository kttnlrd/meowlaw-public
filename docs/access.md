# Access control

By default everyone who can reach the server reads every corpus. To keep some
corpora (internal handbooks, client precedents, licensed material) from some
readers, add an `[access]` section to `meowlaw.toml`:

```toml
[access]
default = "public"          # tag of corpora that set none
anonymous = ["public"]      # tags readable without a token

[access.corpora]            # corpus id pattern (* wildcards) -> tag
"handbook-*" = "internal"
"precedent-*" = "restricted"

[[access.tokens]]
name = "staff"
env = "MEOWLAW_TOKEN_STAFF" # environment variable holding the token
tags = ["public", "internal"]

[[access.tokens]]
name = "partners"
env = "MEOWLAW_TOKEN_PARTNERS"
tags = ["public", "internal", "restricted"]
```

A corpus's tag is its `access` metadata if set (in front matter, a sidecar
or `corpus.toml`: `[meta] access = "internal"`), else the first matching
pattern under `[access.corpora]`, else `default`.

The main token (`server.token_env`) reads everything and is the only one
that can use the ingest page. Set `anonymous = []` to require a token for
everything, including `/mcp`.

## What a reader without the tag sees

The same rule applies to the REST API, the UI and MCP:

- Hidden corpora are missing from corpus lists, the drawer and search
  results. Search leaves them out before ranking, so pages still fill.
- Fetching, outlining or graphing a hidden node, or a citation that resolves
  to one, fails as "not found".
- Citations from visible text into a hidden corpus read as unresolved.
- `cited by`, `similar`, definitions and graph neighbours from hidden corpora
  are dropped. A path that only exists through a hidden node is "no path".
- `get_info` omits store-wide totals.

In the UI, readers sign in with "Use an access token" in the footer; the
token is kept in the browser's local storage.

The command line and stdio MCP (`meowlaw mcp`) read the store directly and
are not restricted. Control who can run them with file permissions.

Hidden text still exists in the store and in exports such as `meowlaw graph
export`. Use separate deployments when material must not share a database.
