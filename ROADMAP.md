# Roadmap

What exists today is in [README.md](README.md). This is what is still needed,
in rough priority order for an open-source release.

## 1. Before a public release

- **Clean repository.** Publish `meowlaw/` as a new repository with fresh
  history. Do not open-source a repository whose history contains licensed
  third-party text.
- **Licence.** Decided: AGPL-3.0-or-later, with a commercial licence offered
  alongside it for users who cannot meet the network clause. The relicence is
  applied across LICENSE.md, Cargo.toml, NOTICE, README, CONTRIBUTING and this
  file. Remaining: settle the commercial terms in the NOTICE contact.
- **Project files.** CONTRIBUTING, CODE_OF_CONDUCT, SECURITY (vulnerability
  reporting), issue templates, and a contributor licence agreement (needed so you can
  grant commercial licences over code others contribute).
- **Profile review process.** Built-in profiles beyond `generic` should be
  reviewed by a lawyer qualified in that jurisdiction; mark unreviewed ones
  "starter" as `de` is now.
- **Release binaries** for Linux, macOS and Windows from CI; a container image.

## 2. Law-specific gaps

- **Commencement and amendment history.** Versions, `as_at` search and
  compare exist ([versions](docs/versions.md)). Still missing: commencement,
  application and repeal dates per provision, and `amends` edges from
  amending Acts.
- **Pinpoint nodes.** Split provisions into subsection/paragraph nodes so
  `s 8-1(1)(a)` resolves to the exact unit, not the section.
- **Citator signals.** Classify how a case treats what it cites (applied,
  followed, distinguished, overruled), court hierarchy, decision date, judges,
  outcome. Rank by authority using hierarchy, not only citation counts.
- **Case-to-legislation depth.** Per-paragraph "interprets section X" links
  from judgments, and reverse "cases on this provision" views.
- **Formulas and irregular PDF tables.** Tables are read from every format
  ([tables](docs/tables.md)); PDF layout detection is heuristic and merged
  cells are flattened. Formulas are kept as text.
- **OCR.** A built-in path for scanned material (`ocrmypdf`/Tesseract), with
  confidence recorded on the node.

## 3. Sources

- **Fetchers** for official open-data endpoints that publish Akoma Ntoso, USLM
  or similar structured formats, so a corpus can be pulled and refreshed by ID.
  Each fetcher must record the source's licence and respect its terms of use;
  some well-known legal databases prohibit bulk downloading.
- **Scheduled refresh.** `meowlaw sync` detects and reports changes and broken
  links; running it on a schedule against fetched sources is still to do.
- **Converters** into the canonical JSON for formats we do not read directly.

## 4. Search and graph quality

- **Evaluation harness.** Golden queries with expected node IDs, recall@k and
  MRR in CI, per profile.
- **Extraction test suites** per profile, built from real (licensed) samples.
- **Incremental extraction** instead of whole-store rebuilds.
- **Per-corpus analysers.** Stemming is one language per store; language
  versions are aligned ([versions](docs/versions.md)) but a multilingual store
  needs an analyser per corpus language.
- **ANN index** (e.g. HNSW) once vectors pass a few hundred thousand.
- **Optional model-assisted extraction** for definitions and references that
  patterns miss, recorded as `method = model` and shown as such.
- **Manual links and corrections** from the UI, stored as `manual` edges.

## 5. Multi-user deployment

- **Per-user tokens and OAuth 2.1** for remote MCP clients.
- **Separate stores per tenant.** Per-corpus access control by token exists
  ([access](docs/access.md)); material that must not share a database still
  needs its own deployment.
- **Rate limits and usage logs.**
- **Audit and provenance.** Source file checksum and per-node content hash,
  ingestion log, and a stable citation for each answer.
- **Backups and migrations** for the SQLite schema.

## 6. UI

- Keyboard navigation, pinned results, a fuller graph explorer, issue
  reporting, applying lint fixes from the ingest page.

## 7. Port from the LawKitty codebase

The existing Python service has Australia-specific parsers (ITAA table
repairs, ruling ingestion, treaty article handling) and domain tools
(private-ruling search, proposed law, quotes). Port what generalises into
profiles or ingest readers; keep firm- or licence-specific pieces out of the
open-source tree.
