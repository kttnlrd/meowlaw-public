#!/usr/bin/env python3
"""Ingest Corporations Act 2001 from finreg.db into legislation-explorer format.

Reads provisions from the existing SQLite DB and produces:
  - data/corporations-act-2001/tree.json
  - data/corporations-act-2001/sections/{part}/{division}/{section}.md

Usage:
    python3 scripts/ingest_corps_act.py [--db PATH] [--out-dir PATH]
"""

from __future__ import annotations
import os

import argparse
import json
import re
import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SOURCE_DB = os.path.expanduser(os.environ.get("ASIC_FINREG_DB", "~/projects/archived/asic-scraper/finreg.db"))
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "corporations-act-2001"
SECTION_DIR = OUT_DIR / "sections"

# Normalisation
_EM_DASH = "\u2014"
_HAIR_SPACE = "\u200a"
_NBSP = "\u00a0"
# E-e: the two non-ASCII hyphens.  FTS5's unicode61 tokenizer treats both as
# *separators*, so the quoted-phrase LIKE path in search_service.py misses them:
# '"sub-fund"' returned 0 rows while '"sub\u2011fund"' returned 206 (CDN-0124's
# companion ticket E-e).  Map both to ASCII '-'.
_HYPHEN_NB = "\u2011"   # NON-BREAKING HYPHEN
_HYPHEN_FIG = "\u2010"  # HYPHEN (figure dash-width form)


def clean(text: str) -> str:
    text = text.replace(_EM_DASH, "—")
    text = text.replace(_HAIR_SPACE, " ")
    text = text.replace(_NBSP, " ")
    text = text.replace(_HYPHEN_NB, "-").replace(_HYPHEN_FIG, "-")

    # Fix double-encoding: UTF-8 bytes stored as Latin-1 decoded chars
    # These appear in different chapters depending on PDF source encoding
    _fixes = {
        "\u00e2\u0080\u0099": "'",   # RIGHT SINGLE QUOTATION MARK (')
        "\u00e2\u0080\u0098": "'",   # LEFT SINGLE QUOTATION MARK (')
        "\u00e2\u0080\u009c": '"',   # LEFT DOUBLE QUOTATION MARK (")
        "\u00e2\u0080\u009d": '"',   # RIGHT DOUBLE QUOTATION MARK (")
        "\u00e2\u0080\u0094": "—",   # EM DASH (—)
        "\u00e2\u0080\u0093": "–",   # EN DASH (–)
        "\u00e2\u0080\u00a2": "•",   # BULLET (•)
        "\u00e2\u0080\u00a6": "…",   # HORIZONTAL ELLIPSIS (…)
        "\u00ef\u0082\u00b7": "•",  # BULLET variant (UTF-8 bytes stored as Latin-1)
    }
    for bad, good in _fixes.items():
        text = text.replace(bad, good)
    text = text.replace("\u00c2", "")  # stray Â from encoding issues

    return text


def _add_paragraph_breaks(body: str) -> str:
    """Insert blank lines between successive standalone entries (dictionary sections).

    In markdown, consecutive lines without blank lines render as a single
    paragraph.  Dictionary sections (s.9, s.761A) need blank lines between
    definition entries.

    Heuristic: when most body lines are short (<100 chars), assume it's a
    list/dictionary and insert blank lines at definition boundaries.
    Skips continuation lines ((a), (i), Note:) and lines that directly
    follow a colon-terminated line (keeps term + sub-paragraphs together).
    """
    lines = body.split("\n")
    if len(lines) < 5:
        return body

    non_empty = [l for l in lines if l.strip()]
    short = sum(1 for l in non_empty if 0 < len(l.strip()) < 100)
    if short < len(non_empty) * 0.6:
        return body  # prose section, leave as-is

    # Skip if this looks like a table (e.g. [operative], bare numbers, etc.)
    has_table_marker = any(l.strip() == "[operative]" for l in lines)
    bare_numbers = sum(1 for l in non_empty if l.strip().isdigit())
    if has_table_marker or bare_numbers > len(non_empty) * 0.15:
        return body  # table section, leave as-is

    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        is_continuation = (
            stripped.startswith("(")
            or stripped.startswith("Note:")
            or stripped.startswith("Example:")
        )

        prev_line = result[-1].strip() if result else ""
        if (
            i > 0
            and not is_continuation
            and prev_line
            and not prev_line.endswith(":")
        ):
            result.append("")

        result.append(line)

    return "\n".join(result)


