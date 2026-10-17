# Linting sources

`meowlaw lint <file or directory>` checks source files before they are
ingested and says what will go wrong. It reads files with the same readers as
the ingester, so what it reports about structure is what ingestion will see.

```
meowlaw lint sources/                       # every file under sources/
meowlaw lint act.pdf --kind legislation --profile au
meowlaw lint notes.txt --fix                # apply safe fixes (keeps notes.txt.bak)
meowlaw lint sources/ --min warning --json
meowlaw lint --rules                        # every rule id with a description
```

Severities:

- **error**: ingestion will fail or produce badly wrong output. The command
  exits non-zero.
- **warning**: ingestion works but the result is degraded (missed headings,
  lost numbers, junk in the text).
- **info**: worth knowing; usually harmless.

The profile and kind default to the file's own metadata (front matter,
sidecar, `corpus.toml`), then `generic`.

## What it checks

| Area | Examples |
| --- | --- |
| Encoding | UTF-16, Windows-1252 or Latin-1 read as UTF-8, mojibake, byte order marks |
| Characters | NUL and control characters, soft hyphens, zero-width characters, ligatures, symbol-font glyphs, replacement characters |
| Layout | Mixed or CR-only line endings, very long lines, words split across lines |
| Page furniture | Page numbers, "Page X of Y", running headers and footers |
| Tables | Column layouts that will be read as tables, tables flattened into text, Markdown tables with the wrong column count |
| Markdown | Heading level jumps, `#Heading` without a space, underlined headings, unclosed code fences, broken links |
| Metadata | Unknown keys (with the likely intended key), bad values, unknown profiles, YAML front matter where TOML is expected |
| Structure | No headings, numbering that goes backwards, duplicate numbers, empty or very large provisions, headings the profile does not match, unbalanced brackets |
| Cases | No numbered paragraphs, gaps large enough to merge paragraphs, numbering that restarts |
| PDF | No text layer (scanned), encrypted, two-column layout, garbled extraction, paragraph numbers the reader would drop |
| DOCX | Tracked changes, comments, Word auto-numbering (numbers that will not reach the text), no heading styles, text boxes, hidden text |
| HTML, XML, JSON | Nothing readable, text outside the blocks the reader keeps, unknown XML schema or entities, invalid JSON |

PDF checks need `pdftotext` and `pdfinfo` (poppler-utils).

## Fixes

`--fix` applies only changes that keep the wording, to `.txt` and `.md`
files: removing soft hyphens, zero-width characters and page furniture,
normalising unusual spaces and line endings, and joining words split by a
line-end hyphen (kept hyphenated when the hyphenated form appears elsewhere in
the text). A bare number is removed as a page number only when it stands
alone between blank lines, since it may be a paragraph number.

## Where else it runs

- `meowlaw sync` lints every changed source and puts the error and warning
  counts and the worst findings in the sync report (`--no-lint` skips it).
- The ingest page shows the full lint report in **Preview**.
