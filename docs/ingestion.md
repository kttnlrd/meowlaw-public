# Ingestion

`meowlaw ingest <path>` turns a file or directory into one or more corpora,
stores them, then re-runs extraction and indexing over everything (pass
`--no-rebuild` during a batch and run `meowlaw rebuild` once at the end).

Use `--dry-run` to print the tree without storing it. That is the fastest way
to tune a profile against a new source.

## Corpus kinds

| `--kind` | Use for | Tree built from |
|---|---|---|
| `legislation` | Acts, statutes, codes, constitutions | profile heading patterns |
| `instrument` | Regulations, rules made under an Act, orders | profile heading patterns |
| `rule_set` | Court rules, professional conduct rules, standards | profile heading patterns |
| `treaty` | Treaties, conventions | profile heading patterns |
| `case` | Judgments and tribunal decisions | numbered paragraphs |
| `guidance` | Regulator rulings, practice notes, guides | heading depth |
| `secondary` | Textbooks, commentary, handbooks, policies, precedents | heading depth |

## Formats

| Format | Extension | Notes |
|---|---|---|
| Markdown | `.md` | ATX headings (`#`), paragraphs, lists, tables. TOML front matter between `+++` lines. |
| Plain text | `.txt` | Short lines are offered to the profile as heading candidates. |
| HTML | `.html` | `h1`–`h6` headings; `p`, `li`, `tr`, `dd`, `blockquote`, `pre` as paragraphs. |
| XML | `.xml`, `.akn` | Akoma Ntoso (OASIS LegalDocML) and USLM. Hierarchical elements (`part`, `chapter`, `section`, `article`, ...) with `num` and `heading` children. `meta`, `toc` and notes are skipped. |
| DOCX | `.docx` | `Heading N` and `Title` paragraph styles become headings. |
| PDF | `.pdf` | Needs `pdftotext` (poppler-utils). Scanned PDFs need OCR first (e.g. `ocrmypdf`). |
| JSON | `.json` | The canonical interchange format (below). Bypasses parsing. |

Force a format with `--format`.

## Metadata

Corpus metadata is merged from, lowest priority first:

1. defaults: `id` from the file name, `title` from the first heading;
2. `corpus.toml` (directory corpus) or a sidecar `<file>.meta.toml` / `<file>.meta.json`;
3. front matter (`+++ ... +++`) in Markdown and text;
4. CLI flags (`--id`, `--title`, `--kind`, `--profile`, `--jurisdiction`, `--alias`, `--licence`, `--as-at`, `--source-url`).

```toml
id = "privacy-act-1988"          # slug; no ':' or '/'
title = "Privacy Act 1988"
kind = "legislation"
jurisdiction = "AU-CTH"          # free text; ISO 3166 + sub-code recommended
profile = "au"
aliases = ["Privacy Act"]        # names that resolve to this corpus in citations
as_at = "2025-01-01"             # compilation / point-in-time date
licence = "CC BY 4.0"
source_url = "https://..."
files = ["vol1.md", "vol2.md"]   # directory corpus: reading order
```

**Aliases matter.** Cross-corpus citations resolve through the corpus title and
its aliases. Add the short forms practitioners use ("ITAA 1997", "GDPR",
"26 U.S.C."), or put them in the profile's `[corpus_aliases]`.

## Directories

- A directory **with** `corpus.toml` is one corpus; files are concatenated in
  `files` order (or sorted by name). Use this for multi-volume Acts.
- A directory **without** `corpus.toml` is a batch: every supported file
  becomes its own corpus. Use this for a folder of judgments.

## Cases

```markdown
+++
title = "Smith v Jones"
citations = ["[2024] EXSC 1"]
jurisdiction = "XX"
as_at = "2024-09-12"
+++
Smith v Jones [2024] EXSC 1

CATCHWORDS: ...

1 This appeal concerns ...

# Consideration

2 ...
```

- Text before the first numbered paragraph (parties, catchwords, orders) stays
  on the case root.
- Paragraphs are recognised by the profile's `[cases] paragraph` pattern and
  must be sequential (a jump of up to 3 is tolerated), so a line beginning
  "2019 was..." is not mistaken for paragraph 2019.
- Headings become containers grouping the paragraphs that follow.
- If `citations` is not given, the first case citation found in the opening
  text is used, and the corpus `id` becomes its slug (`2024-exsc-1`).
- Paragraph IDs: `<case-id>:para<n>`.

## Secondary material

Textbooks, handbooks, policies and guidance are split by heading depth. In
plain text, numbered headings (`3.2 Leave`) and ALL-CAPS lines are treated as
headings. Chunks longer than 6,000 bytes are split at paragraph boundaries into
`/p2`, `/p3` children. IDs are slugs of the heading path
(`staff-handbook:leave/annual-leave`).

## Summaries and paragraphs (rulings, cases, guidance, books)

Every non-legislative corpus is held two ways at once:

1. **Summary**: a `<corpus>:summary` node. It is shown at the top of the
   document and searched like any other node. For embeddings, its citations
   are stripped (sections, cases, rulings, Act names) and the result becomes
   the vector of the corpus root, which stands for the whole document in the
   similarity graph. Two rulings are therefore "similar" because of what they
   are about, not because they cite the same sections.
2. **Full text**: every paragraph is its own node. Numbered rulings and
   judgments keep their numbers (`tr-2018-5:para12`, `2026-hca-18:para30`);
   unnumbered material is numbered in reading order (`handbook:p7`).
   Paragraphs are embedded individually for vector search, so a query can
   land on the exact paragraph, but they stay out of the similarity graph.
   Definitions and citations are extracted from the full text.

