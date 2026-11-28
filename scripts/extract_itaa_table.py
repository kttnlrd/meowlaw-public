#!/usr/bin/env python3
"""CDN-0171: rebuild mangled markdown tables in ITAA 1997 section files from
the authoritative compilation PDFs using PyMuPDF layout extraction.

Column boundaries (page-local x units): item ~125, case ~161, cost ~359.

Usage:
  python3 extract_itaa_table.py <pdf> <section>
"""
from __future__ import annotations

import argparse
import re
import sys

import fitz

ITEM_MAX = 145
COST_MIN = 320


def find_table_pages(doc, section: str) -> list:
    hits = []
    for pno in range(doc.page_count):
        page = doc[pno]
        txt = page.get_text()
        if f"Section {section}" in txt and "In this case" in txt and "The cost is" in txt:
            hits.append(pno)
    return hits


def extract_table(doc, pno: int) -> list[list[str]]:
    """Extract rows as [item, case, cost] from a page using line layout."""
    page = doc[pno]
    d = page.get_text("dict")
    lines: list[tuple[float, list[tuple[float, str]]]] = []  # (y0, [(x0, word)])
    for block in d["blocks"]:
        if block["type"] != 0:  # text only
            continue
        for line in block["lines"]:
            y0 = line["bbox"][1]
            ws = []
            for span in line["spans"]:
                for ch in span.get("chars", []):
                    pass
                # spans have text; use span-level words via split
                txt = span["text"]
                if not txt.strip():
                    continue
                x0 = span["bbox"][0]
                ws.append((x0, txt))
            if ws:
                lines.append((y0, ws))
    # sort by y0 then x0
    lines.sort(key=lambda t: (t[0], t[1][0] if t[1] else 0))

    rows: list[list[str]] = []
    cur = None

    def flush():
        nonlocal cur
        if cur:
            rows.append(cur)
        cur = None

    for y0, ws in lines:
        joined = " ".join(w for _, w in ws)
        # Detect row start: first word in item column is a number
        first_x, first_w = ws[0]
        if first_x < ITEM_MAX and re.match(r"^\d+$", first_w):
            flush()
            item = first_w
            case, cost = [], []
            for x, w in ws[1:]:
                if x < COST_MIN:
                    case.append(w)
                else:
                    cost.append(w)
            cur = [item, case, cost]
        elif cur is not None:
            for x, w in ws:
                if x < COST_MIN:
                    cur[1].append(w)
                else:
                    cur[2].append(w)
    flush()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("section")
    args = ap.parse_args()

    doc = fitz.open(args.pdf)
    pages = find_table_pages(doc, args.section)
    print(f"pages with {args.section} table: {[p+1 for p in pages]}", file=sys.stderr)

    for pno in pages:
        rows = extract_table(doc, pno)
        if not rows:
            continue
        print(f"--- page {pno+1}: {len(rows)} rows ---", file=sys.stderr)
        print("| Item | In this case: | The cost is: |")
        print("| --- | --- | --- |")
        for item, case, cost in rows:
            c = " ".join(case).replace("’", "'").replace("“", '"').replace("”", '"')
            co = " ".join(cost).replace("’", "'").replace("“", '"').replace("”", '"')
            print(f"| {item} | {c} | {co} |")


if __name__ == "__main__":
    main()