# A "(1)"-style subsection marker.  Used by the stray-heading fallback in main():
# a dropped heading is always followed by one of these, a chapeau never is.
_SUBSECTION_MARKER = re.compile(r"^\(\d+[A-Z]*\)")

_INDENT_PATTERNS = [
    (re.compile(r"^\((\d+[A-Z]*)\)\s+(.*)"), 0, r"**(\1)** \2"),       # (1), (1A), (2) → bold
    (re.compile(r"^\(([ivxlcdm]{2,}|i)\)\s+(.*)"), 2, r"> > **(\1)** \2"),# (i), (ii), (iii) → double blockquote
    (re.compile(r"^\(([a-z]{1,3})\)\s+(.*)"), 1, r"> **(\1)** \2"),     # (a), (aa), (ab), (i) → single blockquote
    (re.compile(r"^\(([A-Z])\)\s+(.*)"), 3, r"> > > **(\1)** \2"),     # (A), (B) → triple blockquote
    (re.compile(r"^(Note\s*\d*:)\s+(.*)"), 1, r"> **\1** \2"),         # Note: → blockquote
    (re.compile(r"^(Example\s*\d*:)\s+(.*)"), 1, r"> **\1** \2"),      # Example: → blockquote
]


def _format_section_body(body: str) -> str:
    """Convert raw legislation text to proper markdown formatting.

    Matches the ITAA act format: subsection markers bold, paragraphs
    blockquoted, notes blockquoted.  Table sections (detected by
    [operative] marker) are left as single-spaced lines.
    """
    lines = body.split("\n")

    # Detect table sections by [operative] marker or bare number rows
    has_table = any(l.strip() == "[operative]" for l in lines)
    if not has_table:
        # Check for table-like pattern: bare numbers on their own lines
        # (e.g. "1", "2", "Item", "Nature of company" alternating)
        bare_numbers = sum(1 for l in lines if l.strip().isdigit())
        if bare_numbers >= 3:
            has_table = True
    separator = "\n" if has_table else "\n\n"

    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue

        formatted = False
        for pattern, indent, replacement in _INDENT_PATTERNS:
            m = pattern.match(stripped)
            if m:
                formatted_line = pattern.sub(replacement, stripped)
                result.append(formatted_line)
                formatted = True
                break

        if not formatted:
            result.append(stripped)

    return separator.join(result)


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _chunk_to_part_id(chapter: str) -> str:
    """Convert 'Chapter 2D—Officers and employees' to a short id like '2D'."""
    chapter = chapter.strip()
    if chapter.startswith("Schedule") or chapter.startswith("Schedule"):
        m = re.search(r"Schedule\s+(\d+[A-Z]*)", chapter)
        return f"sch{m.group(1)}" if m else "schedule"
    m = re.search(r"Chapter\s+(\d+[A-Z]*)", chapter)
    return m.group(1) if m else chapter


def _chunk_to_chapter_short(chapter: str) -> str:
    m = re.search(r"Chapter\s+(\d+[A-Z]*)\s*[—–-]\s*(.*)", chapter)
    if m:
        return f"Ch {m.group(1)} — {clean(m.group(2)).strip()}"
    return clean(chapter)


def _part_to_id(part: str) -> str:
    m = re.search(r"Part\s+([\d.]+[A-Z]*)", part)
    return m.group(1) if m else part.strip()


def _part_to_short(part: str) -> str:
    m = re.search(r"Part\s+([\d.]+[A-Z]*)\s*[—–-]\s*(.*)", part)
    if m:
        return f"Part {m.group(1)} — {clean(m.group(2)).strip()}"
    return clean(part)


def _division_to_id(div: str) -> str:
    if not div or div == "None":
        return ""
    m = re.search(r"Division\s+([\d.]+[A-Z]*)", div)
    return m.group(1) if m else div.strip()


