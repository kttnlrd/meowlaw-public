# searchd

Hybrid retrieval service for LawKitty: **tantivy BM25 + usearch HNSW**, fused with
RRF, with a decisive boost when a query reads as an exact section reference.

It replaces the Python exhaustive matrix scan (`matrix @ query` over
384,869 × 1536 floats = ~124ms/query) with an HNSW graph search (~2ms), so
latency stops scaling with corpus size.

## Why this exists

The win is the **algorithm, not the language**. A numpy BLAS matvec over the full
matrix beats a naive scalar dot loop; what beats *both* is not scanning at all.

Measured on this corpus (384,869 × 1536):

| | production Python | searchd |
|---|---|---|
| vector scan | ~124ms, O(N), every query | ~2ms (HNSW) |
| lexical | 0.1–21ms (FTS5) | 0.3–10ms (tantivy) |
| whole hybrid | ~290ms | ~160ms; **~13ms** with a warm embed cache |
| resident cost | ~2.4GB in the app process | mmapped, droppable |

## The ranking defect it fixes

A query that reads as a section reference is a *lookup*, not a topic. The old
path folded the citation match into the fusion as just another RRF list, so a
rank-1 citation hit was worth 1/61 ≈ 0.0164 and prose that merely *mentioned*
the section could outrank it. Measured before the fix:

    q=26AH   ->  1. ITAA 1936 s 160AAB   2. s 15-75   3. s 26AH      <- wrong

An exact reference match is now decisive (`+1.0` ≈ 61× a rank-1 RRF
contribution), in **both** this service and `backend/routes/search.py`, so the
fix holds even if searchd is down.

## Security

`searchd` has **no authentication**. It binds `127.0.0.1` and is never exposed:
the Python app owns auth, sessions and metering, and proxies to it. Binding
`0.0.0.0` would publish an open, unmetered corpus endpoint. `SEARCHD_BIND` exists
only for deliberate debugging — never set it in a deployed unit.

## Files

| file | role |
|---|---|
| `src/bin/serve.rs` | the service: hybrid search, slicers, facets, `/health` |
| `src/bin/lexbuild.rs` | builds the tantivy BM25 index from `meta.json` |
| `src/bin/indexbuild.rs` | builds the usearch ANN index from `matrix.f32` |
| `src/main.rs` | the original benchmark/eval harness (spike 001) |
| `build_index.py` | extracts matrix/ids/meta from the app's embeddings |
| `index.html` | standalone UI (not used by the app) |
| `test_search.py` | regression suite |

`build_index.py` reads the app's **existing** precomputed embeddings — no
re-embedding, no OpenAI spend.

## Rebuild procedure

Rebuild whenever the corpus grows. Row counts are derived from the files, so a
growing corpus cannot silently misalign (the spike's bench hardcoded 306,452 and
broke the moment the corpus hit 384,869).

```bash
cd searchd
python3 build_index.py                    # matrix.f32, ids.i64, meta.json
# (build_index.py SRC /tmp/x --meta-only  -> meta.json only, to a temp dir)
./target/release/lexbuild .               # tantivy_idx/      (~3s, 74MB)
./target/release/indexbuild . both        # idx_f32/idx_i8    (~10min)
systemctl --user restart searchd
```

`serve.rs` asserts `ann.size() == meta` rows at startup, so a stale index next
to fresh data fails loudly instead of returning keys shifted by the difference.

## Host sizing

| host | index | why |
|---|---|---|
| this box (dev) | `idx_f32.usearch` | best recall; there is RAM for it |
| droplet (4 vCPU / 7.8GB) | `idx_i8.usearch` | 3.7× smaller, ~93% recall |

Set per host via `SEARCHD_INDEX` in the unit rather than baking the smaller index
into the default. On the droplet this also *frees* RAM overall: the app's 2.9GB
Python RSS holds the 2.36GB matrix, which searchd replaces with a ~650MB
mmapped index.

## API

`GET /api/search?q=&k=&type=&act=&series=&mode=`

- `type` — comma-separated source types (`section,ruling,private_ruling,case,...`)
- `act` — comma-separated sources (`itaa-1997`, `private-rulings`, ...)
- `series` — comma-separated public-ruling series (`TR,TD,PS LA`, case-insensitive). Like the
  app's `rtype` filter it constrains public rulings only; sections, cases and private rulings
  pass through, so pair it with `type=ruling` for rulings of that series alone.
- `outcome` / `year` / `court` — comma-separated (`yes,no,mixed` / `2010,2011` / `HCA,FCAFC`,
  case-insensitive). **Strict**, unlike `series`: only rows that carry one of the values pass, so
  `court=HCA` is HCA cases alone. Needs a `meta.json` + `tantivy_idx` built with those columns;
  against an older build the request returns an explicit `error` and no hits.
- `mode=lex` — BM25 only, **no embedding call at all** (the instant first paint)

Returns `{query, embed_ms, ann_ms, bm25_ms, total_ms, lexical_only, hits[], facets{types[],acts[],series[],outcome[],year[],court[],scope,pool}, error}`.
The four dimension arrays are omitted when empty. Their counts are over the **unfiltered fused
candidate pool** (`scope: "candidates"`, `pool` = distinct rows, ≤ 200), not the page and not the
corpus, and they count rows (chunks), so a multi-chunk ruling counts once per chunk. Rows without
the dimension are left out — there is no `unknown` bucket.

`SEARCHD_EXPANSION_SEARCH=<n>` overrides the HNSW search beam after load (unset = the index's
own value, 64). usearch searches at least `k` deep, so values ≤ 100 do nothing for the
`CANDIDATES = 100` search.
`embed_ms` dominates: the index was never the slow part, the OpenAI embed is.

Regression suite: `python3 test_search.py` (needs a running service).
