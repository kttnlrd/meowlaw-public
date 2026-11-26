# Legislation Explorer — Display Specification

**Status:** Live spec — source of truth for how every data type renders in the UI.
**Scope:** All content types, feature panels, and conditional behaviour.
**Verification:** Each section is auditable against TESTING_PLAN_V3.md procedures and the live app.

---

## 0. Global conventions (apply to all types)

### Typography
- Headings: Montserrat, `COLORS.heading`
- Body text: Lora, 15px, line-height 1.7, `COLORS.text`
- Metadata/labels: Montserrat 12px, uppercase, letter-spacing 0.3, `COLORS.textMuted`
- All colours via `COLORS` tokens (theme-driven; no hardcoded hex in components)

### Breadcrumb (all types)
`{Type label} › {identifier}` — 12px uppercase muted, margin-bottom 20.

### Markdown rendering (all markdown-bodied types)
- `remark-gfm` + `rehype-raw` (tables, strikethrough, raw HTML)
- h1: mobile 20 / desktop 22, bottom border
- h2: mobile 17 / desktop 18; h3: mobile 15 / desktop 16
- `p` margin-bottom 12; `blockquote` 3px left border, muted
- `ul/ol` margin-left 20, `li` margin-bottom 4
- `table`: full-width, bordered, `thead` surfaceHover background
- Internal links intercepted: `/itaa-1997/sX#Y` → `onNavigate`, `/rulings/X` → `onNavigateRuling`
- Defined-term links: dashed underline → DefinitionPopover

### Mobile (<768px)
- h1 20px (vs 22), body 15px
- Tree: min-height 40px (vs 28px), reduced indent, word-wrap on
- Content padding `16px 12px 24px`
- Sidebar bottom buttons stack vertically

---

## 1. Primary Legislation Sections
**Acts:** itaa-1997, itaa-1936, taa-1953, gst-1999, corporations-act-2001, aml-ctf-2006, aml-ctf-rules-2007, nz-it-2007

### Sidebar tree
- Hierarchy: Part → Division → Subdivision → Section
- Normal legislation ordering: **id first** → `— {title}`
- Part nodes: uppercase label `Part {id} — {title}` (signpost style)
- Active section: `rgba(39,158,136,0.12)` background highlight

### Content area
1. **Breadcrumb:** `{act} › Part {part} › Division {division}` + Pin/Unpin button (accent when pinned)
2. **Body:** markdown per global conventions
3. **Comments** — collapsible `▲/▼`, count in heading, add form (author + textarea + Post), comment cards with author/date/text/Resolve
4. **SmartLinkPanel "Related"** — 5 collapsible groups, all default-closed, max 10 items each:
   - Sections (Same Act / Cross-Act, `s{id} — {title}`)
   - Rulings (`{title || citation}`)
   - Defined Terms (`{term} — defined in {section}`)
   - Cases (grouped by court: HCA → FCAFC → FCA → AATA → ARTA → others, `**{citation}** — {title}`)
   - Commentary (`{heading_title}`, `{publication} ¶{paragraph_number} — {chapter_title}`)

