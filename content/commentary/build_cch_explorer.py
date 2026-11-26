#!/usr/bin/env python3
"""
Convert CCH commentary JSON into corpus act format.

Produces for each publication:
  - data/{pub}/tree.json      (parts=chapters, sections=major_headings)
  - data/{pub}/sections/*.md  (one per major heading, with frontmatter)
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The shared normaliser lives in <repo>/pipeline; works run as a script or imported.
sys.path.insert(0, str(ROOT / "pipeline"))
from text_normalize import nfkc_ligatures  # noqa: E402


def _natural_key(s: str):
    """Natural sort key: '2' < '10', '83A' after '83'."""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]

# ponytail: parse rules ship, inputs/outputs never do — paths are repo-relative and
# env-overridable; OPTIONAL_DATA_DIR (default <repo>/data) is where outputs land.
INPUT_DIR = Path(os.environ.get("CCH_INPUT_DIR", ROOT / "pipeline" / "output"))
OUTPUT_BASE = Path(os.environ.get("OPTIONAL_DATA_DIR", ROOT / "data"))

PUBS = {
    "master_tax_guide.json":    {"id": "master-tax-guide",   "name": "Australian Master Tax Guide", "ch_label": "Ch", "compilation_no": 1, "compilation_date": "2026-04-01"},
    "master_gst_guide.json":    {"id": "master-gst-guide",   "name": "Australian Master GST Guide", "ch_label": "Ch", "compilation_no": 1, "compilation_date": "2026-04-01"},
    "master_tax_examples.json": {"id": "master-tax-examples","name": "Australian Master Tax Examples", "ch_label": "Topic", "compilation_no": 2, "compilation_date": "2026-04-01"},
}


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    s = re.sub(r"\s+", "-", s)
    # Collapse multiple hyphens to single
    s = re.sub(r"-+", "-", s)
    return s[:80]


def normalize_quotes(text: str) -> str:
    """Straighten Unicode curly quotes/apostrophes to ASCII, then normalise ligatures.

    E-c: the ligature pass is here because this is the one function every string the
    builder serves passes through - chapter titles (:144), heading titles (:154), sub-heading
    titles (:175) and every content block (clean_markdown_text, :45).  Restricting it to
    U+FB00-06 is deliberate: full NFKC would rewrite the superscripts and fractions in the
    formulas this corpus serves.
    """
    return nfkc_ligatures(text
            .replace('\u2018', "'").replace('\u2019', "'")
            .replace('\u201c', '"').replace('\u201d', '"'))


def clean_markdown_text(text: str) -> str:
    """Convert raw CCH text to proper markdown. Reconstructs paragraphs from line-wrapped PDF text."""
    # Normalise Unicode curly quotes to straight ASCII (matching the original build).
    text = normalize_quotes(text)
    
    lines = text.split('\n')
    result = []
    in_bullet_list = False
    current_para = []
    
    def flush_para():
        nonlocal current_para
        if current_para:
            result.append(' '.join(current_para))
            current_para = []
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # CCH uses '#' two ways: a sub-heading marker ('# Title') and a table
        # column delimiter (a line containing only '#'). Handle both before the
        # bullet/paragraph logic so they never render as markdown headings.
        if stripped == '#':
            # Standalone '#' — table column delimiter, not a heading. Drop it.
            continue
        if stripped.startswith('# '):
            rest = stripped[2:].strip()
            if rest and rest != '#':
                if re.fullmatch(r'[\d.%,$\s\-–—]+', rest):
                    # Numeric table-cell fragment (e.g. '# 0%', '# $68,009 –$85,010')
                    # — not a heading. Keep as inline content.
                    current_para.append(rest)
                else:
                    # Genuine sub-heading ('# Groups.', '# 3 Have a coordinator').
                    flush_para()
                    result.append(f'## {rest}')
            continue
        
        # Convert • bullets to markdown list items
        if stripped.startswith('•'):
            flush_para()
            in_bullet_list = True
            result.append('- ' + stripped[1:].strip())
            continue
        
        if in_bullet_list:
            if stripped.startswith('-') or stripped.startswith('•'):
                result.append('- ' + stripped[1:].strip())
                continue
            elif stripped and not stripped.startswith('•'):
                # Check if this is a continuation of the bullet or a new paragraph
                # If it starts lowercase, it's a continuation
                if stripped[0].islower() or (result and result[-1].startswith('- ') and len(stripped) < 80):
                    result.append('  ' + stripped)
                    continue
                else:
                    in_bullet_list = False
            else:
                in_bullet_list = False
        
        if not stripped:
            flush_para()
            continue
        
        # Heuristic: detect paragraph breaks in line-wrapped text
        # If current line ends with sentence punctuation and next line starts with uppercase,
        # treat as paragraph break (flush current paragraph)
        is_sentence_end = stripped[-1] in '.?!'
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ''
        next_starts_upper = next_line and next_line[0].isupper()
        
        current_para.append(stripped)
        
        if is_sentence_end and next_starts_upper:
            flush_para()
    
    flush_para()
    
    body = '\n\n'.join(result)
    # Collapse CCH inline '#' column delimiters (space-surrounded hash, not '(#)').
    body = re.sub(r'(?<![(\w])\s#\s', '  ', body)
    return body


# ── E-a: a chapter title is never derived from a file name ──────────────────
# The archive ingest took the chapter title from the PDF's FILE NAME
# (ARCHIVE_cadena-knowledge-MCP/pipeline/ingest_cch_commentary.py:249-255, and
# scripts/ingest_2025_docs.py:64-72 the same way), and 22 of the zip entry names carried
# the UTF-8 flag that the unzip step ignored and decoded as CP866 - so the bullet in
# "Dividends • Imputation System" arrived as the Cyrillic "тАв" and became a served chapter
# title, a tree part title and a section_index chapter_title.  A file name is not a source
# of a heading: the served title comes from, in order,
#   1. the PDF's own first-page heading, when the upstream record carries it
#      (`first_page_heading` / `heading` / `chapter_heading` / `content_title`);
#   2. the upstream `title`, when it is NOT a file name and holds no non-Latin script
#      (a Cyrillic run in a Latin-script guide is the CP866 signature, not a title);
#   3. the chapter number label, with a warning naming the title it refused.
# The first major heading is deliberately NOT the fallback: it is a section heading, not
# the chapter's ("Background to income tax in Australia" for Ch 01 "Introduction to
# Australian Tax System"), so using it would silently mislabel the chapter.
HEADING_FIELDS = ("first_page_heading", "heading", "chapter_heading", "content_title")
# The blocks C20_non_latin_script reports; the guide is a Latin-script publication, so any
# of these in a chapter title is a decoding signature rather than content.  Macrons
# (OECD, Māori) are Latin and stay legal.
NON_LATIN_SCRIPT = re.compile(
    r"[\u0400-\u04FF\u0500-\u052F\u0370-\u03FF\u1F00-\u1FFF"
    r"\u3040-\u30FF\u4E00-\u9FFF\u0600-\u06FF\u0590-\u05FF]")


def _is_file_name(value: str, ch: dict) -> bool:
    """True when the string is the PDF's file name rather than a heading."""
    v = value.strip()
    src = str(ch.get("source_file") or "").strip()
    if v.lower().endswith(".pdf"):
        return True
    if src and v in (src, Path(src).stem, Path(src).name):
        return True
    # "Ch 04 - Dividends • Imputation System" is the file-name shape: the ingest built it
    # by splitting the stem on " - " (ingest_2025_docs.py:69).
    if re.match(r"^(?:Ch|Topic)\s+\d+\s*[-–—]\s*\S", v):
        return True
    return False


