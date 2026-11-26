"""
parse_taa53.py — TAA 1953 PDF-to-markdown structural parser.

Reads pdftotext -layout output from raw/*.txt and emits one markdown file
per section under sections/.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

ACT_NAME = "Fringe Benefits Tax Assessment Act 1986"  # overridden by CLI --act-name
ACT_SHORT = "FBTAA 1986"  # overridden by CLI --act-short

def _natural_key(s: str):
    """Natural sort key: '2' < '10', '83A' after '83'."""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
RE_PART = re.compile(r"^Part\s+([0-9]+[A-Z]*|[IVX]+[A-Z]?)\s*[—–\-]?\s*(.+)$")
RE_DIVISION = re.compile(r"^Division\s+(\d+[A-Z]*)\s*[—–\-]?\s*(.+)$")
RE_SUBDIVISION = re.compile(r"^Subdivision\s+([A-Z]+)\s*[—–\-]?\s*(.+)$")

# FBTAA/SIS style: "Section 5B" on its own line (left-aligned), title from TOC
RE_SECTION_MARKER = re.compile(r"^Section\s+(\d+[A-Z]*(?:-\d+)?)\s*$")

# Section header: "1 Short title" or "45-1 What this Division is about"
RE_SECTION = re.compile(r"^(\d+[A-Z]*(?:-\d+)?)\s+(\S.*)$")

RE_SUBSECTION = re.compile(r"^\s*\((\d+[A-Z]*)\)\s+(.*)$")
RE_PARAGRAPH = re.compile(r"^\s+\(([a-z]{1,3})\)\s+(.*)$")
RE_SUBPARAGRAPH = re.compile(r"^\s+\(([ivx]+)\)\s+(.*)$")

RE_NOTE = re.compile(r"^\s*Note\s*\d*:\s*(.*)$")
RE_EXAMPLE = re.compile(r"^\s*Example\s*\d*:\s*(.*)$")

RE_NOISE = re.compile(
    r"^("
    r"Taxation Administration Act 1953|"
    r"Fringe Benefits Tax Assessment Act 1986|"
    r"Superannuation Industry \(Supervision\) Act 1993|"
    r"Compilation No\.|"
    r"Authorised Version|"
    r"Compilation date:|"
    r"Registered:|"
    r"Includes amendments:|"
    r"No\.\s+\d+,\s+\d+|"
    r"\d+\s*$"  # bare page numbers
    r"|"
    r"^_+$"  # underline/page-break separators
    r"|"
    r"^\*For definition"  # footnote definition markers
    r"|"
    r"^\s*\*For definition"  # indented footnote definition markers
    r")"
)

# ---------------------------------------------------------------------------
# TOC title map
# ---------------------------------------------------------------------------
# TOC lines look like: "                     5B   Working out an employer's
# fringe benefits taxable amount ...............3" — section number, then
# title, then dotted leader + page number. Extract number -> title.
RE_TOC_ENTRY = re.compile(
    r"^\s*(\d+[A-Z]*(?:-\d+)?)\s+(.+?)\s*\.{3,}\s*\d+\s*$"
)
RE_TOC_NUMBER = re.compile(r"^\s*(\d+[A-Z]*(?:-\d+)?)\s+(.+)$")


def build_toc_title_map(raw_text: Path) -> dict[str, str]:
    """Map section number -> title from the table of contents.

    Handles wrapped entries: a number+title line followed by a
    continuation line (no number) that carries the dotted leader.
    """
    toc: dict[str, str] = {}
    try:
        lines = raw_text.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return toc
    pending: tuple[str, str] | None = None  # (number, partial title)
    for raw in lines:
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue
        if "..." in stripped or "…" in stripped:
            m = RE_TOC_ENTRY.match(line)
            if m:
                toc.setdefault(m.group(1).strip(), m.group(2).strip())
                pending = None
            elif pending:
                # continuation line: dots + page number, no leading number
                tail = re.sub(r"\s*\.{3,}\s*\d+\s*$", "", stripped).strip()
                if tail:
                    num, title = pending
                    toc.setdefault(num, (title + " " + tail).strip())
                pending = None
            else:
                pending = None
        else:
            m = RE_TOC_NUMBER.match(line)
            if m:
                pending = (m.group(1).strip(), m.group(2).strip())
            else:
                pending = None
    return toc


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class ParseContext:
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
        part = f"part-{self.context.part.lower()}" if self.context.part else "part-unknown"
        div = f"division-{self.context.division.lower()}" if self.context.division else "division-unknown"
        return Path(part) / div / f"{self.number.lower()}.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def strip_page_number(title: str) -> str:
    return re.sub(r"\s+\d+\s*$", "", title).strip()


def has_trailing_page_number(line: str) -> bool:
    return bool(re.search(r"\s+\d+\s*$", line))


def is_page_footer(line: str) -> bool:
    return ACT_NAME in line


def is_toc_section_entry(line: str) -> bool:
    return bool(re.match(r"^\s+\d+[A-Z]*(?:-\d+)?\s+\S.*\.{3,}", line))


def is_page_header_noise(line: str) -> bool:
    if not line.strip():
        return True
    stripped = line.strip()
    leading_ws = len(line) - len(line.lstrip())
    # FBTAA/SIS: a left-aligned "Section 5B" is the real section marker;
    # only right-aligned (running header at page top) is noise.
    if re.match(r"^Section\s+\d+[A-Z]*(?:-\d+)?$", stripped) and leading_ws > 20:
        return True
    if re.match(r"^Part\s+[IVX]+[A-Z]?\s+(?!—)[^—].*$", stripped):
        return True
    if re.match(r"^Part\s+\d+-[A-Z0-9]*\s+(?!—)\S", stripped):  # Part 4-15 style (Schedule 1)
        return True
    if re.match(r"^Division\s+\d+[A-Z]*\s+(?!—)[^—].*$", stripped):
        return True
    if re.match(r"^Subdivision\s+[A-Z]+\s+(?!—)[^—].*$", stripped):
        return True
    if re.search(r"Part\s+[IVX0-9]+[A-Z]?$", stripped):
        return True
    if re.search(r"Division\s+\d+[A-Z]*$", stripped):
        return True
    if re.search(r"Section\s+\d+[A-Z]*(?:-\d+)?$", stripped) and leading_ws > 20:
        return True
    # Page headers for Schedule 1 running headers
    # (handled as structural elements in main parse loop)
    # if re.match(r"^Schedule\s+\d+\s", stripped):
    #     return True
    # if re.match(r"^Chapter\s+\d+\s", stripped):
    #     return True
    if RE_NOISE.match(line.strip()):
        return True
    return False


def _continues_title(lines: list[str], i: int, structural_patterns: list, max_leading_ws: int | None = None) -> tuple[str | None, int]:
    extra = ""
    while i + 1 < len(lines):
        next_line = lines[i + 1]
        if not next_line.strip():
            break
        if any(p.match(next_line) for p in structural_patterns):
            break
        if RE_NOISE.match(next_line.strip()) or is_page_footer(next_line):
            break
        leading_ws = len(next_line) - len(next_line.lstrip())
        if max_leading_ws is not None and leading_ws >= max_leading_ws:
            break
        i += 1
        extra += " " + next_line.strip()
    return extra, i


# ---------------------------------------------------------------------------
# Markdown serialisation
# ---------------------------------------------------------------------------
def classify_body_line(line: str) -> tuple[str, dict]:
    if not line.strip():
        return "blank", {}
    if RE_NOISE.match(line.strip()) or is_page_footer(line):
        return "noise", {}
    if m := RE_SUBSECTION.match(line):
        return "subsection", {"num": m.group(1), "text": m.group(2)}
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
        'act: "' + ACT_SHORT + '"',
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
            if body and body[-1].startswith("> > "):
                body[-1] = body[-1] + " " + data["text"]
            elif body and body[-1].startswith("> "):
                body[-1] = body[-1] + " " + data["text"]
            elif body:
                body[-1] = body[-1] + " " + data["text"]
            else:
                body.append(data["text"])

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
def parse_volume(raw_text: Path, out_dir: Path, ctx: ParseContext, dry_run: bool = False, toc_titles: dict[str, str] | None = None) -> list[Section]:
    sections: list[Section] = []
    current: Section | None = None
    after_form_feed = False
    toc_titles = toc_titles or {}

    with raw_text.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")

        if "\f" in line:
            after_form_feed = True
            line = line.replace("\f", "")
            if not line.strip():
                i += 1
                continue

        if after_form_feed and is_page_header_noise(line):
            i += 1
            continue

        part_match = RE_PART.match(line)
        div_match = RE_DIVISION.match(line)
        sub_match = RE_SUBDIVISION.match(line)

        if after_form_feed:
            if part_match and part_match.group(1) == ctx.part:
                i += 1
                continue
            if div_match and div_match.group(1) == ctx.division:
                i += 1
                continue
            if sub_match and sub_match.group(1) == ctx.subdivision:
                i += 1
                continue
            # Page-top repeat of the section we're already inside
            # (continuation page breadcrumb: Part/Division/Section)
            if current is not None and (m_sec := RE_SECTION_MARKER.match(line)) and m_sec.group(1) == current.number:
                i += 1
                continue
            # Check for Schedule/Chapter page header repeats
            if (sch_match := re.match(r"^Schedule\s+\d+", line)):
                i += 1
                continue
            if (ch_match := re.match(r"^Chapter\s+\d+", line)):
                i += 1
                continue
            after_form_feed = False

        if is_toc_section_entry(line):
            i += 1
            continue

        # Structural: Part
        if part_match:
            if has_trailing_page_number(line):
                i += 1
                continue
            # Same breadcrumb rule: a part header right after a fresh
            # section marker is a running header, not a structural boundary.
            if current and any(current.lines) and part_match.group(1) != ctx.part:
                sections.append(current)
                current = None
            ctx.part = part_match.group(1)
            ctx.part_title = strip_page_number(part_match.group(2))
            extra, i = _continues_title(lines, i, [RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION, RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE])
            if extra:
                ctx.part_title += extra
            ctx.division = None
            ctx.division_title = None
            ctx.subdivision = None
            ctx.subdivision_title = None
            logging.info("Part %s — %s", ctx.part, ctx.part_title)
            i += 1
            continue

        # Structural: Schedule / Chapter (Schedule 1 parts)
        if (sch_match := re.match(r"^Schedule\s+(\d+)[—–\-]?\s*(.*)$", line)) and not has_trailing_page_number(line):
            if current:
                sections.append(current)
                current = None
            ctx.part = "SCHEDULE" + sch_match.group(1)
            ctx.part_title = strip_page_number(sch_match.group(2))
            extra, i = _continues_title(lines, i, [RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION, RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE])
            if extra:
                ctx.part_title += extra
            ctx.division = None
            ctx.division_title = None
            ctx.subdivision = None
            ctx.subdivision_title = None
            logging.info("Schedule %s — %s", sch_match.group(1), ctx.part_title)
            i += 1
            continue

        # Structural: Chapter (within a Schedule)
        if (ch_match := re.match(r"^Chapter\s+(\d+)[—–\-]?\s*(.*)$", line)) and not has_trailing_page_number(line):
            # Chapters act as part-level grouping; keep the existing part id
            if current:
                sections.append(current)
                current = None
            ch_title = strip_page_number(ch_match.group(2))
            extra, i = _continues_title(lines, i, [RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION, RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE])
            if extra:
                ch_title += extra
            # If we haven't seen a Schedule header yet, assign to SCHEDULE1
            if not ctx.part:
                ctx.part = "SCHEDULE1"
                ctx.part_title = ch_title
            # If already in a schedule, don't overwrite the part_title
            ctx.division = None
            ctx.division_title = None
            ctx.subdivision = None
            ctx.subdivision_title = None
            logging.info("Chapter %s — %s", ch_match.group(1), ch_title)
            i += 1
            continue

        # Structural: Division
        if div_match:
            if has_trailing_page_number(line):
                i += 1
                continue
            # A section-start breadcrumb ("Division 2—..." right after a
            # fresh "Section N" marker) must not close the new section.
            # Only a division change on a section that already has body
            # content is a real structural boundary.
            if current and any(current.lines) and div_match.group(1) != ctx.division:
                sections.append(current)
                current = None
            ctx.division = div_match.group(1)
            ctx.division_title = strip_page_number(div_match.group(2))
            extra, i = _continues_title(lines, i, [RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION, RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE])
            if extra:
                ctx.division_title += extra
            ctx.subdivision = None
            ctx.subdivision_title = None
            logging.info("  Division %s — %s", ctx.division, ctx.division_title)
            i += 1
            continue

        # Structural: Subdivision
        if sub_match:
            if has_trailing_page_number(line):
                i += 1
                continue
            # Same rule as Division: breadcrumbs right after a fresh
            # section marker must not close it.
            if current and any(current.lines) and sub_match.group(1) != ctx.subdivision:
                sections.append(current)
                current = None
            ctx.subdivision = sub_match.group(1)
            ctx.subdivision_title = strip_page_number(sub_match.group(2))
            extra, i = _continues_title(lines, i, [RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION, RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE])
            if extra:
                ctx.subdivision_title += extra
            logging.info("    Subdivision %s — %s", ctx.subdivision, ctx.subdivision_title)
            i += 1
            continue

        # Section
        if m := RE_SECTION_MARKER.match(line):
            if is_page_footer(line) or len(line) - len(line.lstrip()) > 20:
                # right-aligned running header ("Section 5B" at page top)
                i += 1
                continue
            section_number = m.group(1)
            if current is not None and current.number == section_number:
                # left-aligned page-top repeat of the section we're already
                # inside (continuation page) — not a new section
                i += 1
                continue
            # Breadcrumb guard: a left-aligned "Section N" marker whose next
            # non-blank line is BODY content (not the section's title) is a
            # page-top breadcrumb inside the previous section's text — the
            # real start is the inline "N Title" header further down. Only a
            # marker followed by a TOC-matching title is a true section start.
            if section_number in toc_titles:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines):
                    nxt = lines[j]
                    tt = toc_titles[section_number]
                    t_norm = re.sub(r"\s+", " ", nxt.lower()).strip()
                    tt_norm = re.sub(r"\s+", " ", tt.lower()).strip()
                    tt_norm = re.sub(r"[.]+$", "", tt_norm)
                    is_title = bool(
                        t_norm[:30] == tt_norm[:30]
                        or tt_norm[:30] in t_norm
                        or t_norm[:30] in tt_norm
                    )
                    if not is_title:
                        # breadcrumb inside previous section's body — ignore
                        i += 1
                        continue
            if current:
                sections.append(current)
            section_ctx = ParseContext(**vars(ctx))
            section_title = toc_titles.get(section_number, "")
            extra, i = _continues_title(lines, i, [RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION_MARKER, RE_SECTION, RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE], max_leading_ws=10)
            if extra and not section_title:
                section_title = extra.strip()
            current = Section(
                number=section_number,
                title=section_title,
                context=section_ctx,
            )
            logging.info("      s %s — %s", current.number, current.title)
            i += 1
            continue

        # Section (inline header form: "52 Covenants to be included in ...")
        # FBTAA/SIS start sections with a column-0 "N Title" line; the
        # "Section N" marker lines above are page-top breadcrumbs. When a
        # section starts mid-page with only the inline header, the marker
        # branch never fires and content bleeds into the previous section.
        # Treat a column-0 inline header as a real boundary IF its title
        # matches the TOC title prefix for that number (kills act-name
        # noise and TOC table rows like "Part 2A, to the extent it...").
        if (m_inl := RE_SECTION.match(line)) and not line[0].isspace():
            inl_num = m_inl.group(1)
            inl_title = m_inl.group(2)
            toc_title = toc_titles.get(inl_num, "")
            t_norm = re.sub(r"\s+", " ", inl_title.lower()).strip()
            tt_norm = re.sub(r"\s+", " ", toc_title.lower()).strip()
            tt_norm = re.sub(r"[.]+$", "", tt_norm)
            is_header = bool(
                toc_title
                and (
                    t_norm[:30] == tt_norm[:30]
                    or tt_norm[:30] in t_norm
                    or t_norm[:30] in tt_norm
                )
            )
            if is_header and (current is None or current.number != inl_num):
                if current:
                    sections.append(current)
                section_ctx = ParseContext(**vars(ctx))
                current = Section(
                    number=inl_num,
                    title=toc_title,
                    context=section_ctx,
                )
                logging.info("      s %s — %s (inline header)", current.number, current.title)
                i += 1
                continue

        # Body line
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
# Tree builder
# ---------------------------------------------------------------------------

def build_tree(sections: list[Section]) -> dict:
    tree = {"act": ACT_SHORT, "compilation_no": 0, "compilation_date": "", "parts": []}
    # pull compilation meta from first section context
    for s in sections:
        tree["compilation_no"] = s.context.compilation_no
        tree["compilation_date"] = s.context.compilation_date
        break
    part_map: dict[str, dict] = {}
    div_map: dict[tuple[str, str], dict] = {}

    for s in sections:
        ctx = s.context
        part_id = ctx.part or ""
        part_title = ctx.part_title or ""
        div_id = ctx.division or ""
        div_title = ctx.division_title or ""
        subdiv_id = ctx.subdivision or ""
        subdiv_title = ctx.subdivision_title or ""

        if part_id not in part_map:
            part_node = {"id": part_id, "title": part_title, "divisions": [], "sections": []}
            part_map[part_id] = part_node
            tree["parts"].append(part_node)
        part_node = part_map[part_id]

        if subdiv_id:
            # Need division -> subdivision hierarchy
            if (part_id, div_id) not in div_map:
                div_node = {"id": div_id, "title": div_title, "subdivisions": [], "sections": []}
                div_map[(part_id, div_id)] = div_node
                part_node["divisions"].append(div_node)
            div_node = div_map[(part_id, div_id)]

            # Find or create subdivision
            subdiv_node = None
            for sd in div_node.get("subdivisions", []):
                if sd["id"] == subdiv_id:
                    subdiv_node = sd
                    break
            if subdiv_node is None:
                subdiv_node = {"id": subdiv_id, "title": subdiv_title, "sections": []}
                div_node["subdivisions"].append(subdiv_node)
            subdiv_node["sections"].append({"id": s.number, "title": s.title, "path": str(s.output_path)})
        elif div_id:
            if (part_id, div_id) not in div_map:
                div_node = {"id": div_id, "title": div_title, "subdivisions": [], "sections": []}
                div_map[(part_id, div_id)] = div_node
                part_node["divisions"].append(div_node)
            div_node = div_map[(part_id, div_id)]
            div_node["sections"].append({"id": s.number, "title": s.title, "path": str(s.output_path)})
        else:
            part_node["sections"].append({"id": s.number, "title": s.title, "path": str(s.output_path)})

    # Sort everything naturally
    tree["parts"].sort(key=lambda p: _natural_key(p["id"]))
    for part in tree["parts"]:
        part["divisions"].sort(key=lambda d: _natural_key(d["id"]))
        # Sort sections directly under part, if any
        if "sections" in part:
            part["sections"].sort(key=lambda s: _natural_key(s["id"]))
        for div in part["divisions"]:
            div["subdivisions"].sort(key=lambda s: _natural_key(s["id"]))
            div["sections"].sort(key=lambda s: _natural_key(s["id"]))
            for sub in div["subdivisions"]:
                sub["sections"].sort(key=lambda s: _natural_key(s["id"]))

    return tree


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--compilation-no", type=int, default=96)
    ap.add_argument("--compilation-date", type=str, default="2025-04-01")
    ap.add_argument("--act-name", type=str, default="Fringe Benefits Tax Assessment Act 1986")
    ap.add_argument("--act-short", type=str, default="FBTAA 1986")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    global ACT_NAME, ACT_SHORT
    ACT_NAME = args.act_name
    ACT_SHORT = args.act_short

    logging.basicConfig(level=args.log_level, format="%(message)s")

    total = 0
    all_sections: list[Section] = []
    raw_files = sorted(args.raw_dir.glob("part*.txt"))  # part1.txt, part2.txt...

    # Build TOC title map from volume 1 (covers all sections)
    toc_titles: dict[str, str] = {}
    for raw_file in raw_files:
        toc_titles.update(build_toc_title_map(raw_file))
    logging.info("TOC title map: %d entries", len(toc_titles))

    for raw_file in raw_files:
        logging.info("=== Parsing %s ===", raw_file.name)
        ctx = ParseContext(
            compilation_no=args.compilation_no,
            compilation_date=args.compilation_date,
            source_pdf=raw_file.stem + ".pdf",
        )
        sections = parse_volume(raw_file, args.out_dir, ctx, dry_run=args.dry_run, toc_titles=toc_titles)
        logging.info("  -> %d sections", len(sections))
        total += len(sections)
        all_sections.extend(sections)

    # Write tree.json
    tree = build_tree(all_sections)
    tree_path = args.out_dir.parent / "tree.json"
    tree_path.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")
    logging.info("Wrote %s (%d parts)", tree_path, len(tree["parts"]))
    logging.info("Done. %d sections total.", total)


if __name__ == "__main__":
    main()
