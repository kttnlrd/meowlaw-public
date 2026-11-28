"""
parse_gst1999.py — GST Act 1999 PDF-to-markdown structural parser.

Pipeline stage 2 for GST Act 1999. Reads pdftotext -layout output from
raw/*.txt and emits one markdown file per section under sections/.

Usage:
    python3 pipeline/parse_gst1999.py --raw-dir data/gst-1999/raw \
                                      --out-dir data/gst-1999/sections \
                                      --compilation-no 96 \
                                      --compilation-date 2026-01-01
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from dictionary_utils import starts_new_definition

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

RE_CHAPTER = re.compile(r"^Chapter\s+(\d+)\s*[\u2014\u2013\-]?\s*(.+)$")
RE_PART = re.compile(r"^Part\s+(\d+-\d+)\s*[\u2014\u2013\-]?\s*(.+)$")
RE_DIVISION = re.compile(r"^Division\s+(\d+)\s*[\u2014\u2013\-]?\s*(.+)$")
RE_SUBDIVISION = re.compile(r"^Subdivision\s+(\d+-[A-Z]+)\s*[\u2014\u2013\-]?\s*(.+)$")

# Section header: "9-5 Taxable supplies"
RE_SECTION = re.compile(r"^(\d+-\d+)\s+(\S.*)$")

RE_SUBSECTION = re.compile(r"^\s*\((\d+)\)\s+(.*)$")
RE_PARAGRAPH = re.compile(r"^\s+\(([a-z]{1,3})\)\s+(.*)$")
RE_SUBPARAGRAPH = re.compile(r"^\s+\(([ivx]+)\)\s+(.*)$")

RE_NOTE = re.compile(r"^\s*Note\s*\d*:\s*(.*)$")
RE_EXAMPLE = re.compile(r"^\s*Example\s*\d*:\s*(.*)$")

RE_NOISE = re.compile(
    r"^("
    r"A New Tax System \(Goods and Services Tax\) Act 1999|"
    r"\d+\s+A New Tax System \(Goods and Services Tax\) Act 1999|"
    r"Compilation No\.|"
    r"Authorised Version|"
    r"Compilation date:|"
    r"Registered:|"
    r"\d+\s*$"  # bare page numbers
    r")"
)

RE_ASTERISK_FOOTER = re.compile(r"^\*To find definitions of asterisked terms.*$")
RE_FOOTER_SEPARATOR = re.compile(r"^_{3,}$")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ParseContext:
    chapter: str | None = None
    chapter_title: str | None = None
    part: str | None = None
    part_title: str | None = None
    division: str | None = None
    division_title: str | None = None
    subdivision: str | None = None
    subdivision_title: str | None = None
    compilation_no: int = 0
    compilation_date: str = ""
    source_pdf: str = ""


@dataclass
class Section:
    number: str
    title: str
    context: ParseContext
    lines: list[str] = field(default_factory=list)

    @property
    def output_path(self) -> Path:
        part = f"part-{self.context.part}" if self.context.part else "part-unknown"
        div = f"division-{self.context.division}" if self.context.division else "division-unknown"
        return Path(part) / div / f"{self.number}.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_page_number(title: str) -> str:
    return re.sub(r"\s+\d+\s*$", "", title).strip()


def has_trailing_page_number(line: str) -> bool:
    return bool(re.search(r"\s+\d+\s*$", line))


def has_trailing_page_number_multi(lines: list[str], i: int) -> tuple[bool, int]:
    """Check if a multi-line title ends with a page number within 2 lines."""
    extra = ""
    j = i
    while j + 1 < len(lines) and j < i + 3:
        next_line = lines[j + 1]
        if not next_line.strip():
            break
        if has_trailing_page_number(next_line):
            return True, j + 1
        # If next line is a structural marker, stop
        if any(p.match(next_line) for p in [RE_CHAPTER, RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION]):
            break
        j += 1
        extra += " " + next_line.strip()
    return False, i


def is_toc_section_entry(line: str) -> bool:
    return bool(re.match(r"^\s+\d+-\d+\s+\S.*\.{3,}", line))


def is_page_header_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    # Any structural line without an em-dash (\u2014) or en-dash (\u2013) is a running header.
    has_body_dash = bool(re.search(r"[\u2014\u2013]", stripped))
    if stripped.startswith("Chapter ") and not has_body_dash:
        return True
    if stripped.startswith("Part ") and not has_body_dash:
        return True
    if stripped.startswith("Division ") and not has_body_dash:
        return True
    if stripped.startswith("Subdivision ") and not has_body_dash:
        return True
    if stripped.startswith("Section ") and re.match(r"^Section \d+-\d+$", stripped):
        return True
    # Reverse-order running headers (title first, structural element last)
    if re.search(r"Chapter \d+$", stripped):
        return True
    if re.search(r"Part \d+-\d+$", stripped):
        return True
    if re.search(r"Division \d+$", stripped):
        return True
    if re.search(r"Subdivision \d+-[A-Z]+$", stripped):
        return True
    # Running headers (truncated, often indented)
    if re.match(r"^The basic rules Chapter \d+$", stripped):
        return True
    if re.match(r"^The exemptions Chapter \d+$", stripped):
        return True
    if re.match(r"^The special rules Chapter \d+$", stripped):
        return True
    if re.match(r"^Miscellaneous Chapter \d+$", stripped):
        return True
    if re.match(r"^Interpretative provisions Chapter \d+$", stripped):
        return True
    if re.match(r"^Administration, collection and recovery Chapter \d+$", stripped):
        return True
    if re.match(r"^(Introduction|Preliminary|Using this Act|Supplies and acquisitions|Importations|Net amounts and adjustments|Registration|Tax periods|Returns, payments and refunds|GST-free supplies|Input taxed supplies|Special rules|Miscellaneous|Interpretation) Part \d+-\d+$", stripped):
        return True
    if re.match(r"^(Taxable supplies|Creditable acquisitions|Taxable importations|Creditable importations|Adjustment events|Bad debts|Who is required to be registered|How you become registered|How to work out the tax periods|What is attributable to tax periods|GST returns|Payments of GST|Refunds|GST-free supplies|Input taxed supplies|Special rules|Miscellaneous|Interpretation) Division \d+$", stripped):
        return True
    return False


def gather_title(lines: list[str], i: int, base_title: str) -> tuple[str, int]:
    """Gather continuation lines for a multi-line title."""
    title = base_title
    j = i
    while j + 1 < len(lines):
        next_line = lines[j + 1]
        if not next_line.strip():
            break
        # If next line is a structural marker or body element, stop
        if any(p.match(next_line) for p in [
            RE_CHAPTER, RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION,
            RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH,
            RE_NOTE, RE_EXAMPLE,
        ]):
            break
        if RE_NOISE.match(next_line) or RE_ASTERISK_FOOTER.match(next_line):
            break
        # Stop if next line has a trailing page number (TOC)
        if has_trailing_page_number(next_line):
            break
        # Stop at guide/table markers
        stripped = next_line.strip()
        if stripped in ("Table of Subdivisions", "Guide to this Division", "Guide to this Part", "Guide to this Chapter"):
            break
        if stripped.startswith("Table of Subdivisions") or stripped.startswith("Guide to "):
            break
        j += 1
        title += " " + stripped
    return title, j


# ---------------------------------------------------------------------------
# Body line classification
# ---------------------------------------------------------------------------

def classify_body_line(line: str) -> tuple[str, dict]:
    if not line.strip():
        return "blank", {}

    if RE_NOISE.match(line.strip()):
        return "noise", {}

    if RE_ASTERISK_FOOTER.match(line.strip()):
        return "noise", {}

    if RE_FOOTER_SEPARATOR.match(line.strip()):
        return "noise", {}

    if m := RE_SUBSECTION.match(line):
        return "subsection", {"num": m.group(1), "text": m.group(2)}

    # Subparagraph check BEFORE paragraph because (i) matches both.
    if m := RE_SUBPARAGRAPH.match(line):
        leading_ws = len(line) - len(line.lstrip())
        if leading_ws >= 10:
            return "subparagraph", {"roman": m.group(1), "text": m.group(2)}

    if m := RE_PARAGRAPH.match(line):
        return "paragraph", {"letter": m.group(1), "text": m.group(2)}

    if m := RE_NOTE.match(line):
        return "note", {"text": m.group(1)}

    if m := RE_EXAMPLE.match(line):
        return "example", {"text": m.group(1)}

    return "continuation", {"text": line.strip()}


# ---------------------------------------------------------------------------
# Markdown serialisation
# ---------------------------------------------------------------------------

# Table detection & rendering
# ---------------------------------------------------------------------------

def _wide_gaps(line: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of 3+ consecutive spaces, excluding
    leading whitespace (page-layout indentation) and trailing runs."""
    gaps: list[tuple[int, int]] = []
    start = None
    for i, ch in enumerate(line):
        if ch == " ":
            if start is None:
                start = i
        else:
            if start is not None:
                if start > 0 and i - start >= 3:
                    gaps.append((start, i))
                start = None
    return gaps