### Section 995-1 / 6(1) / 195-1 (dictionary sections)
- Currently: full 310KB markdown body rendered inline (ITAA 1997 s 995-1)
- **Target (per bug #4):** expandable definition tree — term list, per-term fetch, no inline 310KB dump

---

## 2. Double Tax Agreements
**Scope:** 42 countries, each `data/treaties/{country}/`

### Sidebar tree
- Country → Article nodes
- Article node: `{title}`, slug path retained for routing
- Slug-like IDs hidden from display

### Content area
1. **Breadcrumb:** `{country} › Article {articleId}`
2. **Header:** h1 article title (only if present)
3. **Body:** markdown, frontmatter stripped (regex `/^---\n[\s\S]*?\n---\n?/`)
4. **No panels below body**

---

## 3. ATO Rulings (incl. ATO IDs)
**Scope:** 11,930 files — TD/TR/PCG/PS LA rulings + AID_ files

### Sidebar tree
- Ruling-style ordering: **title first** → id (smaller, opaque)
- Citations resolve under `rulings` act

### Content area
1. **Breadcrumb:** `Ruling › {citation}`
2. **Header:** h1 `{fm.title || citation}` + descriptive title if different + **Download** button (accent, `/api/ruling/{citation}/download`)
3. **Metadata panel (AI Summary)** — bordered box, ONLY when `type !== 'ATO ID'` and any field exists. Order:
   - Status
   - Subject
   - Question
   - Decision
   - Background
   - Ruling
   - Notice (yellow `#fff8e1` box)
   - Legislation (ul)
   - Cases (ul)
   - ATO URL (`View on ATO website ↗`)
4. **TOC ("Contents")** — only when `##`/`###` headers exist; h2/h3 get slug anchor IDs; nested indent for h3
5. **Body:** markdown
6. **Referenced Sections** — `{shortActName} s{section} — {title}` links
7. **Related Cases** — deduplicated by citation, `{title || citation} ({year})`

### ATO ID conditional
- Metadata panel **hidden** for ATO IDs — full body renders instead

---

## 4. Tax Cases
**Scope:** 6,167 HTML files (HCA, FCA, FCAFC, AATA)

### Sidebar tree
- Court → Year → Case hierarchy (mapped through TreeNode: court=Part, year=Division, case=Section)
- Case ordering: title first → citation

### Content area
1. **Breadcrumb:** `Tax Case › {title || citation}`
2. **Header:** h1 `{title} — {citation}` (or citation only)
3. **Metadata rows** (dark `rgba(0,0,0,0.15)` rows, label:value, conditional per field) in order:
   Citation → Court → Decision Date → Judges → Outcome → Catchwords → Paragraphs → Content Length (`N.N KB`) → Cited By
4. **External links row:**
   - View on AustLII (accent-filled)
   - View on HCA (outlined) — when URL exists
   - View on FedCourt (outlined) — when URL exists
   - Download HTML (outlined)
5. **Case Summary** (from `/static/cleaned/summaries/{safe}.json`): Facts → Issues → Held → Reasoning → Outcome → Cases Cited (clickable) → Legislation Cited (clickable)
6. **Related Provisions** — parsed legislation links
7. **Section References** — `{act} s {section}` links
8. **Related Rulings** — clickable ul

---

## 5. Regulatory Guides (ASIC RGs)

### Content area
1. **Breadcrumb:** `ASIC Regulatory Guide › {citation}`
2. **Header:** h1 `{citation} — {title}` + status badge
   - Active: green (`#059669` / `#ecfdf5`)
   - Withdrawn/unavailable/no_pdf: amber (`#b45309` / `#fffbeb`)
3. **Metadata panel:** Status → Last updated → Download PDF button (if `hasPdf && downloadUrl`) → View on ASIC website + direct PDF link
4. **Body:** plain pre-wrap text (NO markdown), toggle "Show full text" / "Hide full text", only if non-empty
5. **Structured summary panel** (if `hasSummary`): Subject → Background → ASIC position → Cases referenced → Legislation referenced
   - If `!hasSummary`: dashed-border placeholder (withdrawn message or "Structured summary not yet...")

---

## 6. Commentary
**Scope:** master-tax-guide, master-tax-examples, master-gst-guide

### Content area
- Same rendering path as Primary Legislation (SectionContent)
- SmartLinkPanel "Commentary" group shows: `{heading_title}` + `{publication} ¶{paragraph_number} — {chapter_title}`
- Acts listed under "Australian Tax" in act picker

---

## 7. Insolvency (Keays Textbook)

### Content area
- Chapter structure via TreeNode (slug IDs like `ch-01`, `topic-03` hidden)
- Body: markdown per global conventions
- YAML frontmatter stripped by API

---

## 8. Definitions Index

### API
- `data/definitions.json` maps term → section/anchor
- `/api/definitions/{act}` serves full index

### Frontend
- Inline popover (DefinitionPopover): dashed-underline term → click → term name (Montserrat 14 bold) + definition text (Lora 13, max-height 240px scroll) + "Go to definition →"
- Definition section links: bold+italic defined terms in non-dictionary sections link to the dictionary section
- **Target (bug #4):** s 995-1 / s 6(1) / s 195-1 render as expandable tree, not 310KB dump

---

## 9. Smartlinks / Cross-References

### Legislation refs
- Same-act: `s 8-1(2)` → `/itaa-1997/s8-1(2)`
- Cross-act: `GST Act s 9-5` → `/gst-1999/s9-5`
- Subsection: `s 6-5(1)(a)` → `/itaa-1997/s6-5(1)(a)`
- Schedule refs: `Schedule 1, item 1`

### Ruling refs
- `/rulings/{citation}` intercepted → onNavigateRuling

### Case refs
- `/tax-cases/case/{citation}` → onNavigateCase

---

## 10. Search

### Type filter tabs (SearchPanel)
- All / Sections / Rulings / Cases / Commentary
- Active tab: accent background, white text; inactive: surface
- **Gap:** treaty + regulatory-guides + insolvency not yet in filter tabs

### Result badges
- `case` → purple `#8B5CF6` "Case"
- `ruling` → amber `#F59E0B` "Ruling"
- `commentary` → green `#10B981` "Comm"
- default → section style

### Results
- `{act}/{section}` + `{title}` + snippet; ruling results labelled "Ruling"
- Cross-act ranking: Best match / By section / By act
- Exact section-number queries (`8-1`) bubble match to rank 1

---

## 11. Feature panels (cross-type)

| Feature | Behaviour |
|---------|-----------|
| Pin | Split view, max 2 pins, persist localStorage, mobile = tabs at top |
| Comments | Collapsible, unresolved count badge, resolve, show/hide resolved |
| Knowledge graph | Ruling / case / section nodes, `type: ruling|c|section` |
| Keyboard | `j`/`k` next/prev section, `n` toggle commentary, `p` pin |
| Sign-in | Azure AD, sidebar bottom bar |

---

## 12. Known deviations from spec (open bugs)

| ID | Type | Deviation | Severity |
|----|------|-----------|----------|
| TRTY-001 | Treaty | MCP `get_treaty_article` includes raw YAML frontmatter in content (42 countries) — frontend strips it, MCP does not | Medium |
| ROUTE-001 | All | `s`-prefixed section URLs (`/itaa-1997/s8-1`, definition links `/itaa-1997/s995-1#...`) 404 — URL→state parser passes `s8-1` verbatim to `/api/section`; backend doesn't strip. Tree clicks work (bare ids); direct/copied links break | **High** |
| ROUTE-002 | Treaty | Direct article URLs (`/usa/article-03-general-definitions`) render blank — backend `/api/treaties/{country}/article/{article}` expects integer; URL parser sends slug. Tree clicks work | **High** |
| DEF-001 | Definitions | "Defined Terms" group in Related panel never renders — `get_section_references` reads raw markdown without running `link_definitions()` (acts.py:104), so its `*term*` regex only finds subsection markers, 0 definitions | **High** |
| RUL-001 | Rulings | Normal rulings (CR/TR/TD) return no body — summary-only by design (backend comment); full text via Download. Spec section 3 updated to match | By design |
| #4 | Definitions | s 995-1 / s 6(1) / s 195-1 render 310KB inline dump instead of expandable tree | Medium |
| DEF-002 | Definitions | `truncated` flag inconsistent — 52-char cross-ref "deduct" flagged truncated=True | Low |
| SEARCH-001 | Search | treaty/RG/insolvency types missing from filter tabs (only section/ruling/case/commentary) | Low |
| UI-001 | Spec act | Spec breadcrumb shows empty `DIVISION` segment (`SPEC › PART 0 › DIVISION`) | Cosmetic |
| UI-002 | Treaty | Tree shows slug `usa` as node id ("usa — United States of America") — spec says slug-like ids hidden | Cosmetic |
| UI-003 | Treaty | Article heading duplicated — TreatyContent renders h1 from article title AND the markdown body contains its own `# Article N — Title` heading | Cosmetic |

*Spec generated 2026-08-14. Audit 2026-08-14: 11 types assessed — 2 high-sev route bugs, 1 high-sev definitions bug, rest per spec.*