def chapter_title(ch: dict) -> str:
    """The chapter's own heading - never a file name, never a decoding signature."""
    for key in HEADING_FIELDS:
        v = ch.get(key)
        if isinstance(v, str) and v.strip() and not _is_file_name(v, ch):
            return normalize_quotes(v.strip())
    v = ch.get("title")
    if isinstance(v, str) and v.strip() and not _is_file_name(v, ch) \
            and not NON_LATIN_SCRIPT.search(v):
        return normalize_quotes(v.strip())
    num = str(ch.get("number") or "").strip()
    print(f"  WARNING: chapter {num!r} carries no usable heading - "
          f"{str(v)[:60]!r} is a file name or a non-Latin decoding signature; "
          f"falling back to the chapter label, not the file name")
    return f"Chapter {num}" if num else ""


def build_pub(json_file: str, meta: dict):
    pub_id = meta["id"]
    pub_name = meta["name"]
    pub_dir = OUTPUT_BASE / pub_id
    sections_dir = pub_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads((INPUT_DIR / json_file).read_text(encoding="utf-8"))
    tree = {
        "act": pub_name,
        "compilation_no": meta.get("compilation_no", ""),
        "compilation_date": meta.get("compilation_date", ""),
        "parts": [],
    }
    section_index = []

    for ch in data.get("chapters", []):
        ch_num = ch.get("number", "")
        ch_title = chapter_title(ch)
        part_id = f"ch-{ch_num}" if ch_num else slugify(ch_title)

        part = {
            "id": part_id,
            "title": f"{meta['ch_label']} {ch_num} — {ch_title}" if ch_num else ch_title,
            "sections": []
        }

        for mh in ch.get("major_headings", []):
            heading_title = normalize_quotes(mh.get("title", ""))
            para = mh.get("paragraph_number", "")
            sec_id = slugify(heading_title) or f"{part_id}-{len(part['sections'])}"

            # Build markdown content
            md_lines = []
            if para:
                md_lines.append(f"# {heading_title} {para}\n")
            else:
                md_lines.append(f"# {heading_title}\n")

            for cb in mh.get("content_blocks", []):
                if cb.get("text"):
                    cleaned = clean_markdown_text(cb["text"])
                    md_lines.append(cleaned)
                    md_lines.append("")
                if cb.get("section_refs"):
                    md_lines.append(f"*Refs: {', '.join(cb['section_refs'])}*")
                    md_lines.append("")

            for sh in mh.get("sub_headings", []):
                md_lines.append(f"## {normalize_quotes(sh.get('title', ''))}\n")
                for cb in sh.get("content_blocks", []):
                    if cb.get("text"):
                        cleaned = clean_markdown_text(cb["text"])
                        md_lines.append(cleaned)
                        md_lines.append("")

            md_body = "\n".join(md_lines).strip()
            sec_path = f"{sec_id}.md"

            # Frontmatter
            frontmatter = f"""---
act: "{pub_name}"
part: "{ch_num}"
section: "{sec_id}"
title: "{heading_title}"
paragraph: "{para}"
---
"""
            md_file = sections_dir / sec_path
            md_file.write_text(frontmatter + md_body, encoding="utf-8")

            part["sections"].append({
                "id": sec_id,
                "title": heading_title,
                "path": sec_path,
            })
            section_index.append({
                "id": sec_id,
                "title": heading_title,
                "paragraph": para,
                "chapter": ch_num,
                "chapter_title": ch_title,
            })

        tree["parts"].append(part)

    # Sort parts and sections before writing tree.json
    tree["parts"].sort(key=lambda p: _natural_key(p["id"]))
    for part in tree["parts"]:
        part["sections"].sort(key=lambda s: _natural_key(s["id"]))

    # Write tree
    (pub_dir / "tree.json").write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")

    # Write section index for search
    (pub_dir / "section_index.json").write_text(json.dumps(section_index, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  {pub_id}: {len(tree['parts'])} parts, {len(section_index)} sections")


def main():
    for json_file, meta in PUBS.items():
        print(f"Building {meta['name']}...")
        build_pub(json_file, meta)

    print(f"\nOutput in: {OUTPUT_BASE}")
    print("Publications ready for explorer:")
    for meta in PUBS.values():
        print(f"  - {meta['id']}")


if __name__ == "__main__":
    main()