Where the summary comes from:

| Manifest | Summary node |
|---|---|
| `summary = "..."` (with `summary_source = "..."`) | that text |
| `content = "summary"` | the whole body (the file is a summary of a document not held, e.g. a judgment summary) |
| neither | the opening 1,200 characters, labelled `extract: opening text` |

Paragraph pinpoints resolve: `[2026] HCA 18 at [30]`, `TR 2018/5 para 12`,
`TR 2018/5 at [12]`, and `paragraph 30 of this Ruling` inside the ruling.

Numbered paragraphs are recognised as `N. ` at a line start or after
sentence punctuation, and must run in sequence through the document (gaps of
up to 3), so footnote numbers, list items and years are not mistaken for
paragraph numbers. Fewer than 3 such numbers means the document is treated
as unnumbered.

### Machine-generated summaries

Without a supplied summary, the summary node holds the opening text (labelled
`extract: opening text`). `meowlaw summarise` replaces those placeholders
with summaries from any OpenAI-compatible chat endpoint set under
`[summaries]` in `meowlaw.toml` (see `meowlaw.example.toml`):

- Each is labelled `machine-generated (<model>), not reviewed` in the summary's
  source, which the UI and MCP `fetch` show, and carries
  `machine_generated: true` in its metadata.
- Supplied summaries, and files that are themselves summaries, are never
  replaced.
- Results are cached by a hash of model, prompt and text, so re-running after
  a sync only calls the endpoint for documents whose text changed.
- `--dry-run` lists what would be summarised; `--corpus <id>` limits it to one
  source. With `auto = true` it runs after `meowlaw sync` and ingest-page
  uploads.

Check machine summaries before relying on them: they feed the similarity
graph, and a model can misstate what a document says.

## Canonical JSON

Any external scraper or converter can target this format. One document or an
array:

```json
{
  "corpus": { "id": "example-act", "title": "Example Act", "kind": "legislation", "profile": "generic" },
  "nodes": [
    { "id": "example-act", "corpus": "example-act", "kind": "root", "title": "Example Act", "text": "" },
    { "id": "example-act:s1", "corpus": "example-act", "kind": "provision", "label": "Section",
      "number": "1", "title": "Short title", "text": "This Act is ...", "parent": "example-act", "ordinal": 1 }
  ]
}
```

Rules (checked on ingest): node IDs unique; every node belongs to the corpus;
parents appear before children; exactly one root. Node kinds: `root`,
`container`, `provision`, `paragraph`, `chunk`, `schedule`.

## IDs

| Node | ID |
|---|---|
| Corpus root | `<corpus>` |
| Provision | `<corpus>:<abbrev><number>` (`gdpr:art5`, `widget-act:s3`) |
| Provision whose number repeats (schedules) | `<corpus>:<container path>/<abbrev><number>` (`widget-act:sch1/s1`) |
| Container | `<corpus>:<path>` (`code:ch1/pt2`) |
| Case paragraph | `<corpus>:para<n>` |
| Chunk | `<corpus>:<heading-slug>/...` |
| Definition | `def:<corpus>:<term-slug>` |

Provision IDs are stable across re-ingestion as long as the numbering is.
Chunk IDs follow headings, so renaming a heading changes them.

## Updating sources

`meowlaw sync <sources.toml>` keeps the store in line with a list of sources:

```toml
[[source]]
path = "sources/itaa-1997.json"

[[source]]
path = "sources/tr-2018-5.md"
kind = "guidance"          # any metadata field can be set here
profile = "au"
```

| Mode | Command | What happens |
| --- | --- | --- |
| Update (default) | `meowlaw sync sources.toml` | Sources whose files are unchanged since the last sync (content hash) are skipped. Changed ones are compared with the stored version, then replaced. |
| Fresh | `meowlaw sync sources.toml --fresh` | Every listed source is removed and ingested again. |
| Dry run | `meowlaw sync sources.toml --dry-run` | Reports the differences and changes nothing. |
| Force | `meowlaw sync sources.toml --force` | Re-ingests unchanged files too. |

Extraction and the search index are rebuilt once at the end, then embeddings
for new or changed nodes (skip with `--no-embed`). The report, also written to
`<data_dir>/reports/sync-<time>.md`, lists per source the provisions added,
changed and removed, citations elsewhere that will stop resolving because
their target was removed, and the citations that became unresolved or
resolved.

## Testing a source before ingesting

`meowlaw profiles test <file> [--profile au] [--kind guidance]` shows the
headings each `[[levels]]` entry matched, heading-like lines nothing matched,
citations per pattern with resolved and unresolved examples, and the
definitions found. Nothing is stored. See [profiles](profiles.md#testing-a-profile).

## Ingest page

With `allow_ingest = true` under `[server]`, `/admin.html` (linked as "Add
sources" in the header) lets you:

- upload a file and fill in its metadata,
- **Preview**: the profile test above, on the upload;
- **Check changes**: a dry-run sync against the stored version;
- **Ingest**: store it, rebuild, embed, and show the sync report;
- remove uploaded sources;
- see the most frequent unresolved citations.

Uploads are kept in `<data_dir>/uploads/` with a `<file>.meta.json` sidecar
holding the form's metadata, so `meowlaw ingest <data_dir>/uploads` rebuilds
them. Uploading a file with the same name again updates that source.

When a token is set (`server.token_env`), every ingest request needs it.
Without a token the page only works when the server is bound to localhost.
