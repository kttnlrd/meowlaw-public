#!/usr/bin/env python3
"""
Fix flattened/interleaved two-column tables in legislation markdown by
re-parsing the raw fixed-width volumes (the source of truth).

Corruption class (Harry's s170(7) example): the raw->markdown converter
flattened aligned two-column tables into single lines, interleaving left
and right column cells with collapsed spacing:

  "1 The Commissioner, before the end            The Court may order an
   extension of the of the limited amendment period or ..."

This script:
  1. Locates the section in the raw volume (skipping ToC + page junk)
  2. Parses every two-column table in the section (fixed-width split;
     new rows are detected by their leading row number)
  3. In the markdown, finds the CORRUPTED lines (the interleave signature
     WITHOUT pipe-delimiters — proper markdown tables are untouched)
  4. Replaces each corrupted run with a rebuilt <table>

Usage:
  python3 scripts/fix-flattened-tables.py --section itaa-1936/170 [--write]
  python3 scripts/fix-flattened-tables.py --all [--write]   (scan report)
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"

# Raw volumes per act (aligned fixed-width text)
RAW_VOLUMES = {
    "itaa-1997": [f"vol{i:02d}.txt" for i in range(1, 13)],
    "itaa-1936": [f"vol{i:02d}.txt" for i in range(1, 8)],
    "gst-1999": ["vol01.txt", "vol02.txt"],
    "taa-1953": ["vol01.txt", "vol02.txt"],
    "fbt-1986": ["vol01.txt", "vol02.txt"],
    "sis-1993": ["vol01.txt", "vol02.txt"],
    "aml-ctf-2006": ["vol01.txt"],
    "nz-it-2007": ["vol01.txt", "vol02.txt", "vol03.txt", "vol04.txt"],
    "corporations-act-2001": ["vol01.txt", "vol02.txt", "vol03.txt", "vol04.txt", "vol05.txt"],
}

# Two-column table header lines in raw text
TABLE_HEADER_RE = re.compile(
    r"(?:In this case|In this situation|If(?: the)?|Where|"
    r"the liabilities cannot exceed|the result is|the consequence is)\s*:?\s+"
    r"(?:the position is|the liabilities cannot exceed|the result is|the consequence is)\s*:",
    re.IGNORECASE,
)

GAP_RE = re.compile(r" {10,}")

# Page-header / footer junk lines in raw volumes
JUNK_RE = re.compile(
    r"(?:Compilation No\.|Compilation date:|Authorised Version|"
    r"Income Tax Assessment Act 1936|Returns and assessments|"
    r"Section \d+[A-Z]?$|^\s*\d{1,4}\s*$|^\s*\f\s*$)",
    re.IGNORECASE,
)


def find_section_region(raw: str, section: str) -> tuple[int, int]:
    """Find (start, end) char offsets of a section in the raw volume text.

    Anchors on "170 <Capitalised Title>" at line start, skipping ToC
    entries (dot-leader page numbers). End = next section heading
    (170A/170B/...), also skipping ToC entries.
    """
    start = -1
    pat = re.compile(rf"^\s*{re.escape(section)}\s+[A-Z][^\n]*$", re.MULTILINE)
    for m in pat.finditer(raw):
        line = m.group(0)
        if re.search(r"\.{3,}\s*\d+\s*$", line):
            continue
        start = m.start()
        break
    if start < 0:
        m = re.search(rf"^\s*{re.escape(section)}\s+[A-Z]", raw, re.MULTILINE)
        start = m.start() if m else -1
    if start < 0:
        return (-1, -1)
    end = len(raw)
    pat_next = re.compile(rf"^\s*{re.escape(section)}[A-Za-z]\s+[A-Z][^\n]*$", re.MULTILINE)
    for m in pat_next.finditer(raw, start + len(section) + 2):
        if re.search(r"\.{3,}\s*\d+\s*$", m.group(0)):
            continue
        end = m.start()
        break
    return (start, end)


def parse_table_region(raw_lines: list[str], start: int) -> list[list[str]]:
    """Parse a fixed-width two-column table starting at the header line.

    Returns a list of rows, each [left, right] (right may be empty).
    A NEW row starts when a line begins with a row number ("1 ...",
    "2 ...") at the first column — continuation lines are indented.
    """
    header = raw_lines[start]
    m = re.search(
        r"(the position is:|the liabilities cannot exceed:|the result is:|the consequence is:)",
        header, re.IGNORECASE,
    )
    if not m:
        return []
    # column boundary: end of the first big whitespace gap in the header
    col2_start = m.start()
    gm = GAP_RE.search(header)
    if gm and gm.start() > 0:
        col2_start = gm.end()
    rows: list[list[str]] = []
    cur: list[str] = []
    in_table = False
    new_row_re = re.compile(r"^\s*\d+\s+")
    for idx, line in enumerate(raw_lines[start + 1:], start + 1):
        s = line.strip()
        # caption line: title-case, no punctuation, immediately followed by
        # a table header (e.g. "Extensions of limited amendment period"
        # before "In this case: ... the position is:")
        nxt = raw_lines[idx + 1] if idx + 1 < len(raw_lines) else ""
        if (
            s
            and re.match(r"^[A-Z][A-Za-z][A-Za-z ]{2,70}$", s)
            and "." not in s
            and TABLE_HEADER_RE.search(nxt)
        ):
            continue
        # a SECOND table header ends this table (e.g. s170(7) has two
        # sequential tables — "Extensions of limited amendment period"
        # caption + header, then again for item 2)
        if TABLE_HEADER_RE.search(line):
            if in_table and (rows or cur):
                break
            continue
        s = line.strip()
        if JUNK_RE.search(line) or re.search(r"^\s*\d{1,4}\s*$", s):
            if in_table and (rows or cur):
                break  # page boundary ends the table
            continue
        # numbered subsection ends the table (section-heading pattern
        # "N Capital..." is NOT used — it false-matches table rows like
        # "1 The Commissioner")
        if re.match(r"^\s*\(\d+[a-z]?\)\s", line):
            if in_table and (rows or cur):
                break
            continue
        if not s:
            if in_table and (rows or cur):
                break
            continue
        in_table = True
        # split at the column boundary (fall back to first big gap)
        if len(line) > col2_start and line[:col2_start].strip():
            left = line[:col2_start].strip()
            right = line[col2_start:].strip()
        else:
            parts = GAP_RE.split(line, maxsplit=1)
            if len(parts) == 2:
                left = parts[0].strip()
                right = parts[1].strip()
            else:
                left = line.strip()
                right = ""
        # new logical row: leading row number at first column
        is_new_row = bool(new_row_re.match(line)) and bool(left)
        if is_new_row and cur:
            rows.append(cur)
            cur = [left, right]
            continue
        if not cur:
            cur = [left, right]
            continue
        # continuation: append to whichever cell has content
        if left and right:
            # both present on a continuation line — prefer appending to the
            # non-empty side that matches column position
            if cur[0] and not cur[1]:
                cur[0] += " " + left
                cur[1] = right
            elif cur[1] and not cur[0]:
                cur[0] = left
                cur[1] += " " + right
            elif cur[0] and cur[1]:
                cur[0] += " " + left
                cur[1] += " " + right
            else:
                cur = [left, right]
            continue
        if left:
            cur[0] = (cur[0] + " " + left).strip()
            continue
        if right:
            cur[1] = (cur[1] + " " + right).strip()
            continue
    if cur:
        rows.append(cur)
    return rows


def find_and_parse(raw: str, section: str) -> list[list[list[str]]]:
    """Parse all two-column tables in a section's raw region."""
    start, end = find_section_region(raw, section)
    if start < 0:
        return []
    lines = raw.split("\n")
    start_line = raw[:start].count("\n")
    end_line = min(raw[:end].count("\n") + 1, len(lines))
    tables: list[list[list[str]]] = []
    for i in range(start_line, end_line):
        if TABLE_HEADER_RE.search(lines[i]):
            rows = parse_table_region(lines, i)
            if len(rows) >= 1:
                tables.append(rows)
    return tables


