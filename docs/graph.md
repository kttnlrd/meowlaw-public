# Graph

One graph, two kinds of edge, in the `edges` table:

- **Explicit edges** from extraction: `cites`, `defines`, `uses_term`,
  `contains`, plus manual `related` / `same_as` / `amends`. Weight 1.0.
- **Vector edges** (`similar`) from embeddings: each node's nearest
  neighbours by cosine, weighted by the score.

Nodes are tree nodes (`<corpus>:...`) and definitions (`def:<corpus>:<term>`).
The graph is used by enriched search, the MCP graph tools, the UI's graph
panel, and export.

LawKitty kept similarity in a separate table that the graph tools never read.
Here both edge types sit in one table, so `graph_neighbourhood`, `graph_path`
and `related` traverse them together and search can follow either.

## Vector edges

```sh
meowlaw embed          # embed nodes without vectors, then rebuild `similar` edges
meowlaw similarity     # rebuild `similar` edges only (after changing thresholds)
```

How they are built (`meowlaw_search::vector`):

1. Each node's text is split into chunks of at most 1,500 characters at
   paragraph, line or sentence boundaries. Each chunk is prefixed with
   `corpus title › parent heading › heading` and embedded.
2. Each node's graph vector is the normalised mean of its chunk vectors.
3. For each node, all other nodes are scored by cosine (in parallel). The
   node's own ancestors and descendants are skipped, because the tree already
   links them.
4. The top `top_k` neighbours scoring at least `threshold` are kept.
5. A **per-kind quota** then adds up to `per_kind` neighbours from each
   *other* corpus kind (cases, guidance, secondary) at a lower
   `per_kind_threshold`. That way a provision still reaches its nearest
   rulings and cases when other provisions crowd them out of the top-k.
   LawKitty's section-to-case and section-to-ruling top-5 links did the same
   job for two fixed types.
6. Each edge stores its cosine as `weight` and as evidence (`cosine 0.812`).

```toml
[similarity]          # defaults shown
top_k = 8
threshold = 0.82
per_kind = 3
per_kind_threshold = 0.76
```

Thresholds depend on the model. These suit BGE-small, whose scores bunch
high: on ITAA 1997 + 1936, a typical unrelated pair scores about 0.72 and a
node's nearest neighbour about 0.80 to 0.90. Calibrate on your own corpus by
looking at the scores in `related` output, then run `meowlaw similarity`.

Vector edges find *topical* neighbours, not legal relevance. In the AU
example, *Bendel*'s nearest vector neighbours are trust-distribution
provisions (s 97, s 98A, s 100AA ITAA 1936), while s 109D, the provision in
issue, ranks about 2,000th by cosine and is reached only through the explicit
citation edge. That is why both edge types sit in one graph.

Embedders:

- `provider = "local"`: an ONNX sentence-embedding model run in-process by
  `tract` (pure Rust). No Python, no API key, and no network after the model
  download. Tested with `BAAI/bge-small-en-v1.5` (MIT licence, 384 dimensions).
- `provider = "http"`: any OpenAI-compatible `/v1/embeddings` endpoint.

```toml
[embeddings]
provider = "local"
model = "bge-small-en-v1.5"
model_dir = "models/bge-small-en-v1.5"   # model.onnx + tokenizer.json
seq = 256
batch = 16
```

## Building it

```sh
meowlaw ingest ...          # rebuilds by default
meowlaw rebuild             # after batches ingested with --no-rebuild
meowlaw stats               # node / edge / reference / definition counts
meowlaw unresolved          # citations that found no node
```

Quality comes from three things, in this order:

1. **Coverage.** A citation can only resolve to a source you have loaded. Load
   the Acts that your cases and guidance cite.
2. **Aliases.** Add the short names people actually use to each corpus
   (`aliases`) or to the profile (`[corpus_aliases]`).
3. **Patterns.** Tune the profile until `unresolved` shows only sources you do
   not hold.

Manual links (survive every rebuild) can be added through the store API
(`Store::add_manual_edge`); a CLI and UI for this are on the roadmap.

## Querying it

| What | CLI | MCP / REST |
|---|---|---|
| Everything one hop away, both directions | `meowlaw graph neighbourhood <id> --depth 1` | `graph_neighbourhood` / `GET /api/graph/neighbourhood?id=` |
| Grouped neighbours (cites, cited by, defines, uses) | — | `related` / `GET /api/related?id=` |
| Shortest chain between two nodes | `meowlaw graph path <from> <to>` | `graph_path` / `GET /api/graph/path?from=&to=` |

Traversal ignores `contains` unless asked (`edge_kinds`), because the tree is
better read through `outline`. Paths are found undirected, and each step
reports the real direction (`forward` / `backward`) and the citation text.

## Exporting and visualising

```sh
meowlaw graph export --format graphml --out graph.graphml   # Gephi, yEd, Cytoscape
meowlaw graph export --format dot --corpus widget-act | dot -Tsvg > widget.svg
meowlaw graph export --format json --tree > graph.json      # include tree edges
```

Suggested views in Gephi: colour by `corpus`, size by in-degree (authority),
filter `kind = cites` to see the citation network, `kind = uses_term` to see
which provisions depend on which definitions.

## How search uses it

**Basic search** does not touch the graph: BM25 over headings, ancestor
headings and text.

**Enriched search** uses it twice:

1. *Expansion.* The ten strongest candidates vote for the nodes they cite
   (weight 1/rank), at half weight for the nodes that cite them, and at half
   weight times cosine for their `similar` neighbours. The top 30 voted nodes
   join the fusion as the `graph` list. The query vector separately feeds the
   `vector` list (best chunk per node).
2. *Authority.* Each candidate's score is multiplied by
   `1 + 0.05 · ln(1 + inbound citations)`, so heavily cited provisions edge
   ahead of equally relevant but uncited ones.

Both weights are in `meowlaw_search::Weights`. See [search](search.md).