def _split_cells(line: str, bounds: list[int]) -> list[str]:
    """Split a line into cells at column boundaries, each stripped."""
    cells = []
    for j, b in enumerate(bounds):
        end = bounds[j + 1] if j + 1 < len(bounds) else len(line)
        cells.append(line[b:end].strip())
    return cells


def _looks_like_table_header(line: str) -> bool:
    """A table header row has >=2 wide gaps and >=3 non-empty cells,
    or (2-col) exactly 1 wide gap, 2 non-empty cells matching known headers."""
    gaps = _wide_gaps(line)
    if len(gaps) < 1:
        return False
    bounds = [0] + [g[1] for g in gaps]
    cells = _split_cells(line, bounds)
    n_cells = sum(1 for c in cells if c)
    if len(gaps) >= 2 and n_cells >= 3:
        return True
    # 2-column table detection: known header patterns
    if len(gaps) == 1 and n_cells == 2:
        c0 = cells[0].lower().rstrip(':')
        c1 = cells[1].lower()
        if c0 in ('item',) and c1.startswith('this term'):
            return True
    return False


def _render_table(lines: list[str], header_idx: int) -> tuple[str, int]:
    """Render a layout-aligned text table (starting at header_idx) as markdown.

    Returns (markdown_string, index_just_after_table).
    """
    header_line = lines[header_idx]
    gaps = _wide_gaps(header_line)
    bounds = [0] + [g[1] for g in gaps]
    ncols = len(bounds)

    hdr = _split_cells(header_line, bounds)

    # Two-line header: a single bare word directly above the header row
    # (e.g. "Event" / "Number  In these circumstances:  ..."). Prepend it to col 0.
    prefix = ""
    if header_idx > 0:
        above = lines[header_idx - 1].strip()
        if above and not _wide_gaps(lines[header_idx - 1]) and len(above.split()) == 1:
            prefix = above + " "
    hdr[0] = prefix + hdr[0]

    # Optional title line above (e.g. "Acquisition rules (no CGT event)")
    title = ""
    title_idx = header_idx - (2 if prefix else 1)
    if title_idx >= 0:
        # A heavily-indented line is a right-aligned header continuation, not a title.
        t_line = lines[title_idx]
        if len(t_line) - len(t_line.lstrip()) > 20:
            title_idx -= 1
        if title_idx >= 0:
            t_line = lines[title_idx]
            t = t_line.strip()
            if t and not _wide_gaps(t_line):
                if not any(p.match(t_line) for p in (
                    RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE,
                )):
                    title = t

    # Collect rows
    rows: list[list[str]] = []
    j = header_idx + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            break
        if any(p.match(line) for p in (
            RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE,
        )):
            break
        if ncols == 2:
            # 2-column tables: split each row on its own internal gap
            # (right-aligned single-digit numbers shift column positions).
            s = line.lstrip()
            rgaps = _wide_gaps(s)
            if not rgaps:
                break  # page noise (divider, footnote) — end this table half
            g = rgaps[0]
            cells = [s[:g[0]].strip(), s[g[1]:].strip()]
            if cells[0]:
                rows.append(cells)
            elif rows and cells[1]:
                rows[-1][1] = (rows[-1][1] + " " + cells[1]).strip()
        else:
            cells = _split_cells(line, bounds)
            if cells and cells[0]:
                rows.append(cells)
            elif rows:
                for k in range(len(cells)):
                    if cells[k]:
                        rows[-1][k] = (rows[-1][k] + " " + cells[k]).strip()
        j += 1

    def esc(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ").strip()

    out: list[str] = []
    if title:
        out.append(f"**{title}**")
        out.append("")
    out.append("| " + " | ".join(esc(c) for c in hdr) + " |")
    out.append("| " + " | ".join("---" for _ in range(ncols)) + " |")
    for r in rows:
        while len(r) < ncols:
            r.append("")
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out), j


