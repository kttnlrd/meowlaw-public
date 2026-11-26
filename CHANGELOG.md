# Changelog

## 1.0.0 — 27 Nov 2026

First LawKitty release. Version numbers restart here; the 2.x and 3.x entries further down
are the history of its predecessor, Legislation Explorer.

**Sign-in and access**

- Sign in with Google, or a Microsoft work, school or personal account, alongside the emailed login code.
- Licensed commentary (CCH, Keays, NZ Master Tax Guide), procedural maps, workflows and interpretive chains are visible only to admins (`ADMIN_EMAILS`) and accounts granted them, on the website and over MCP. Gating ignores case in graph keys and act ids.
- `next` redirects are restricted to same-site paths; `/oauth/authorize` errors return 400 instead of redirecting; login codes are capped at 20 emails per address per day.
- MCP `server/discover` probes get a JSON-RPC "method not found" reply.

**Publishing**

- Source published under AGPL-3.0-or-later. Host names, paths and the default admin are configuration, not code; see `SETUP.md`.

### Corpus expansion

**New features**

- **New Zealand, browsable**: a new "New Zealand legislation" group in the sidebar with 7 NZ Acts (4,349 provisions: Income Tax 2007, GST 1985, Tax Administration 1994, KiwiSaver 2006, Student Loan 2011, Estate and Gift Duties 1968, Stamp and Cheque Duties 1971), plus a dedicated **NZ tax rulings browser** carrying 1,596 IRD rulings cross-referenced to the sections they explain.
- **AML/CTF 2026 reform, browsable**: the four instruments sit in the legislation list and each opens like any Act: AML/CTF Act 2006 at the 1 October 2026 compilation (332 sections), Rules 2025 (159), Transitional Rules 2026 (21) and the repealed Rules 2007 (606).
- **State taxes group**: 16 state Acts, 4,121 provisions across NSW, QLD, TAS, SA and WA (duties, land tax, payroll tax), each with its own tree and section view.
- **Core legislation pinned** at the top of the sources panel so the five working Acts are one click away: ITAA 1997 (4,653 sections), ITAA 1936, Corporations Act (4,163), GST Act (827), TAA 1953 (1,243).
- **Assistant (MCP) improvements**: batch fetch (`refs=[...]`) to pull several sections in one call, `as_at` point-in-time on any fetch, a compilation number and date on every section returned, subsection pinpoints, a `body_incomplete` flag that separates a cut response from text the corpus itself ends early, and `report_bad_recall` so a missed search becomes a ticket instead of a lost signal.
- **Corpus propagation**: one command pushes corpus changes to every instance (changed Acts only), rebuilding the search index and graph and restarting the service. The internal instance keeps its commentary; the public one never sees it.

**Bugs fixed**

- Search index was missing 8,629 sections the corpus holds. Rebuilt: 29,845 rows, 34,098 on the internal instance.
- Compilation drift: ITAA 1997 served 266 and FBT 1986 served 96 while the register showed 267 and 97. Every instance now matches the register.
- `fetch` reported text as truncated when the stored body itself ended early. Now reported as `body_incomplete`.
- Related material came back on every fetch and bloated responses. Now opt-in.
- A chunk request spanning a billion sections could exhaust memory. Now rejected before allocation.
- Sidebar links for rulings, NZ rulings and tax cases left the previous Act's tree on screen. Fixed, along with the stale-act state on /search.

**Verified**: integrity gate runs on all three instances, both public sites up, all 46 Acts serving.

## 3.0.2 — 19 Aug 2026

**New features** (highlights since 2.7.2 — see individual releases below for detail)

