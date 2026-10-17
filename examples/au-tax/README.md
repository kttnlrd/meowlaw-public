# Example: ITAA 1997, ITAA 1936, Bendel, TR 2018/5

Real Australian sources, converted from the LawKitty corpus. All four are
committed in `sources/` so the example runs without the LawKitty data:
`itaa-1997.json` and `itaa-1936.json` (canonical JSON), `tr-2018-5.md`,
`bendel-2026-hca-18.md`. `convert_lawkitty.py` shows how the Act files were
made.

| Source | Corpus id | What is ingested |
|---|---|---|
| Income Tax Assessment Act 1997, compilation 266 (2026-07-01) | `itaa-1997` | 4,649 sections under Parts, Divisions and Subdivisions |
| Income Tax Assessment Act 1936 (2026-04-01) | `itaa-1936` | 993 sections |
| TR 2018/5 *Income tax: central management and control test of residency* | `tr-2018-5` | Ruling text as held by LawKitty (as at 2026-04-01) |
| *Commissioner of Taxation v Bendel* [2026] HCA 18 | `2026-hca-18` | LawKitty's machine-generated summary and its list of legislation cited. **Not the judgment.** |

ITAA 1936 is included because both Bendel (s 109D, Div 7A) and TR 2018/5
(s 6(1)) mainly cite it.

## Build

```sh
# 1. (Optional) regenerate the Act files from the LawKitty layout
LIC="Commonwealth of Australia, CC BY 4.0 (Federal Register of Legislation). Unofficial copy."
python3 convert_lawkitty.py ../../../data/itaa-1997 itaa-1997 "Income Tax Assessment Act 1997" sources/itaa-1997.json \
  --alias "ITAA 1997" --alias "1997 Act" --alias "Income Tax Assessment Act 1997 (Cth)" \
  --source-url https://www.legislation.gov.au/C2004A05138/latest/text --licence "$LIC"
python3 convert_lawkitty.py ../../../data/itaa-1936 itaa-1936 "Income Tax Assessment Act 1936" sources/itaa-1936.json \
  --alias "ITAA 1936" --alias "1936 Act" --alias "Income Tax Assessment Act 1936 (Cth)" --licence "$LIC"

# 2. Ingest
meowlaw ingest sources/itaa-1997.json --no-rebuild
meowlaw ingest sources/itaa-1936.json --no-rebuild
meowlaw ingest sources/tr-2018-5.md --no-rebuild
meowlaw ingest sources/bendel-2026-hca-18.md --no-rebuild
meowlaw rebuild

# 3. Local embeddings and vector edges
mkdir -p models/bge-small-en-v1.5 && cd models/bge-small-en-v1.5
curl -LO https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/onnx/model.onnx
curl -LO https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/tokenizer.json
cd ../..
meowlaw embed
```

with this in `meowlaw.toml`:

```toml
[embeddings]
provider = "local"
model = "bge-small-en-v1.5"
model_dir = "examples/au-tax/models/bge-small-en-v1.5"
```

## Things to try

```sh
meowlaw show 2026-hca-18                       # 19 citations, all resolved
meowlaw show --citation "s 109D ITAA 1936"
meowlaw define loan --corpus itaa-1936         # s 109D(3), scoped to Division 7A
meowlaw search "unpaid present entitlement private company loan"
meowlaw search "central management and control company residency"
meowlaw graph path 2026-hca-18 tr-2018-5
meowlaw unresolved --corpus tr-2018-5          # cases cited but not loaded
```

## Results (4-core container, release build)

| Step | Result | Time |
|---|---|---|
| Ingest both Acts, TR 2018/5, Bendel | 6,697 nodes | about 0.5 s per source |
| Extraction + BM25 index | 27,946 citations (84% resolved), 2,103 definitions, 55,907 edges | 1.8 s |
| Local embeddings (BGE-small, tract, no Python) | 5,656 nodes, 9,277 chunks | 12 min |
| Vector edges (all pairs, parallel) | 43,275 `similar` edges, 2,760 across corpora | 2.8 s |
| Query, warm server | basic about 18 ms; enriched (with query embedding) about 230 ms | |
| Store size | SQLite 65 MB, index 7.8 MB | |

What the links show:

- Bendel: all 19 citations resolve (s 109D and the rest of Div 7A, ss 44, 95,
  97 ITAA 1936; ss 6-25, 104-55, 960-100 ITAA 1997).
- TR 2018/5 links to s 6 ITAA 1936 (the residency definition, cited as
  "subsection 6(1)"). Its other 17 citations are cases that are not loaded.
- `define loan --corpus itaa-1936` returns s 109D(3), scoped to Division 7A.
- Vector neighbours of Bendel are trust-distribution provisions (s 97,
  s 98A, s 100AA); s 109D itself is reached only by the citation edge.
- TR 2018/5 (company residency) and Bendel (Div 7A) are on different subjects.
  `graph path` joins them only through weak `similar` edges (cosine about
  0.76 to 0.81), which is noise, not a legal connection.

## Summary and full text

- TR 2018/5 is held both ways: a summary (LawKitty's, in the front matter)
  and the full text split into its 41 numbered paragraphs
  (`tr-2018-5:para1` to `para41`). `TR 2018/5 para 12` resolves to
  `tr-2018-5:para12`, and the ruling's own "see paragraph 30 of this Ruling"
  links to `para30`.
- Bendel's file is a summary of the judgment (`content = "summary"`), so the
  case has a summary node and no paragraph text. Its page links to the High
  Court and AustLII copies of the judgment.
- Both documents' graph vectors come from their summaries with citations
  stripped.