def _table_start_at(lines: list[str], i: int) -> int | None:
    """If a table starts at line i (title or header), return its header index, else None."""
    if _looks_like_table_header(lines[i]):
        return i
    # Title line (optionally followed by a bare prefix word) then a header row.
    for ahead in (1, 2):
        hi = i + ahead
        if hi >= len(lines):
            break
        if _looks_like_table_header(lines[hi]):
            if all(lines[k].strip() and len(_wide_gaps(lines[k])) < 2 for k in range(i, hi)):
                return hi
    return None


def _split_table_md(md: str) -> tuple[str | None, str, str, list[str]]:
    """Split rendered table markdown into (title, header, separator, rows)."""
    lines = md.split("\n")
    title = None
    idx = 0
    if lines and lines[0].startswith("**") and not lines[0].startswith("|"):
        title = lines[0]
        idx = 1
        if idx < len(lines) and lines[idx] == "":
            idx += 1
    header = lines[idx] if idx < len(lines) else ""
    sep = lines[idx + 1] if idx + 1 < len(lines) else ""
    rows = lines[idx + 2:]
    return title, header, sep, rows


def _concat_tables(md1: str, md2: str) -> str | None:
    """Merge two tables with the same header; return None if not mergeable."""
    t1, h1, s1, r1 = _split_table_md(md1)
    t2, h2, s2, r2 = _split_table_md(md2)
    if not h1 or h1 != h2:
        return None
    parts: list[str] = []
    if t1:
        parts.append(t1)
        parts.append("")
    parts.append(h1)
    parts.append(s1)
    parts.extend(r1)
    parts.extend(r2)
    return "\n".join(parts)