def markdown_table(rows: list[list[str]]) -> str:
    out = ["<table>"]
    for left, right in rows:
        out.append(
            f"  <tr><td>{left.strip()}</td><td>{right.strip()}</td></tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _score_table(corrupt_line: str, table: list[list[str]]) -> float:
    """Fraction of table words appearing in the corrupted line.

    Requires a hard anchor: the first row's left cell (normalised) must
    appear in the corrupted line — otherwise 0.0. This prevents a generic
    word-overlap match (e.g. the (3) table matching a (7) corruption).
    """
    norm_line = _norm(corrupt_line).lower()
    if not table or not table[0]:
        return 0.0
    # hard anchor: first 5 words of the first row's left cell must ALL
    # appear (as tokens) in the corrupted line. Interleaving fragments
    # contiguous text, so use token-presence not substring — but the (3)
    # table's "amends" won't occur in a (7) corruption, so this still
    # discriminates.
    anchor_tokens = [re.sub(r"[^a-z0-9]", "", w) for w in _norm(table[0][0]).lower().split()[:5]]
    anchor_tokens = [t for t in anchor_tokens if t]
    if not anchor_tokens:
        return 0.0
    line_tokens = set(re.findall(r"[a-z0-9]+", norm_line))
    if not all(t in line_tokens for t in anchor_tokens):
        return 0.0
    words = set(_norm(" ".join(c for row in table for c in row)).lower().split())
    if not words:
        return 0.0
    found = sum(1 for w in words if re.sub(r"[^a-z0-9]", "", w) in line_tokens)
    return found / len(words)


def fix_section(act: str, rel_path: str, write: bool = False) -> bool:
    section = Path(rel_path).stem
    md_path = DATA_DIR / act / rel_path
    if not md_path.exists():
        print(f"  [skip] {rel_path}: file missing")
        return False

    tables: list[list[list[str]]] = []
    for vol in RAW_VOLUMES.get(act, []):
        raw_path = DATA_DIR / act / "raw" / vol
        if not raw_path.exists():
            continue
        raw = raw_path.read_text(encoding="utf-8", errors="replace")
        tables = find_and_parse(raw, section)
        if tables:
            break

    if not tables:
        print(f"  [skip] {rel_path}: no table found in raw")
        return False

    md = md_path.read_text()
    lines = md.split("\n")
    replaced = 0
    used_tables = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        # CORRUPTED line: interleave signature WITHOUT pipe delimiters
        if (
            re.search(r"In this case:.*the position is:|"
                      r"the liabilities cannot exceed:.*the result is:", line, re.IGNORECASE)
            and "|" not in line
            and len(line) > 200
        ):
            # consume the WHOLE corrupted run: consecutive corrupted lines
            # plus interleaved anchor/note lines, until the next numbered
            # subsection (each corrupted line is one flattened table)
            run_end = i
            corrupt_in_run = []
            while run_end < len(lines) and not re.match(r"^\s*\*\*\(\d+", lines[run_end]):
                l = lines[run_end]
                if (
                    "|" not in l
                    and re.search(r"In this case:.*the position is:|"
                                  r"the liabilities cannot exceed:.*the result is:",
                                  l, re.IGNORECASE)
                ):
                    corrupt_in_run.append(run_end)
                run_end += 1
            if not corrupt_in_run:
                i += 1
                continue
            # match each corrupted line to its best unused raw table
            blocks = []
            ok = True
            for ci in corrupt_in_run:
                best_idx, best_score = -1, 0.0
                for ti, tbl in enumerate(tables):
                    if ti in used_tables:
                        continue
                    score = _score_table(lines[ci], tbl)
                    if score > best_score:
                        best_idx, best_score = ti, score
                if best_idx < 0 or best_score < 0.3:
                    print(f"  [warn] {rel_path}: no matching table (best {best_score:.2f}) at line {ci+1}")
                    ok = False
                    break
                used_tables.add(best_idx)
                blocks.append(markdown_table(tables[best_idx]))
            if not ok:
                i = run_end
                continue
            lines[i:run_end] = blocks
            replaced += len(blocks)
            i += len(blocks)
        else:
            i += 1

    if not replaced:
        print(f"  [skip] {rel_path}: no corrupted lines found (already fixed?)")
        return False

    new_md = "\n".join(lines)
    if write:
        md_path.write_text(new_md)
        print(f"  [fixed] {rel_path}: rebuilt {replaced} table(s)")
    else:
        print(f"  [dry-run] {rel_path}: would rebuild {replaced} table(s)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", help="e.g. itaa-1936/170 or full rel path")
    ap.add_argument("--all", action="store_true", help="fix all files from scan report")
    ap.add_argument("--write", action="store_true", help="write changes (default dry-run)")
    args = ap.parse_args()

    if args.section:
        parts = args.section.split("/", 1)
        if len(parts) == 1:
            act, rel = parts[0], f"sections/{parts[0]}.md"
        elif "/" in parts[1] and parts[1].endswith(".md"):
            act, rel = parts
        else:
            act = parts[0]
            rel = parts[1] if parts[1].endswith(".md") else f"sections/{parts[1]}.md"
        fix_section(act, rel, args.write)
    elif args.all:
        report = json.load(open(DATA_DIR / "garbled_scan_report.json"))
        targets = []
        for act, d in report.items():
            for cls in ("T1", "T6"):
                for it in d.get("issues", {}).get(cls, []):
                    # stored path already includes the act prefix
                    rel = it["file"].split("/", 1)[1] if it["file"].startswith(act + "/") else it["file"]
                    targets.append((act, rel))
        targets = sorted(set(targets))
        print(f"{len(targets)} flattened-table files to fix")
        for act, rel in targets:
            fix_section(act, rel, args.write)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
