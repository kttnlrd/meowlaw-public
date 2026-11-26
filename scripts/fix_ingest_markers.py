#!/usr/bin/env python3
"""Fix ``ingest-unclassified`` / ``ingest-formula`` markers in ITAA-1997 sections.

The MeowLaw PDF extractor dumped tables/formulas it couldn't classify as raw
fixed-width ``ingest-*`` fences. The raw volumes (``data/itaa-1997/raw/vol*.txt``,
the same source scriptkitty used) hold the same tables cleanly aligned. We:

1. **Tables** — re-parse them from the raw volume, matching each fence to its
   table by FULL-BODY content overlap (not just the header, which repeats across
   page-split tables like the ``Index`` tables).
2. **Formulas / diagrams / dotted-leader indexes** — strip the ugly fence label,
   keeping the body as a plain fenced block.

Only fence bodies are touched; prose, anchors and frontmatter are preserved.

Usage:
  python scripts/fix_ingest_markers.py --dry-run
  python scripts/fix_ingest_markers.py --write
  python scripts/fix_ingest_markers.py --file part-2-40/division-83A/83A-325.md --write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
ACT = "itaa-1997"
RAW_DIR = DATA / ACT / "raw"
SECTIONS = DATA / ACT / "sections"

FENCE_RE = re.compile(
    r"```(ingest-unclassified|ingest-formula) source=(\S+) page=(\d+)\n(.*?)```",
    re.S,
)

RE_SUBSECTION = re.compile(r"^\s*\((\d+)\)\s+(.*)$")
RE_PARAGRAPH = re.compile(r"^\s+\(([a-z]{1,3})\)\s+(.*)$")
RE_SUBPARAGRAPH = re.compile(r"^\s+\(([ivx]+)\)\s+(.*)$")
RE_NOTE = re.compile(r"^\s*Note\s*\d*:\s*(.*)$")
RE_EXAMPLE = re.compile(r"^\s*Example\s*\d*:\s*(.*)$")
_JUNK = re.compile(r"Income Tax Assessment Act|Compilation No\.|Compilation date:|Authorised Version")
_DIVIDER = re.compile(r"^\s*[_\u2014\-]{3,}\s*$")


def _wide_gaps(line: str) -> list[tuple[int, int]]:
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
    cells = []
    for j, b in enumerate(bounds):
        end = bounds[j + 1] if j + 1 < len(bounds) else len(line)
        cells.append(line[b:end].strip())
    return cells


def _looks_like_table_header(line: str) -> bool:
    if _JUNK.search(line):
        return False
    gaps = _wide_gaps(line)
    if not gaps:
        return False
    bounds = [0] + [g[1] for g in gaps]
    cells = _split_cells(line, bounds)
    n_cells = sum(1 for c in cells if c)
    if len(gaps) >= 2 and n_cells >= 3:
        return True
    if len(gaps) == 1 and n_cells == 2:
        c0 = cells[0].lower().rstrip(":")
        c1 = cells[1].lower()
        if (gaps[0][1] - gaps[0][0]) >= 6:
            return True
        if c0 in ("item",) and c1.startswith("this term"):
            return True
    return False


def _render_table(lines: list[str], header_idx: int) -> tuple[str, int]:
    header_line = lines[header_idx]
    gaps = _wide_gaps(header_line)
    bounds = [0] + [g[1] for g in gaps]
    ncols = len(bounds)
    hdr = _split_cells(header_line, bounds)

    title = ""
    title_idx = header_idx - 1
    if title_idx >= 0:
        t_line = lines[title_idx]
        if len(t_line) - len(t_line.lstrip()) > 20:
            title_idx -= 1
        if title_idx >= 0:
            t = lines[title_idx].strip()
            if t and not _wide_gaps(lines[title_idx]):
                if not any(p.match(lines[title_idx]) for p in (RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE)):
                    title = t

    rows: list[list[str]] = []
    j = header_idx + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            break
        if _JUNK.search(line) or _DIVIDER.match(line.strip()):
            break
        if any(p.match(line) for p in (RE_SUBSECTION, RE_PARAGRAPH, RE_SUBPARAGRAPH, RE_NOTE, RE_EXAMPLE)):
            break
        if ncols == 2:
            s = line.lstrip()
            rgaps = _wide_gaps(s)
            if not rgaps:
                break
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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set[str]:
    return {re.sub(r"[^a-z0-9]", "", w) for w in _norm(s).lower().split() if re.sub(r"[^a-z0-9]", "", w)}


def _is_dotted_index(body: str) -> bool:
    return bool(re.search(r"\.{4,}", body))


def _find_section_region(raw: str, section: str) -> tuple[int, int]:
    """Return (start_line, end_line) of the section body in the raw volume.

    Anchors on the section heading at column 0, and ends at the next column-0
    section heading (so long multi-page tables like the ``Index`` tables are
    fully included).
    """
    lines = raw.split("\n")
    cur = re.compile(rf"^{re.escape(section)}\s+[A-Z]")
    start = -1
    for i, l in enumerate(lines):
        if cur.match(l):
            start = i
            break
    if start < 0:
        cur2 = re.compile(rf"^\s*{re.escape(section)}\s+[A-Z]")
        for i, l in enumerate(lines):
            if cur2.match(l):
                start = i
                break
    if start < 0:
        return (-1, -1)
    # find the next section heading (column-0 "NNN-NNN Title")
    nxt = re.compile(r"^\d+-\d+[A-Z]?\s+[A-Z]")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if nxt.match(lines[i]):
            end = i
            break
    return (start, end)


def _parse_tables(region_lines: list[str]) -> list[str]:
    tables: list[str] = []
    i = 0
    while i < len(region_lines):
        if _looks_like_table_header(region_lines[i]):
            md, end = _render_table(region_lines, i)
            tables.append(md)
            i = end
        else:
            i += 1
    return tables


def process_file(rel_path: Path, write: bool, report: list, raw_cache: dict) -> int:
    md_path = SECTIONS / rel_path
    if not md_path.exists():
        return 0
    txt = md_path.read_text(encoding="utf-8")
    if "ingest-" not in txt:
        return 0
    fences = list(FENCE_RE.finditer(txt))
    if not fences:
        return 0

    replacements = []
    for m in fences:
        kind, src, page, body = m.group(1), m.group(2), m.group(3), m.group(4)

        if kind != "ingest-unclassified" or _is_dotted_index(body):
            replacements.append((m.start(), m.end(), "```\n" + body + "```"))
            continue

        vol = src.replace(".pdf", ".txt")
        raw_path = RAW_DIR / vol
        if not raw_path.exists():
            replacements.append((m.start(), m.end(), "```\n" + body + "```"))
            continue
        if vol not in raw_cache:
            raw_cache[vol] = raw_path.read_text(encoding="utf-8", errors="replace")
        raw = raw_cache[vol]
        start, end = _find_section_region(raw, rel_path.stem)
        if start < 0:
            replacements.append((m.start(), m.end(), "```\n" + body + "```"))
            continue
        tables = _parse_tables(raw.split("\n")[start:end])
        if not tables:
            replacements.append((m.start(), m.end(), "```\n" + body + "```"))
            continue

        fence_tokens = _tokens(body)
        best_md, best_score = None, 0.0
        for md in tables:
            md_tokens = _tokens(md)
            if not md_tokens or not fence_tokens:
                continue
            score = len(fence_tokens & md_tokens) / len(fence_tokens | md_tokens)
            if score > best_score:
                best_md, best_score = md, score
        if best_md is None or best_score < 0.3:
            replacements.append((m.start(), m.end(), "```\n" + body + "```"))
            report.append(f"[warn] {rel_path}: no raw match (page {page}, best={best_score:.2f})")
            continue
        replacements.append((m.start(), m.end(), best_md))
        report.append(f"[table] {rel_path}: p{page} match {best_score:.2f}")

    if write and replacements:
        new_txt = txt
        for start, end, new in sorted(replacements, key=lambda x: x[0], reverse=True):
            new_txt = new_txt[:start] + new + new_txt[end:]
        md_path.write_text(new_txt, encoding="utf-8")
    return len(replacements)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fix ingest markers in ITAA-1997 sections.")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--file", help="single file (relative to data/itaa-1997/sections)")
    args = ap.parse_args()

    if args.file:
        files = [Path(args.file)]
    else:
        files = sorted(
            p.relative_to(SECTIONS)
            for p in SECTIONS.rglob("*.md")
            if "ingest-" in p.read_text(encoding="utf-8", errors="replace")
        )

    report = []
    total = 0
    raw_cache: dict[str, str] = {}
    for rel in files:
        total += process_file(rel, args.write, report, raw_cache)

    for line in report:
        print(line)
    print(f"\n{'[DRY-RUN]' if not args.write else '[WROTE]'} {total} fences across {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