def _division_to_short(div: str) -> str:
    if not div or div == "None":
        return ""
    m = re.search(r"Division\s+([\d.]+[A-Z]*)\s*[—–-]\s*(.*)", div)
    if m:
        return f"Div {m.group(1)} — {clean(m.group(2)).strip()}"
    return clean(div)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Corporations Act into legislation-explorer")
    ap.add_argument("--db", default=SOURCE_DB, help="Path to finreg.db")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR, help="Output directory")
    args = ap.parse_args()

    out_dir = args.out_dir.resolve()
    sections_dir = out_dir / "sections"

    print(f"Reading from: {args.db}")
    print(f"Writing to:   {out_dir}")

    conn = sqlite3.connect(args.db)
    cursor = conn.execute(
        """
        SELECT doc_id, content, metadata
        FROM documents
        WHERE type = 'provision'
          AND json_extract(metadata, '$.act') = 'Corporations Act 2001'
          AND doc_id LIKE 's.%'
        ORDER BY doc_id
        """
    )

    # Hierarchical structure
    tree_parts: dict[str, dict] = {}
    tree_divs: dict[tuple[str, str], dict] = {}
    tree_subdivs: dict[tuple[str, str, str], dict] = {}

    written = 0
    skipped = 0

    for row in cursor:
        doc_id, content, meta_str = row
        meta = json.loads(meta_str)

        chapter = meta.get("chapter", "")
        part = meta.get("part", "")
        division = meta.get("division", "")

        # Get section info from metadata
        section_number = meta.get("section_number", doc_id)  # "s.1"
        section_id = section_number.lstrip("s.")  # "1"
        section_title = clean(meta.get("section_title", ""))

        # Extract body: strip the "s.NUMBER Title" prefix from content
        # Content format: "s.1 Short title This Act may be cited..."
        section_prefix = section_number  # "s.1"
        if content.startswith(section_prefix):
            body = content[len(section_prefix):].strip()
        else:
            m = re.match(r"^s\.\d+[A-Z]*(?:-\d+)?\s+", content)
            body = content[m.end():].strip() if m else content.strip()

        # Also strip the section title from start of body if present (handles NBSP)
        raw_title = meta.get("section_title", "")
        # The act text often repeats the title as a sub-heading, so strip it twice
        for _ in range(2):
            if raw_title and body.startswith(raw_title):
                body = body[len(raw_title):].strip()
            elif raw_title:
                # Try with cleaned version
                cleaned_raw = clean(raw_title)
                if body.startswith(cleaned_raw):
                    body = body[len(cleaned_raw):].strip()
                # Try with NBSP->space
                elif raw_title.replace(_NBSP, " ") and body.startswith(raw_title.replace(_NBSP, " ")):
                    body = body[len(raw_title.replace(_NBSP, " ")):].strip()
                else:
                    break  # no match, stop trying

        # Fallback: if the body still starts with a short heading-like line
        # (e.g. "General obligations\n(1)..."), drop it — the heading is
        # already captured in the markdown title.
        #
        # CDN-0124: this fallback used to delete the *chapeau* (the opening
        # words of the section) whenever the opening words did not happen to
        # start with one of the hard-coded prefixes.  s 189's chapeau ends
        # "...provided by others If:" — there is no "If " (no trailing space),
        # so it was deleted with the other 402 real chapeaus.  A stray heading
        # is now only dropped when it *cannot* be chapeau text:
        #   * it does not end in ':', '.' or ';' (a chapeau such as
        #     "A company must not acquire shares ... except:" always does), and
        #   * the line after it is a "(1)"-style subsection marker, which a
        #     dropped heading always is followed by.
        # Re-running the old rule over finreg.db dropped 928 first lines;
        # 367 end in ':' and 36 in '.' (= 403 real chapeaus), the other 525 are
        # headings followed by "(1)" and are still dropped.
        first_line, sep, rest = body.partition("\n")
        first_line = first_line.strip()
        next_line = rest.lstrip("\n").split("\n", 1)[0].strip() if rest.strip() else ""
        if (
            first_line
            and len(first_line) < 120
            and not first_line.startswith("(")
            and not first_line.startswith("Note:")
            and not first_line.startswith("Example:")
            and not first_line.startswith("In this Act:")
            and not first_line.startswith("Where ")
            and not first_line.startswith("If ")
            and not first_line.endswith((":", ".", ";"))
            and _SUBSECTION_MARKER.match(next_line)
            and rest.strip()
        ):
            # Looks like a stray heading line — check it's followed by content
            body = rest.strip()

        # Resolve hierarchy
        chapter_id = _chunk_to_part_id(chapter)
        chapter_title = _chunk_to_chapter_short(chapter)

        part_id = _part_to_id(part)
        part_title = _part_to_short(part)

        div_id = _division_to_id(division) if division and division != "None" else ""
        div_title = _division_to_short(division) if division and division != "None" else ""

        # --- Build tree ---
        if chapter_id not in tree_parts:
            tree_parts[chapter_id] = {
                "id": chapter_id,
                "title": chapter_title,
                "divisions": [],
                "sections": [],
            }

        chap_node = tree_parts[chapter_id]
        div_key = (chapter_id, part_id)

        if part_id and div_key not in tree_divs:
            div_node = {
                "id": part_id,
                "title": part_title,
                "subdivisions": [],
                "sections": [],
            }
            tree_divs[div_key] = div_node
            chap_node["divisions"].append(div_node)

        part_node = tree_divs.get(div_key)
        subdiv_key = (chapter_id, part_id, div_id)

        if div_id and subdiv_key not in tree_subdivs:
            subdiv_node = {
                "id": div_id,
                "title": div_title,
                "sections": [],
            }
            tree_subdivs[subdiv_key] = subdiv_node
            if part_node is not None:
                part_node["subdivisions"].append(subdiv_node)

        # Section path: part-{chapter}/division-{part}/{section}.md
        rel_path = Path(f"part-{chapter_id}") / f"division-{part_id}" / f"{section_id}.md"
        sec_entry = {"id": section_id, "title": section_title, "path": str(rel_path)}

        if div_id and subdiv_key in tree_subdivs:
            tree_subdivs[subdiv_key]["sections"].append(sec_entry)
        elif part_node is not None:
            part_node["sections"].append(sec_entry)
        else:
            chap_node["sections"].append(sec_entry)

        # --- Write markdown ---
        section_file = sections_dir / rel_path
        section_file.parent.mkdir(parents=True, exist_ok=True)

        body_clean = _format_section_body(clean(body))

        fm_lines = [
            "---",
            'act: "Corporations Act 2001"',
            f'chapter: "{chapter_title}"',
            f'part: "{part_id}"',
            f'part_title: "{part_title}"',
        ]
        if div_id:
            fm_lines.append(f'division: "{div_id}"')
            fm_lines.append(f'division_title: "{div_title}"')
        else:
            fm_lines.append('division: ""')
            fm_lines.append('division_title: ""')

        fm_lines.append(f'section: "{section_id}"')
        fm_lines.append(f'section_title: "{section_title}"')
        fm_lines.append("compilation_no: 0")
        fm_lines.append('compilation_date: "2025-07-01"')
        fm_lines.append('source_pdf: "corps-act"')
        fm_lines.append("---")
        fm_lines.append("")
        fm_lines.append(f"# {section_id} {section_title}")
        fm_lines.append("")
        fm_lines.append(body_clean)
        fm_lines.append("")
        fm_lines.append("---")
        fm_lines.append("*Corporations Act 2001*")
        fm_lines.append("")

        section_file.write_text("\n".join(fm_lines), encoding="utf-8")
        written += 1

    conn.close()

    # Sort tree
    for chap_id in sorted(tree_parts.keys(), key=_natural_key):
        node = tree_parts[chap_id]
        node["divisions"].sort(key=lambda d: _natural_key(d["id"]))
        node.get("sections", []).sort(key=lambda s: _natural_key(s["id"]))
        for div in node["divisions"]:
            div["subdivisions"].sort(key=lambda s: _natural_key(s["id"]))
            div.get("sections", []).sort(key=lambda s: _natural_key(s["id"]))
            for sub in div["subdivisions"]:
                sub["sections"].sort(key=lambda s: _natural_key(s["id"]))

    tree = {
        "act": "Corporations Act 2001",
        "compilation_no": 0,
        "compilation_date": "2025-07-01",
        "parts": [tree_parts[cid] for cid in sorted(tree_parts.keys(), key=_natural_key)],
    }

    tree_file = out_dir / "tree.json"
    tree_file.parent.mkdir(parents=True, exist_ok=True)
    tree_file.write_text(json.dumps(tree, indent=2), encoding="utf-8")

    total_sections = sum(
        len(node.get("sections", []))
        + sum(len(d.get("sections", [])) for d in node["divisions"])
        + sum(len(s.get("sections", [])) for d in node["divisions"] for s in d.get("subdivisions", []))
        for node in tree["parts"]
    )

    print(f"\nSections written:  {written}")
    print(f"Sections skipped:  {skipped}")
    print(f"Chapter count:     {len(tree['parts'])}")
    print(f"Tree section count: {total_sections}")
    print(f"Tree file:         {tree_file}")


if __name__ == "__main__":
    main()