- **Search v2** — dedicated `/search` page with advanced filters (match all/any terms, date range, sort by best match / section / act, per-source checkboxes) and **shareable search URLs** (`?q=` restores and re-runs on load).
- **Private rulings browser** — year-driven sidebar with counts; 57,608 private rulings searchable by number with a dedicated browser view.
- **Graph-powered research** — search results carry a graph neighbourhood block; **path queries** (`A → B` with typed hops); **LLM serialization** endpoint for feeding graph context to AI tools; **entity resolution backstop** mapped 21,548 unparsed legislation refs across private rulings; **case citation crosswalk** for neutral citations; alias map wired into search and graph APIs (e.g. "FBTAA section 49" resolves directly).
- **Procedural maps** — 37 interactive decision-tree maps (`/maps`): CGT roll-overs & method statement, Div 7A, residency (individual/company/trust), Div 6 trust income, Part X CFC attribution, s 100A, gambling v hobby/pro, crypto loans (s 106-60), GIC & penalty remission, small business concessions; dagre flowchart with pan/zoom/pinch, node detail sheet, mobile bottom sheet; every map validated for graph reachability, verbatim statute refs, commentary slugs and definition keys (DeepSeek review pass over all 37 — two claims rejected after statute verification).
- **Legislation rendering** — pipe-table detection + GFM table rendering across all tax act parsers; **treaty tree** expandable to article level; ITAA 1936 (993 sections incl. schedules), TAA 1953 (1,243) and GST 1999 (827) fully re-parsed, stale duplicates purged, tree ↔ disk 1:1.
- **MCP / AI-assistant tools** — `resolve_alias` ("Div 7A", "s 100A", "Part IVA" → act + section), `list_issues`/`report_issue` with dedup, `get_section` now **graph-first**: related cases, public rulings with titles, **private rulings** (new), Master Tax Examples commentary and same-division sections; `get_act_tree` part scoping + pagination.
- **Data quality gates** — automated integrity verification across all 97,183 records (sections, rulings, private rulings, cases) with randomised statistical sampling (Cochran 95/5, fresh seed every run); corpus cleaned of PDF noise, HTML entities, JS chrome and inline running headers (646 lines / 450+ files).

**Performance**
- MCP responses ~36% smaller (structured output duplication removed); token caps with full-data retrieval on every capped tool (paginated `next_offset` chains, raiseable body limits).

**Bugs fixed** (notable)
- MTG commentary links dead in Related panel → now navigate to the act view.
- Dead `get_act_tree` for unknown acts → clean error + hint.
- Ruling title lookups fixed for PS LA / ATOID filename conventions.
- Version drift corrected (API now reports 3.0.2).

**Verified**
- Integrity gate: 4/4 domains pass, 0 critical (fresh seed).
- 24/24 MCP tools live-swept, all reachable; pytest suite green.

## 3.0.1 — 18 Aug 2026


**Bugs fixed**
- **CDN-0118** — s 83A-45 ITAA 1997 truncated mid-(3)(c)(iii), subsections (4)–(6) missing. Recovered full subsections (1)–(7) via TOC-dump truncation repair (190 files affected corpus-wide; 14 sections restored).
- **CDN-0119** — `[2026] HCA 18` bench reported as unanimous single set of reasons; corrected to actual 5:2 split with judges/dissent/bench_size.
- **CDN-0120** — cases_cited year-collision: `[1961] HCA 18` resolved to "Bendel [2026] HCA 18" because name resolution matched court + judgment number while ignoring year. Join corrected.
- **CDN-0123** — `list_rulings` returned the entire ruling text in the `title` field. Rulings corpus chrome-stripped (11,930 files) + titles backfilled; PR 2014/6 returns clean title + metadata.

**Data integrity pass** (all four domains, gate re-run clean)
- Sections: duplicate anchors suffixed (4,458 collisions / 609 files), TOC-dump truncation repaired, 438 junk cleanups, verifier false-positive on "Meaning of X" titles fixed.
- Rulings: JS analytics + HTML entities stripped; citation lines preserved.
- Cases: 7,359 titles backfilled from `case_name`; error-stubs treated as honest records.
- Private rulings: already clean (57,608 JSONs, 0 critical).
- 0.0% critical error rate across 97,183 records; baseline diff mode (`--baseline`) added.
- Verifier sampling randomised per run (fresh seed unless pinned with `--seed`) — successive runs exercise different files; fixed seed-42 had been masking defects.
- `trailing_junk` check made span-aware: skips legit headings/list items/inline lead-ins; flags only genuine dangling tails (corpus scan: 0 real cases). Ruling summary titles >500 chars truncated (10 TD files). Case summary titles backfilled from AustLII headings (11 ARTA files).

**New features**
- `PATCH /api/issues/{id}` — update issue status (open/known/fixed) and note; sets `fixed` timestamp on transition to fixed. Previously no update path existed (405).

