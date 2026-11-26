# Legislation Explorer — Ingestion Ruleset

This document defines the mandatory conventions for adding new legislation or commentary to the explorer. Follow these rules exactly. Deviation breaks the frontend, search, or cross-reference linkers.

---

## 1. Source Material

### 1.1 Primary Legislation (Acts)
- **Source:** ComLaw / Federal Register of Legislation PDF compilations.
- **Pre-processing:** Convert to plain text with `pdftotext -layout` (preserves indentation critical for structural parsing).
- **Raw files:** Place in `data/{act-id}/raw/{volume}.txt`.

### 1.2 Commentary (CCH / Wolters Kluwer)
- **Source:** `cadena-knowledge-MCP` pipeline JSON output (`$KNOWLEDGE_MCP_DIR/pipeline/output/`).
- **Input format:** JSON with `chapters[] → major_headings[] → content_blocks[]`.
- **Pre-processing:** None — the `build_cch_explorer.py` script reads JSON directly.

---

## 2. Output Structure

Every act or publication lives under `data/{act-id}/` with exactly these files:

```
data/{act-id}/
  tree.json              # Hierarchical navigation tree
  section_index.json     # Flat index for search (CCH only; acts use FTS5)
  sections/              # One .md file per section
    part-1-1/
      division-1/
        1-1.md
        1-2.md
    ...
```

### 2.1 tree.json Schema
```json
{
  "act": "Income Tax Assessment Act 1997",
  "compilation_no": 263,
  "compilation_date": "2026-04-01",
  "parts": [
    {
      "id": "1-1",
      "title": "Preliminary",
      "divisions": [
        {
          "id": "1",
          "title": "Core provisions",
          "subdivisions": [],
          "sections": [
            {"id": "1-1", "title": "Short title", "path": "part-1-1/division-1/1-1.md"}
          ]
        }
      ]
    }
  ]
}
```

Rules:
- `id` must be URL-safe (no spaces, lowercase preferred).
- `path` is relative to `sections/`.
- Parts, divisions, subdivisions, and sections must be sorted by natural sort key (`2` before `10`, `83` before `83A`).

---

## 3. Markdown File Format

Every section is one `.md` file with **YAML frontmatter** followed by body content.

### 3.1 Frontmatter (Primary Legislation)
```yaml
---
act: "ITAA 1997"
part: "1-1"
part_title: "Preliminary"
division: "1"
division_title: "Core provisions"
subdivision: ""
subdivision_title: ""
section: "1-1"
section_title: "Short title"
compilation_no: 263
compilation_date: "2026-04-01"
source_pdf: "itaa97-vol1.pdf"
---
```

### 3.2 Frontmatter (CCH Commentary)
```yaml
---
act: "Australian Master Tax Guide"
part: "1"
section: "introduction"
title: "Introduction"
paragraph: "¶1-010"
---
```

Rules:
- All string values must be quoted.
- `part` for CCH is the chapter number.
- `paragraph` stores the CCH ¶ reference (e.g., `¶1-010`). It is used by the paragraph linker.

### 3.3 Body Markdown Conventions

**Subsections:** Bold with anchor tag.
```markdown
<a id="s6-5-1"></a>
**(1)**  The *general rule* is that you are taxed on your **ordinary income**.
```

**Paragraphs:** Blockquote with anchor tag.
```markdown
> <a id="s6-5-1-a"></a>
> **(a)**  the income must be...
```

**Subparagraphs:** Nested blockquote.
```markdown
> > <a id="s6-5-1-a-i"></a>
> > **(i)**  a payment...
```

**Notes & Examples:** Blockquote with bold prefix.
```markdown
> **Note:** This section does not apply to...
> **Example:** If you receive...
```

**Definitions:** Asterisk-wrapped terms (e.g., `*ordinary income*`) must be preserved exactly. The definition linker scans for these and links them to s995-1 or external definitions.

---

## 4. Text Cleaning Rules

### 4.1 Paragraph Reconstruction (CCH / PDF-wrapped text)
`pdftotext -layout` strips double-newlines. Apply this heuristic:

1. Split text by `\n`.
2. Accumulate lines into a paragraph buffer.
3. **Flush the buffer** (start a new paragraph) if:
   - The current line ends in sentence punctuation (`.`, `?`, `!`) AND the next line starts with an uppercase letter.
   - The line is empty.
4. Convert `•` bullets to Markdown `-` list items.
5. Collapse multiple hyphens in slugs (`---` → `-`).

### 4.2 Noise Stripping (Primary Legislation)
Discard lines matching:
- Running headers: `Income Tax Assessment Act 1997`, chapter/section names, compilation metadata.
- Footers: `*To find definitions of asterisked terms...`, `___` separator lines.
- Page numbers (bare integers at end of TOC lines).
- TOC entries: indented lines with `...` dot leaders and trailing page numbers.
- Form feed (`\f`) and content immediately after it (treat as page break noise).

---

## 5. Cross-Reference Linking

The backend applies regex linkers at serve-time. You do NOT need to insert links in the markdown source, but you MUST write text that matches these patterns so the linkers can find them.

### 5.1 Legislation Section References
Pattern: `\b(s|section)\s+(\d+[A-Z]?-[\d\(\)]+(?:\(\d+\))?)\b`

Valid examples:
- `s 6-5`
- `section 8-1`
- `s 205-5(1)`

Invalid / won't link:
- `subsection 6-5(1)` (does not match pattern)
- `s6-5` (no space)

### 5.2 CCH Paragraph References
Pattern: `((?:&para;|\u00b6)\s*(\d+-\d+))`

Valid examples:
- `¶1-010`
- `&para;1-010`

The paragraph index (`_paragraph_index`) maps `pub:para` to section slugs. If a paragraph number exists in the source JSON, the linker will resolve it.

### 5.3 Definition References
Asterisked terms in primary legislation (e.g., `*ordinary income*`) are linked by the definition linker. Definitions are extracted by `pipeline/extract_definitions.py` and stored in `data/definitions_all.json`.

---

## 6. Search Indexing

### 6.1 FTS5 Index
On startup, the backend builds `search_index.db` (SQLite FTS5) from all markdown content under `data/`.

Schema:
```sql
CREATE VIRTUAL TABLE sections_fts USING fts5(
    act, section, title, content,
    tokenize='porter'
);
```

Rules:
- If you add new content, delete `search_index.db` and restart the server to rebuild.
- The index includes both primary legislation and CCH commentary.
- BM25 ranking is used; results include highlighted snippets.

### 6.2 Section Index (CCH)
CCH publications also emit `section_index.json` for quick paragraph lookups without parsing markdown.

---

## 7. CCH Ingestion Specifics

### 7.1 Chapter Labels
- `master_tax_guide.json` → label prefix `Ch`
- `master_gst_guide.json` → label prefix `Ch`
- `master_tax_examples.json` → label prefix `Topic`

### 7.2 Slug Generation
- Slugify from the major heading title.
- Collapse whitespace and punctuation to single hyphens.
- Truncate to 80 characters.
- Fallback: `ch-{ch_num}-{index}` if title is empty.

### 7.3 Content Block Ordering
Within a major heading, emit in this order:
1. Heading (`# Title ¶1-010`)
2. Content blocks (cleaned text)
3. Section refs italic line (`*Refs: s 6-5, s 8-1*`)
4. Sub-headings (`## Sub-heading`) with their content blocks

### 7.4 NEVER derive a title from a file name (E-a, 2026-09-26)
**Rule.** A chapter, part or section title comes from the PDF's own first-page heading or
the upstream content field. It NEVER comes from the source file's name.

