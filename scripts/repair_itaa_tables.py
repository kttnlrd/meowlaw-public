#!/usr/bin/env python3
"""CDN-0171: rebuild mangled markdown tables in itaa-1997 section files from
the authoritative PDF layout text (legislation.gov.au compilation volumes).

The section parser flattened multi-line PDF tables into jumbled markdown rows
(e.g. s40-180 items 2, 8, 9). pdftotext -layout preserves column alignment,
so we reconstruct each row by character-position column slicing, then replace
the broken markdown table with clean single-cell-per-row markdown.

Usage:
  python3 repair_itaa_tables.py --vol /tmp/vol02.txt --section 40-180 [--apply]

Writes a JSON report of proposed replacements; --apply writes the files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# Character columns observed in the ITAA 1997 compilation PDFs (pdftotext -layout)
# Item | In this case: | The cost is:
ITEM_COL_START = 1      # item number starts here
CASE_COL_START = 11     # "In this case:" text starts ~col 11
COST_COL_START = 66     # "The cost is:" column starts ~col 66

TABLE_HEADER_RE = re.compile(r"^\s*Item\s+\S.*\S\s{3,}\S")


def detect_anchor(header: str) -> int:
    """Find the third column start from a table header line.

    Header shape: "Item <col2 label...> <col3 label>". The largest whitespace
    run in the header separates col2 from col3; the anchor is where col3 starts.
    """
    runs = []
    in_txt = False
    start = 0
    for c, ch in enumerate(header):
        if ch != " " and not in_txt:
            in_txt = True
            start = c
        elif ch == " " and in_txt:
            in_txt = False
            runs.append((start, c, header[start:c]))
    if in_txt:
        runs.append((start, len(header), header[start:]))
    best_gap = -1
    best_i = -1
    for i in range(len(runs) - 1):
        gap = runs[i + 1][0] - runs[i][1]
        if gap > best_gap:
            best_gap = gap
            best_i = i
    if best_i >= 0 and best_gap >= 3:
        return runs[best_i + 1][0]
    return 66


def section_pages(pdf_text: str, section: str) -> tuple[int, int]:
    """Find the line range of the section's body (from its Section header to
    the next section's header)."""
    lines = pdf_text.splitlines()
    starts = []
    for i, ln in enumerate(lines):
        # Body headers are left-aligned; running headers are right-aligned
        # (start at col 60+). Only accept left-aligned ones.
        m = re.match(r"^\s{0,2}Section\s+([0-9]+(?:-[0-9]+)?[A-Z]?)\s*$", ln)
        if m:
            starts.append((m.group(1), i))
    # find our section, then the next section start after it
    for idx, (sid, i) in enumerate(starts):
        if sid == section:
            end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
            # Back up ~50 lines to capture tables that begin on the previous
            # page (a multi-page table can start under the running header of
            # the page before the section's body header).
            return max(0, i - 50), end
    return 0, len(lines)


def section_tables(pdf_text: str, section: str) -> list[tuple[list[str], int]]:
    """Extract table blocks for a section, with fallback when the section
    body header is missing (section continues across a volume boundary and
    only running headers appear)."""
    tables = extract_table_lines(pdf_text, section)
    if tables and any(len(rows_from_table(t, a)) > 0 for t, a in tables):
        return tables
    # Fallback: search the whole text for tables whose first item looks
    # like this section's table (bounded: take tables until the next
    # left-aligned Section header).
    lo, hi = section_pages(pdf_text, section)
    if lo == 0 and hi == len(pdf_text.splitlines()):
        lines = pdf_text.splitlines()
        # Find the last running header for this section, take 50 lines after
        best = -1
        for i, ln in enumerate(lines):
            if re.search(rf"Section\s+{re.escape(section)}\s*$", ln):
                best = i
        if best >= 0:
            lo = max(0, best - 5)
            hi = min(len(lines), best + 2000)
            tables2: list[tuple[list[str], int]] = []
            cur: list[str] | None = None
            cur_anchor = 66
            for ln in lines[lo:hi]:
                if TABLE_HEADER_RE.search(ln):
                    if cur is not None:
                        tables2.append((cur, cur_anchor))
                    cur = [ln]
                    cur_anchor = detect_anchor(ln)
                    continue
                if cur is not None:
                    if ln.strip().startswith("_" * 10) or "\f" in ln or "To find definitions" in ln:
                        tables2.append((cur, cur_anchor))
                        cur = None
                    else:
                        cur.append(ln)
            if cur is not None:
                tables2.append((cur, cur_anchor))
            return tables2
    return tables