def _segment_lines(lines: list[str]) -> list[tuple[str, str]]:
    """Split section lines into ('line', line) and ('table', markdown) segments."""
    segments: list[tuple[str, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        hi = _table_start_at(lines, i)
        if hi is not None:
            md, j = _render_table(lines, hi)
            segments.append(("table", md))
            i = j
        else:
            segments.append(("line", lines[i]))
            i += 1

    # Merge consecutive tables with identical headers (a single table split
    # across page breaks, with the header repeated on each continuation page).
    merged: list[tuple[str, str]] = []
    for seg in segments:
        if seg[0] == "table" and merged and merged[-1][0] == "table":
            combined = _concat_tables(merged[-1][1], seg[1])
            if combined is not None:
                merged[-1] = ("table", combined)
                continue
        merged.append(seg)
    return merged


# ---------------------------------------------------------------------------


def render_section_markdown(section: Section) -> str:
    ctx = section.context

    fm_lines = [
        "---",
        'act: "GST Act 1999"',
        f'chapter: "{ctx.chapter or ""}"',
        f'chapter_title: "{ctx.chapter_title or ""}"',
        f'part: "{ctx.part or ""}"',
        f'part_title: "{ctx.part_title or ""}"',
        f'division: "{ctx.division or ""}"',
        f'division_title: "{ctx.division_title or ""}"',
        f'subdivision: "{ctx.subdivision or ""}"',
        f'subdivision_title: "{ctx.subdivision_title or ""}"',
        f'section: "{section.number}"',
        f'section_title: "{section.title}"',
        f"compilation_no: {ctx.compilation_no}",
        f'compilation_date: "{ctx.compilation_date}"',
        f'source_pdf: "{ctx.source_pdf}"',
        "---",
        "",
        f"# {section.number}  {section.title}",
        "",
    ]

    body: list[str] = []
    current_sub: str | None = None
    current_para: str | None = None

    for seg_kind, seg_val in _segment_lines(section.lines):
        if seg_kind == "table":
            body.append("")
            body.append(seg_val)
            body.append("")
            continue

        raw_line = seg_val
        kind, data = classify_body_line(raw_line)

        if kind in ("blank", "noise"):
            continue

        if kind == "subsection":
            current_sub = data["num"]
            current_para = None
            body.append("")
            body.append(f'<a id="s{section.number}-{current_sub}"></a>')
            body.append(f"**({current_sub})**  {data['text']}")

        elif kind == "paragraph":
            current_para = data["letter"]
            anchor_id = f"s{section.number}-{current_sub}-{current_para}" if current_sub else f"s{section.number}-{current_para}"
            body.append("")
            body.append(f'> <a id="{anchor_id}"></a>')
            body.append(f"> **({current_para})**  {data['text']}")

        elif kind == "subparagraph":
            roman = data["roman"]
            anchor_id = (
                f"s{section.number}-{current_sub}-{current_para}-{roman}"
                if current_sub and current_para
                else f"s{section.number}-{roman}"
            )
            body.append("")
            body.append(f'> > <a id="{anchor_id}"></a>')
            body.append(f"> > **({roman})**  {data['text']}")

        elif kind == "note":
            body.append("")
            body.append(f"> **Note:** {data['text']}")

        elif kind == "example":
            body.append("")
            body.append(f"> **Example:** {data['text']}")

        elif kind == "continuation":
            text = data["text"]
            is_new_def = section.number == "195-1" and starts_new_definition(text)
            if is_new_def:
                body.append("")
                body.append(text)
            elif body and (body[-1].startswith("> > ") or body[-1].startswith("> ")):
                body[-1] = body[-1] + " " + text
            elif body:
                body[-1] = body[-1] + " " + text
            else:
                body.append(text)

    footer = [
        "",
        "---",
        f"*Last updated: {ctx.compilation_date} (Compilation {ctx.compilation_no})*",
        "",
    ]

    return "\n".join(fm_lines + body + footer)


# ---------------------------------------------------------------------------
# Main parse loop
# ---------------------------------------------------------------------------

def parse_volume(
    raw_text: Path,
    out_dir: Path,
    ctx: ParseContext,
    dry_run: bool = False,
) -> list[Section]:
    sections: list[Section] = []
    current: Section | None = None
    after_form_feed = False

    with raw_text.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")

        # Detect form feed
        if "\f" in line:
            after_form_feed = True
            line = line.replace("\f", "")
            if not line.strip():
                i += 1
                continue

        # Skip unconditional page-header noise after form feed
        if after_form_feed and is_page_header_noise(line):
            i += 1
            continue

        # Also strip running headers/footers that appear before a form feed
        stripped = line.strip()
        if re.match(r"^A New Tax System \(Goods and Services Tax\) Act 1999\s+\d+$", stripped):
            i += 1
            continue
        if re.match(r"^Authorised Version C\d+ registered \d{2}/\d{2}/\d{4}$", stripped):
            i += 1
            continue
        if re.match(r"^Compilation No\.\s+\d+\s+Compilation date:", stripped):
            i += 1
            continue
        if stripped == "Prepared by the Office of Parliamentary Counsel, Canberra":
            i += 1
            continue

        # For Part/Division/Subdivision lines after a form feed:
        # skip only if they repeat the current context.
        chapter_match = RE_CHAPTER.match(line)
        part_match = RE_PART.match(line)
        div_match = RE_DIVISION.match(line)
        sub_match = RE_SUBDIVISION.match(line)

        if after_form_feed:
            if chapter_match and chapter_match.group(1) == ctx.chapter:
                i += 1
                continue
            if part_match and part_match.group(1) == ctx.part:
                i += 1
                continue
            if div_match and div_match.group(1) == ctx.division:
                i += 1
                continue
            if sub_match and sub_match.group(1) == ctx.subdivision:
                i += 1
                continue
            after_form_feed = False

        # Skip TOC section listings (indented with dots and page numbers)
        if is_toc_section_entry(line):
            i += 1
            continue

        # Structural markers
        if chapter_match:
            is_toc, skip_to = has_trailing_page_number_multi(lines, i)
            if is_toc or has_trailing_page_number(line):
                i = skip_to + 1
                continue
            if current:
                sections.append(current)
                current = None
            ctx.chapter = chapter_match.group(1)
            title, i = gather_title(lines, i, strip_page_number(chapter_match.group(2)))
            ctx.chapter_title = title
            ctx.part = None
            ctx.part_title = None
            ctx.division = None
            ctx.division_title = None
            ctx.subdivision = None
            ctx.subdivision_title = None
            logging.info("Chapter %s — %s", ctx.chapter, ctx.chapter_title)
            i += 1
            continue

        if part_match:
            is_toc, skip_to = has_trailing_page_number_multi(lines, i)
            if is_toc or has_trailing_page_number(line):
                i = skip_to + 1
                continue
            if current:
                sections.append(current)
                current = None
            ctx.part = part_match.group(1)
            title, i = gather_title(lines, i, strip_page_number(part_match.group(2)))
            ctx.part_title = title
            ctx.division = None
            ctx.division_title = None
            ctx.subdivision = None
            ctx.subdivision_title = None
            logging.info("  Part %s — %s", ctx.part, ctx.part_title)
            i += 1
            continue

        if div_match:
            is_toc, skip_to = has_trailing_page_number_multi(lines, i)
            if is_toc or has_trailing_page_number(line):
                i = skip_to + 1
                continue
            if current:
                sections.append(current)
                current = None
            ctx.division = div_match.group(1)
            title, i = gather_title(lines, i, strip_page_number(div_match.group(2)))
            ctx.division_title = title
            ctx.subdivision = None
            ctx.subdivision_title = None
            logging.info("    Division %s — %s", ctx.division, ctx.division_title)
            i += 1
            continue

        if sub_match:
            is_toc, skip_to = has_trailing_page_number_multi(lines, i)
            if is_toc or has_trailing_page_number(line):
                i = skip_to + 1
                continue
            if current:
                sections.append(current)
                current = None
            ctx.subdivision = sub_match.group(1)
            title, i = gather_title(lines, i, strip_page_number(sub_match.group(2)))
            ctx.subdivision_title = title
            logging.info("      Subdivision %s — %s", ctx.subdivision, ctx.subdivision_title)
            i += 1
            continue

        if m := RE_SECTION.match(line):
            if current:
                sections.append(current)
            section_ctx = ParseContext(**vars(ctx))
            section_number = m.group(1)
            section_title = m.group(2).strip()

            # Conservative multi-line title
            while i + 1 < len(lines):
                next_line = lines[i + 1]
                if not next_line.strip():
                    break
                leading_ws = len(next_line) - len(next_line.lstrip())
                if leading_ws >= 12:
                    break
                if any(p.match(next_line) for p in [
                    RE_CHAPTER, RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION,
                    RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH,
                    RE_NOTE, RE_EXAMPLE
                ]):
                    break
                if RE_NOISE.match(next_line) or RE_ASTERISK_FOOTER.match(next_line):
                    break
                i += 1
                section_title += " " + next_line.strip()

            current = Section(
                number=section_number,
                title=section_title,
                context=section_ctx,
            )
            logging.info("        s %s — %s", current.number, current.title)
            i += 1
            continue

        # Body line belongs to current section.
        if current is not None:
            current.lines.append(line)

        i += 1

    if current:
        sections.append(current)

    if not dry_run:
        for s in sections:
            target = out_dir / s.output_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_section_markdown(s), encoding="utf-8")

    return sections


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--compilation-no", type=int, required=True)
    ap.add_argument("--compilation-date", type=str, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--volume", type=str, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level, format="%(message)s")

    total = 0
    raw_files = sorted(args.raw_dir.glob("*.txt"))
    if args.volume:
        raw_files = [f for f in raw_files if args.volume in f.name]

    for raw_file in raw_files:
        logging.info("=== Parsing %s ===", raw_file.name)
        ctx = ParseContext(
            compilation_no=args.compilation_no,
            compilation_date=args.compilation_date,
            source_pdf=raw_file.stem + ".pdf",
        )
        sections = parse_volume(raw_file, args.out_dir, ctx, dry_run=args.dry_run)
        logging.info("  -> %d sections", len(sections))
        total += len(sections)

    logging.info("Done. %d sections total.", total)


if __name__ == "__main__":
    main()