**Verified**
- Integrity gate: 4/4 domains pass, 0 critical, 1,554 sampled (seed 42).
- Endpoint sweep: 9/9 healthy; `type=private-rulings` returns 5 PRs citing 106-60; 83A-45 returns subsections 1–7.

## 3.0 — 18 Aug 2026

**New features**
- **Dedicated `/search` page with advanced filters** — search moved out of the sticky header into its own route with a filter panel: match operator (All terms / Any term), date range, sort mode (best match / by section / by act), and per-source checkboxes. Sidebar Search button and landing page both route here.
- **Shareable search URLs** — the query lives in `?q=`; direct loads and back/forward restore the term and auto-run the search.
- **Private rulings browser** — year-driven sidebar (year counts, no "Division" labels); clicking a year drives the ruling list in the main pane.

**Bugs fixed**
- **Empty search page on direct load** — `/search?q=...` mounted with a blank query and never executed the search; query now hydrates from the URL and auto-searches on mount.
- **Version drift** — 2.8.2 was added to the changelog without a `VERSION` bump (API kept reporting 2.8.1). Corrected with this 3.0 cut.

**Verified**
- 40/40 REST endpoints (acts, sections, rulings, private rulings, cases, treaties, search, graph, definitions, maps, info) return 200 with correct shape.
- 24/24 MCP tools exercised end-to-end over streamable HTTP with `MCP_AUTH_TOKEN` (legacy bearer token retired → 403).
- 345 pytest tests pass, 2 skipped.
- Frontend flows: `/search` direct load, AND/OR + date filters, result → section deep-link with related-data tabs, private-rulings year navigation.

## 2.8.2 — 18 Aug 2026

**New features**
- **Graph search field** — search results now carry a `graph` neighbourhood block (per-edge-type counts + top-3 related nodes) via a materialised `neighborhood_index` (133k rows, <50ms).
- **LLM serialization endpoint** — `GET /api/graph/serialize?key=&depth=` returns token-lean blocks (80/400 budgets) for feeding graph context to LLMs; `|` separators round-trip labels containing commas.
- **Path queries** — `GET /api/graph/path?from=&to=` bidirectional BFS with typed hops, hop cap 10, frontier cap 25k (recursive-CTE approach rejected: hub blow-up).
- **Entity resolution backstop** — deterministic local resolution + DeepSeek batch mapping for 26,990 unparsed legislation refs across 57,608 private rulings; resumable checkpoints; 21,548 refs mapped, 3,295 flagged ambiguous, 13,041 unresolved (honest statuses, no phantom edges).
- **Case citation crosswalk** — `data/case_crosswalk.json` maps 34 neutral HCA citations to reporter-format graph keys (court-type + year-verified from corpus); old cases now resolve.
- **Alias map wired into runtime** — `backend/services/graph_alias.py` bridges `entity_alias_map.json` into the live service: `/api/search` probes the query + result fields and surfaces resolved keys under an `aliases` field (e.g. "FBTAA section 49" → `section:fbt-1986:49` with its graph block); `/api/graph/data` accepts `ref=` as an alternative to type+params. Keys verified against graph.db before returning — no phantoms. G6: 11 tests.

**Bugs fixed**
- **G4 phantom keys** — `_map_case_ref` marked party-name case refs as `mapped` without checking the key existed in graph.db (75 keys, 107 refs affected). Now every mapped key is verified against the graph, with neutral→reporter crosswalk fallback.
- **DeepSeek batch truncation** — 400-ref batches with `max_tokens=2000` silently truncated JSON mid-object and dropped entire batches; raised to 8000 + failure-tail logging.
- **Background interpreter mismatch** — background processes resolved `/usr/bin/python3.12` without the `openai` SDK; batch client rewritten on stdlib `urllib` (no SDK dependency).
- **tiktoken optional** — serializer no longer hard-requires tiktoken in service envs (conservative chars/3 fallback; caps never exceeded).
- **GraphModal private ruling colour** — `#e84393` for private rulings.

## 2.8.1 — 15 Aug 2026