def extract_table_lines(pdf_text: str, section: str) -> list[tuple[list[str], int]]:
    """Find table blocks in the layout text, scoped to the section's pages.

    Returns (table_lines, cost_anchor) where cost_anchor is the column of
    the third column label in that block's header.
    """
    lo, hi = section_pages(pdf_text, section)
    lines = pdf_text.splitlines()[lo:hi]
    tables: list[tuple[list[str], int]] = []
    cur: list[str] | None = None
    cur_anchor = 66
    for ln in lines:
        if TABLE_HEADER_RE.search(ln):
            if cur is not None:
                tables.append((cur, cur_anchor))
            cur = [ln]
            # The anchor is the start of the third column. The header is
            # "Item <col2-label...> <col3-label>" — find the largest gap.
            cur_anchor = detect_anchor(ln)
            continue
        if cur is not None:
            if ln.strip().startswith("_" * 10) or "\f" in ln or "To find definitions" in ln:
                tables.append((cur, cur_anchor))
                cur = None
            else:
                cur.append(ln)
    if cur is not None:
        tables.append((cur, cur_anchor))
    return tables


def split_columns(line: str, anchor: int = 66) -> tuple[str, str]:
    """Split a table layout line into (case, cost).

    anchor = expected cost column start (from the block header). The cost
    column in these PDFs sits at anchor..anchor-6 across rows, so we pick
    the largest whitespace gap whose split point is within anchor±8.
    Falls back to the plain largest gap.
    """
    line = line.rstrip("\n")
    if not line.strip():
        return "", ""
    runs = []
    in_txt = False
    start = 0
    for c, ch in enumerate(line):
        if ch != " " and not in_txt:
            in_txt = True
            start = c
        elif ch == " " and in_txt:
            in_txt = False
            runs.append((start, c, line[start:c]))
    if in_txt:
        runs.append((start, len(line), line[start:]))
    if len(runs) < 2:
        # Single run: route by x position. Cost column text starts at
        # anchor (or a few chars left); case column text starts far left.
        if runs and runs[0][0] >= anchor - 8:
            return "", line.strip()
        return line.strip(), ""
    # If the whole line sits in the cost column (first run at anchor),
    # route it all to cost. This handles continuation lines that carry
    # only "The cost is" content.
    if runs[0][0] >= anchor - 8:
        return "", " ".join(r[2] for r in runs)
    # Candidate splits: between run i and i+1.
    # Prefer the largest gap whose split point is within anchor±8; if none
    # qualify, use the global largest gap.
    best_gap = -1
    best_i = -1
    anchored_gap = -1
    anchored_i = -1
    global_gap = -1
    global_i = -1
    for i in range(len(runs) - 1):
        gap = runs[i + 1][0] - runs[i][1]
        split_pos = runs[i + 1][0]
        if gap > global_gap:
            global_gap = gap
            global_i = i
        if abs(split_pos - anchor) <= 8 and gap > anchored_gap:
            anchored_gap = gap
            anchored_i = i
    if anchored_i >= 0:
        best_i = anchored_i
        best_gap = anchored_gap
    elif global_i >= 0:
        best_i = global_i
        best_gap = global_gap
    if best_gap < 4:  # no real column separation
        return line.strip(), ""
    case = " ".join(r[2] for r in runs[: best_i + 1])
    cost = " ".join(r[2] for r in runs[best_i + 1 :])
    return case, cost


def rows_from_table(table: list[str], anchor: int = 66) -> list[dict]:
    """Split layout lines into (item, case, cost) cells by column detection."""
    rows: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur.get("item") is not None:
            rows.append(cur)
        cur = None

    for ln in table:
        # A new row starts when the Item column has a number. Item numbers
        # sit at col 1-2 (aligned tables) or ~col 12 (indented tables);
        # never accept a bare year like 1996 as an item.
        item_m = re.match(r"^\s{0,14}(\d+(?:\.\d+)*)\s", ln)
        is_new = bool(item_m) and item_m.group(1) not in {"1936", "1997", "1999", "2001", "2003", "2006", "2013", "2014", "2016", "2020", "2026"}
        if is_new:
            flush()
            item_tok = item_m.group(1)
            cur = {"item": item_tok, "case": [], "cost": []}
            # The item token ends at its position in the line; split the
            # remainder at the anchor. The item may be indented up to 14
            # chars, so use the anchor as-is (no shift) for the rest.
            item_end = item_m.end()
            rest = ln[item_end:]
            case, cost = split_columns(rest, anchor)
            if case:
                cur["case"].append(case)
            if cost:
                cur["cost"].append(cost)
        elif cur is not None:
            case, cost = split_columns(ln, anchor)
            if case:
                cur["case"].append(case)
            if cost:
                cur["cost"].append(cost)
    flush()
    return rows


