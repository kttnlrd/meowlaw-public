# lawkitty-local

A small, local-only, read-only LawKitty: one Rust binary that serves the existing React app
and the browse API it calls, from a data folder on your own disk.

It exists to load fast. Nothing but the list of sources is read at startup. A source's tree is
parsed the first time you open it, a section file when you open that section, and the search
index when you search.

- Binds `127.0.0.1` only, and answers only requests addressed to `127.0.0.1` or `localhost`,
  so a web page elsewhere cannot read your data folder through the browser.
- No sign-in, accounts, tokens, OAuth, admin, usage logging, MCP or rate limits.
- Makes no network calls and works offline. (The web app's `index.html` asks Google Fonts for
  its typefaces; offline, the browser falls back to system fonts.)

## Set up

The app has three parts, built or fetched separately:

| Part | What it is | Where it comes from |
| --- | --- | --- |
| The server | One Rust binary, about 5 MB | Built from `local-app/` |
| The web interface | The LawKitty React app, as static files | Built from `frontend/` into `frontend/dist` |
| The data | The law itself: one folder per Act, ruling set or collection | A separate download (see [Make the data download](#make-the-data-download)) |

### 1. Install the tools (once)

- **Rust** (stable): <https://rustup.rs>. On Windows, take the default "MSVC" option; it also
  installs the Visual Studio C++ build tools it needs.
- **Node.js 20 or later**, which includes npm: <https://nodejs.org>. Only needed to build the
  web interface.
- **Git**, to get the repository. On Windows, Git for Windows also gives you Git Bash, which
  runs the `.sh` scripts below.

### 2. Get the code

```sh
git clone https://github.com/kttnlrd/meowlaw-public.git lawkitty
cd lawkitty
```

### 3. Build the server

```sh
cd local-app
cargo build --release
cd ..
```

The binary is `local-app/target/release/lawkitty-local` (`lawkitty-local.exe` on Windows).
The first build downloads the Rust libraries and takes a few minutes; later builds are quick.

### 4. Build the web interface

```sh
local-app/build-frontend.sh
```

This runs `npm ci` the first time, then `npm run build`, and writes `frontend/dist`. Without
Git Bash on Windows, run the same two commands yourself: `cd frontend`, `npm ci`,
`npm run build`. Do this again whenever `frontend/` changes.

### 5. Get the data

Download the data archive (`lawkitty-data-<version>.tar.gz`) and unpack it anywhere, for
example on a large drive:

```sh
tar -xzf lawkitty-data-20261128.tar.gz -C /path/to/        # creates /path/to/lawkitty-data
```

The full corpus is about 13 GB unpacked. Windows 10 and 11 include `tar`, so the same command
works in PowerShell. The folder's `DATA_VERSION` file records which release it is.

### 6. Run it

```sh
local-app/target/release/lawkitty-local --data /path/to/lawkitty-data
```

or set the folder once in the environment:

```sh
export LAWKITTY_DATA=/path/to/lawkitty-data          # PowerShell: $env:LAWKITTY_DATA = "D:\lawkitty-data"
local-app/target/release/lawkitty-local
```

Then open <http://127.0.0.1:8790/app/>. The server prints the number of sources it found and
how long it took to start; press Ctrl+C to stop it. To update the law, unpack a newer data
archive over the old folder (or beside it) and restart the server.

To run it from somewhere else, copy the binary and put the web interface next to it as a
folder called `dist` (or pass `--dist`): the server looks for `frontend/dist` in the current
folder, then in the checkout it was built in, then for `dist` beside the binary.

## Make the data download

`local-app/make-data.sh` packages a corpus folder (the main app's `data/` directory) as the
download in step 5:

```sh
local-app/make-data.sh /path/to/corpus/data /path/to/output
```

It writes `/path/to/output/lawkitty-data/` and `lawkitty-data-<date>.tar.gz`, and only reads the
corpus. It copies each source folder that has a `tree.json` (with its `sections/`,
`licence.json`, `collection.yaml` and `aliases.json`), plus `definitions_all.json` and
`search_index.db` when present, and stamps `DATA_VERSION` with the date.

It leaves out:

- private and licensed collections: any `collection.yaml` with `access: restricted` or
  `access: admin`, and any folder named in `MAKE_DATA_EXCLUDE` (space-separated);
- the server-only databases (`embeddings.db`, `users.db`, `graph.db`), which the local app
  does not read.

Full-text search needs `search_index.db` in the corpus folder before packaging. Without it the
app still browses everything; search answers empty with a note.

## The web version

The interface you see is the same React app at every scale; only the server behind it changes.

- **On your own machine:** the local app above serves it at `http://127.0.0.1:8790/app/`. It
  deliberately refuses requests that are not addressed to `127.0.0.1` or `localhost`, so it
  cannot be opened from another computer, even on the same network.
- **For other people, on the web:** use the main LawKitty server (the Python app in
  `backend/`), which adds sign-in, per-user access to collections, the MCP server, search over
  rulings and cases, and admin. See `SETUP.md` for installing and configuring it; it serves
  the same `frontend/dist`, built with `npm ci && npm run build` in `frontend/`.
- **Rebuilding the interface:** after any change in `frontend/`, rebuild `frontend/dist`
  (step 4) and restart whichever server you use. Both read the files at start-up.

## Options

| Option | Environment | Default |
| --- | --- | --- |
| `--data <dir>` | `LAWKITTY_DATA` | `./lawkitty-data` |
| `--port <n>` | `LAWKITTY_PORT` | `8790` |
| `--dist <dir>` | `LAWKITTY_DIST` | `frontend/dist` in the working directory, else in the checkout the binary was built in, else `dist` beside the binary |
| `--state <file>` | `LAWKITTY_STATE` | `~/.config/lawkitty-local/state.json` |
| `--search-db <file>` | | `search_index.db` in the data folder, else beside it |

The data folder is the Python app's `data/` directory: one folder per source, each with a
`tree.json` and a `sections/` folder, plus `licence.json` and, for a collection,
`collection.yaml`. It is downloaded separately and is never modified.

## What is served

Real, with the same JSON as the Python app byte for byte:

| Endpoint | Notes |
| --- | --- |
| `GET /api/acts` | Every source with a `tree.json`; collections carry `group` and `access`. `?jurisdiction=` filters. |
| `GET /api/tree/{act}` | `?shallow=1` and `?open=<section>` as in `backend/routes/acts.py`, including split parts and lazy divisions, and the `X-Uncompressed-Length` header. |
| `GET /api/tree/{act}/part` | `?id=` or `?section=`. |
| `GET /api/section/{act}/{section}` | Frontmatter, body (scraped-markup clean-up and definition links), licence notice, dictionary hint. |
| `GET /api/info` | Version and changelog, from `src/info.json`. |

Search, from the Python app's SQLite FTS5 index when a `search_index.db` is present
(legislation sections only: `sections_fts` and `sections_meta`; no embeddings, rulings or
cases): `/api/search`, `/api/search/hybrid`, `/api/search/suggest`, `/api/search/flat`. The
database is opened in place, read-only. With no index these answer with empty results and a
`note` saying so; `/api/search?act=` still matches section numbers and titles from the act's
tree.

Local stand-ins, so the app runs without an account:

| Endpoint | Answer |
| --- | --- |
| `GET /auth/me`, `GET/PUT /api/account` | One fixed local user (`local@localhost`), not an admin. |
| `GET/PUT /api/account/pinned-sources` | Kept in the state file. |
| `GET/PUT /api/user/prefs`, `POST /api/user/prefs/reset` | Kept in the state file. |
| `GET /api/content-types` | Legislation and collections available; everything below unavailable. |
| `GET /api/mcp-tokens`, `/api/maps`, `/api/cases/..`, `/api/rulings/..`, `/api/comments/..`, `/api/section-refs/..`, `/api/search/facets`, `/api/search/warm`, `/api/tax/health` | Empty. |

Not in this build: public and private rulings, tax cases, ASIC and AFSA guides, treaties,
procedural maps, chains, commentary, the definitions browser and popover, the related-content
graph, comments, and everything to do with accounts. Their sidebar entries do not appear, and
any other `/api/...` path answers `404 {"detail":"Not Found"}`. Sections of the Corporations
Act and the Bankruptcy Act are served without the `regulatory_guides` / `afsa_guides` lists.

The web app is served at `/app/` as the Python app serves it: files from `frontend/dist`,
`index.html` for every other `/app/...` path, and old root-level deep links
(`/itaa-1997/8-1`) redirected to `/app/...`.

## How it stays fast

- **Startup** lists the data folder and, for each source, reads `licence.json`,
  `collection.yaml` and the first 16 KB of `tree.json`, which is where the act name and
  compilation sit. A tree that orders its keys differently is scanned once, on the first
  `/api/acts`, without being built in memory.
- **Trees** are parsed on first request and cached for the life of the process. The shallow
  response is cached as bytes. Parts and divisions are written straight from the cached tree.
- **Sections** are read from disk on request. The definitions index
  (`definitions_all.json`) is read on the first section. Up to 512 rendered sections are
  kept in memory.
- **SQLite** is opened read-only on the first search and queried in place.

Data is read once per process: restart after changing the data folder.

## Tests

```sh
cd local
cargo test --release
cargo clippy --all-targets
```

`tests/contract.rs` requests each path in `tests/golden/manifest.json` and compares the
response, byte for byte, with what the Python app returns for it. It needs the corpus
(`LAWKITTY_DATA`, or `../data`) and is skipped without it.

The expected bodies come from the real Python route functions, called directly by
`tests/gen_golden.py` (no HTTP, sign-in or database). Run it with a Python that has the
backend's requirements installed, after any change to the corpus or to the Python routes:

```sh
python local-app/tests/gen_golden.py                             # the committed set
python local-app/tests/gen_golden.py --sweep 40 --out /tmp/sweep # + every tree, 40 sections per source
LAWKITTY_GOLDEN=/tmp/sweep cargo test --release --test contract
```

It also rewrites `src/info.json` from `backend/routes/api.py`.

## Known differences from the Python app

- `collection.yaml` is read as plain `key: value` lines (title, group, access), not through a
  YAML parser.
- Definition links match terms case-insensitively by Unicode lower-casing; Python's regex
  case folding differs for a handful of non-ASCII letters that do not occur in the corpus.
- A malformed `licence.json` drops the licence notice rather than failing the request.
- Search covers the legislation lane only, in BM25 order; result rows carry fewer fields than
  the Python fused search.