- **Legislation table rendering** — pipe-table detection + GFM rendering added to all tax act parsers (`parse_itaa97.py`, `parse_itaa36.py`, `parse_taa53.py`, `parse_gst1999.py`). Tables now render instead of collapsing to run-on text. Threshold: 3+ columns, 2+ wide gaps.
- **Treaty tree** — treaty navigation tree now expandable to article level with article numbers and titles.
- **ITAA 1936 re-parse** — fixed `RE_PART` regex to recognise suffix-letter parts (VA, VIIB, IIIB, IVA). Re-generated `tree.json`: 13 parts, 993 sections (up from 771), schedule sections (Schedules 2D/2F/2H) now included in navigation tree. 25 sections with tables. 0 orphans.
- **TAA 1953 re-parse** — 1,243 clean sections, 56 with tables. Deleted 1,120 stale duplicate files. Tree ↔ disk 1:1.
- **GST 1999 re-parse** — 827 sections, 55 with tables. 4 stale orphans removed. Tree ↔ disk 1:1.
- **Parser guards** — section-detection guards added to `parse_itaa36.py`, `parse_taa53.py`, `parse_itaa36_schedules.py`: skip wide-gap table rows and trailing-colon TOC labels from being misclassified as section headers.
- **Server restart** — backend restarted after data changes.

## 2.8 — 2 Aug 2026

- Treaty data type added with full navigation tree.
- GST compilation footer fix (s 228).
- Deploy loop fix: don't restart when local ahead of origin.
- Display spec document + spec-list data type.
- Audit: all 11 data types checked against display spec; 10 deviations logged.
- Audit batch fixes: s-prefix URLs, treaty slugs, Defined Terms grouping.
- Social Post Composer: frontend + backend.
- Corps Act, AML/CTF, regulatory guides bulk update. Pipeline fixes.

## 2.7.3 — 1 Aug 2026

- **B23: report_issue fixed** — dedup now includes `note` in param_hash hashing (was hashing tool+params only, causing all tool reports with no params to collide). Ticket allocation now insert-first (concurrency-safe via auto-increment id on INSERT, not pre-computed MAX(id)+1). Test noise tickets (CDN-0017–CDN-0037) cleaned.
- **B1/B2: get_section payload capped** — `max_body_length` parameter (default 50K chars), `include_commentary` (default false, returns snippet+locator only). s 995-1 returns truncated preview with note to use `get_definition`.
- **B3: get_definition boundary bleeding** — hard length cap of 5000 chars after text extraction.
- **B4: 2-digit year rulings** — `TR 97/7` → `TR 1997/7` normalizer added.
- **B5: search_cases ranking** — relevance scoring: exact name/citation matches ranked first, then all-words-in-name, then partial matches.
- **B6: case_legislation_refs act attribution** — `_fix_itaa1936_act_titles()` shared helper extracted from `get_case_metadata()`; both `get_case` and `case_legislation_refs` use it.
- **B7: s 269-15(2A) truncation** — RE_NOISE extended to strip indented `*For definition…` footnote lines.
- **B8: 1936 section dedup** — 1936-act heuristic extended beyond the 109-series; de-dupes so one ref isn't emitted under two acts.
- **B11: ATO-ID legislation_referenced** — `_KNOWN_ACT_RE` regex rejects sentence fragments as act titles; dedup by `(act, section)`.
- **B12: insolvency_get_chapter pagination** — `offset`/`limit` params added, content sliced by lines.
- **New tool: resolve_alias** — resolves "Div 7A", "s 100A", "Part IVA", "Subdiv 115-C", "109Y", "8-1", etc. to act + section number with URL.
- **New tool: list_issues** — lists issues with status, patch notes (`fixed` field), and filters (status, tool, limit, max 200).
- **CDN-0002: resolve_alias** — section alias tool added.
- **CDN-0009: paragraph backfill** — pipeline for cases with content but no paragraphs.
- **CDN-0010/0016: decision_date backfill** — 187 case names fixed, hundreds of dates backfilled from summary data.
- **CDN-0011: search_cases** — relevance ranking boost for summary fields over party-name matches.
- **CDN-0012: quoted-phrase search** — `"distributable surplus"` now does exact LIKE match bypassing FTS5 stemming.
- **CDN-0013: self-citation cleanup** — self-references filtered, AustLII chrome stripped, court-code validation.
- **CDN-0015: case_name truncation** — 187 truncated case names backfilled from summary data.
- **CDN-0044: cases_cited names** — resolution path from cases table.
- **API: `/api/issues` now returns `fixed` field** — shows per-issue patch notes for resolved bugs.