def md_table(rows: list[dict]) -> str:
    """Render rows as a clean markdown table."""
    out = ["| Item | In this case: | The cost is: |", "| --- | --- | --- |"]
    for r in rows:
        case = " ".join(r["case"]).replace("’", "'").replace("“", '"').replace("”", '"')
        cost = " ".join(r["cost"]).replace("’", "'").replace("“", '"').replace("”", '"')
        out.append(f"| {r['item']} | {case} | {cost} |")
    return "\n".join(out)


def replace_table_in_file(md_path: Path, rows: list[dict]) -> tuple[bool, int]:
    """Replace the mangled cost-table region in a section markdown file.

    Strategy: find the "| Item | In this case: | The cost is: |" header line,
    then consume lines until the table footer ("| --- | --- | --- |") that
    belongs to a *later* header, or until a new non-table block starts.
    The existing mangled rows may span dozens of lines; we replace everything
    from the header to the last line that still looks like table debris.
    """
    txt = md_path.read_text(errors="replace")
    header_re = re.compile(r"^\s*\|?\s*Item\s*\|\s*In this case:\s*\|.*The cost is:\s*\|?\s*$")
    lines = txt.splitlines()
    header_idx = None
    for i, ln in enumerate(lines):
        if header_re.match(ln):
            header_idx = i
            break
    if header_idx is None:
        return False, 0

    # Consume until a blank line followed by a non-table line, or a new
    # "Item ... The cost is" header, or an explicit footer separator.
    end = header_idx + 1
    saw_any_row = False
    while end < len(lines):
        ln = lines[end]
        if header_re.match(ln):
            break
        stripped = ln.strip()
        if not stripped:
            # Blank line: stop if we've already consumed table rows and the
            # next non-blank line is not table debris.
            nxt = end + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            if nxt >= len(lines):
                end = nxt
                break
            if lines[nxt].lstrip().startswith("|") or re.match(r"^\s*\|?\s*\d+\s*\|", lines[nxt]) or re.match(r"^\s*[-*_]{3,}\s*$", lines[nxt]):
                end += 1
                continue
            break
        if stripped == "| --- | --- | --- |" or stripped == "|--- | --- | --- |" or re.match(r"^\|?\s*-{3,}\s*\|\s*-{3,}", stripped):
            # Footer separator: only belongs to this table if followed by
            # more table rows; otherwise it's the end.
            nxt = end + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            if nxt < len(lines) and (lines[nxt].lstrip().startswith("|") or re.match(r"^\s*\d+\s*\|", lines[nxt])):
                end += 1
                continue
            end += 1
            break
        if stripped.startswith("|") or re.match(r"^\s*\d+\s*\|", stripped) or stripped.startswith(">") or stripped.startswith("**"):
            saw_any_row = True
            end += 1
            continue
        break
    if not saw_any_row:
        return False, 0

    new_table = md_table(rows)
    new_lines = lines[:header_idx] + new_table.splitlines() + lines[end:]
    md_path.write_text("\n".join(new_lines) + "\n")
    return True, end - header_idx


def find_section_file(section: str) -> Path | None:
    for p in (DATA / "itaa-1997" / "sections").rglob(f"{section}.md"):
        return p
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol", required=True, help="pdftotext -layout output file")
    ap.add_argument("--section", required=True, help="section id, e.g. 40-180")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    pdf_text = Path(args.vol).read_text(errors="replace")
    tables = extract_table_lines(pdf_text, args.section)
    print(f"found {len(tables)} candidate table blocks")

    allrows: list[dict] = []
    for i, (t, anchor) in enumerate(tables):
        rows = rows_from_table(t, anchor)
        allrows.extend(rows)
        print(f"--- table {i}: {len(rows)} rows (anchor {anchor}) ---")
    print(f"TOTAL: {len(allrows)} rows")
    print()
    print(md_table(allrows))


if __name__ == "__main__":
    main()