**Why.** `data/tax-docs.zip` in the cadena-knowledge-MCP archive stores 22 MTG filenames
containing U+2022 with the zip UTF-8 flag (0x800) set — the only 22 entries that carry it.
The unzip step ignored the flag and decoded the *names* as CP866, so the bullet in
`Ch 04 - Dividends • Imputation System.pdf` became the three Cyrillic characters `тАв`
(U+0442 U+0410 U+0432). The archive ingest then took the chapter title from that filename
(`ingest_cch_commentary.py:249-255`, and `scripts/ingest_2025_docs.py:64-72` the same way),
and the title reached the served tree, the section index and the Postgres `chapter_title`.
1,370 occurrences in 897 fields had to be repaired by a CP866 round trip
(`scripts/repair_mtg_mojibake.py`, commit `b8b6723dd`).

**How it is enforced.** `pipeline/build_cch_explorer.py:chapter_title()` takes the title
from `first_page_heading` / `heading` / `chapter_heading` / `content_title`, else from the
upstream `title` **only when it is not a file name and carries no non-Latin script**
(a Cyrillic run in a Latin-script guide is a decoding signature, not content; Latin macrons
such as `Tāwhirimātea` are legitimate). When neither is usable it prints a warning and
falls back to the chapter label — it never falls back to the file name. The same rule
applies to any new ingester: take the heading from the document, not from `Path.stem`.
Self-test: `scripts/test_e_a_chapter_title_rule.py`.

### 7.5 Normalise the ligature block, and only it (E-c, 2026-09-26)
**Rule.** Run `pipeline/text_normalize.py:nfkc_ligatures()` over every string an ingester
serves. It applies `unicodedata.normalize('NFKC', ...)` to U+FB00–U+FB06 only.

**Why.** Full NFKC also rewrites superscripts and fractions, which this corpus serves inside
formulas (`2⁵`, `½`), so a blanket NFKC would corrupt them. The ligature block alone must be
normalised because the raw readers — MCP `get_section`, the embeddings, the insolvency FTS —
do not fold: `financial` matched 9 of 21 Keays chapters while `ﬁnancial` matched 19.

---

## 8. Validation Checklist

Before declaring a new act or commentary publication ready:

- [ ] `tree.json` loads without JSON errors.
- [ ] All `sections/*.md` files have valid YAML frontmatter.
- [ ] `part`, `division`, `section` IDs are URL-safe.
- [ ] Navigation tree sorts correctly (natural sort: `2` before `10`).
- [ ] Search index rebuilds without errors (delete `search_index.db`, restart server).
- [ ] A known section reference (e.g., `s 6-5`) in CCH text renders as a clickable link.
- [ ] A known paragraph reference (e.g., `¶1-010`) in CCH text renders as a clickable link.
- [ ] Definitions in primary legislation (asterisked terms) link to s995-1 or external source.
- [ ] No bare page numbers, TOC dot leaders, or running headers appear in rendered markdown.
- [ ] Paragraph breaks are present (not a "wall of text").

---

## 9. File Naming Conventions

| Entity | Pattern | Example |
|--------|---------|---------|
| Act ID (directory) | `{act-slug}` | `itaa-1997`, `itaa-1936`, `gst-1999` |
| CCH publication ID | `master-{topic}-guide` | `master-tax-guide` |
| Section file | `{section-id}.md` | `6-5.md` |
| Section path (hierarchical) | `part-{pid}/division-{did}/{sid}.md` | `part-3-1/division-10/6-5.md` |
| Raw text | `{act}-{volume}.txt` | `itaa97-vol1.txt` |
| Tree output | `tree.json` | `data/itaa-1997/tree.json` |
| Definitions | `definitions_all.json` | `data/definitions_all.json` |

---

## 10. Pipeline Execution Order

When ingesting a new primary act:

1. **Extract:** `pdftotext -layout` PDF → `raw/*.txt`
2. **Parse:** `pipeline/parse_itaa97.py` (or equivalent) → `sections/*.md`
3. **Tree:** `pipeline/build_tree.py` → `tree.json`
4. **Definitions:** `pipeline/extract_definitions.py` → update `definitions_all.json`
5. **Index:** Restart server → `search_index.db` rebuilds automatically.

When ingesting new CCH commentary:

1. **Convert:** `pipeline/build_cch_explorer.py` reads JSON → `sections/*.md` + `tree.json`
2. **Index:** Restart server → `search_index.db` rebuilds automatically.
