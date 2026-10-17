# Search

Two modes, both available in the UI, the CLI (`--mode`), REST (`mode=`) and the
MCP `search` tool.

## Basic

Tantivy BM25 over three fields: the node heading (boost 3), its ancestor
headings plus the corpus title, aliases and citations (boost 0.5), and the text.
All terms are required first; if that returns fewer than the limit, any-term
matches fill the rest. Compound numbers (`8-1`, `2016/679`) are searched as
phrases.

Text fields use Tantivy's English stemmer (`en_stem`), so "stamping" finds
"stamp". Tantivy ships stemmers for other languages; changing the analyser in
`crates/meowlaw-search/src/index.rs` rebuilds the index automatically on the
next start.

## Enriched (default)

Several evidence lists, fused with reciprocal rank fusion
(`score = Σ weight / (30 + rank)`), then an authority multiplier:

| List | Source | Weight |
|---|---|---|
| `citation` | Citations recognised in the query and resolved (`s 4 WA 2024`, `Article 6(1) GDPR`, `[2024] HCA 1`). Bare provision numbers are tried in the filtered corpus, or every structured corpus. | 4.0 |
| `bm25` | Basic search, top 50. Hits containing every term count 1.5x. | 1.2 |
| `definition` | Provisions defining terms that appear in the query, and the provisions those definitions point to. One vote per provision. | 0.6 |
| `graph` | Nodes cited by (or citing) the strongest candidates, and their `similar` neighbours. | 0.3 |
| `vector` | Nearest neighbours by embedding, when configured. | 0.9 |
| authority | × (1 + 0.05 · ln(1 + inbound citations)) | |

A summarised document's vector sits on its root; vector and graph hits on
that root are credited to its summary node, which is what the result shows.

Each result lists `matched_by`, so a user (or a model) can see why it is there.
The response also returns the resolved citations and the definitions found in
the query, which the UI shows as cards above the results.

## Vectors

Local models (no Python, no API key):

```sh
meowlaw models list
meowlaw models pull                         # bge-small-en-v1.5 (English, default)
meowlaw models pull multilingual-e5-small   # about 100 languages
```

`pull` downloads into `<data_dir>/models/<name>` and prints the
`[embeddings]` block to paste, including the model's pooling and any
query/passage prefixes. Similarity thresholds in `[similarity]` are
calibrated for BGE-small; recalibrate for other models (see graph.md).

Or any OpenAI-compatible endpoint:


Optional. Configure any OpenAI-compatible endpoint:

```toml
[embeddings]
url = "http://localhost:11434/v1/embeddings"   # Ollama
model = "nomic-embed-text"
# api_key_env = "OPENAI_API_KEY"
```

Then `meowlaw embed` (only nodes without a vector for that model are sent).
Vectors are stored in SQLite and scanned in memory, which is fine into the low
hundreds of thousands of nodes. Embedding sends node text to the endpoint:
use a local model for confidential material.

## Evaluating changes

Build a golden set of queries with the node IDs a practitioner expects, and
check recall@10 before and after changing weights or profiles. A harness is on
the roadmap.
