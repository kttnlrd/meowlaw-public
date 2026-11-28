#!/usr/bin/env python3.12
"""Recover table structure and footnotes from the MTC 2010 PDF.

The PDF's text layer carries no table markup: each cell is its own visual line and reading
order flattens the table into prose ("39,000 43,000 Full exemption (20a)"). Column positions
are stable to a fraction of a point, so the grid can be rebuilt from x geometry once the table
regions are known. Regions come from PyMuPDF's line-based detector, which sees the rules but
splits borderless tables into fragments; fragments with the same x range and a small y gap are
merged back into one region.

Footnotes are set in 7.4pt (7.0pt on table pages) against 7.9pt body text and begin with a
"1."-style marker, so size plus the marker identifies them without guessing page geometry.
"""
from __future__ import annotations

import re

NUM = re.compile(r"^[-–—]?\s*[\d,]+(?:\.\d+)?%?$")
NOTE_START = re.compile(r"^\d{1,2}\.?\s+\S")
FURNITURE = re.compile(r"MODEL TAX CONVENTION|ISBN 978-92-64|^\(c\) OECD|^\d{1,4}$")
COL_TOL = 6.0
ROW_TOL = 2.0
BODY_SIZE = 7.9
NOTE_MAX_SIZE = 7.5


def _tidy(text: str) -> str:
    """Collapse whitespace and close up italic markers that came back as separate spans."""
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\(\s+([a-zA-Z]{1,4})\s*\)", r"(\1)", text)


def page_rows(page) -> list[dict]:
    """Visual rows of one page: text grouped by y, cells split on wide horizontal gaps."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s["text"].strip()]
            if not spans:
                continue
            big = max(spans, key=lambda s: s["size"])
            lines.append({
                "y": round(line["bbox"][1], 1),
                "x0": round(line["bbox"][0], 1),
                "x1": round(line["bbox"][2], 1),
                "size": round(big["size"], 1),
                "text": _tidy(" ".join(s["text"] for s in spans)),
            })
    lines.sort(key=lambda d: (d["y"], d["x0"]))
    rows: list[dict] = []
    for ln in lines:
        if rows and abs(ln["y"] - rows[-1]["y"]) <= ROW_TOL:
            rows[-1]["cells"].append(ln)
        else:
            rows.append({"y": ln["y"], "cells": [ln]})
    for r in rows:
        r["cells"].sort(key=lambda c: c["x0"])
        r["text"] = "  ".join(c["text"] for c in r["cells"])
        r["gap_cells"] = _split_cells(r["cells"])
        r["size"] = min(c["size"] for c in r["cells"])
        r["x0"] = min(c["x0"] for c in r["cells"])
        r["x1"] = max(c["x1"] for c in r["cells"])
    return rows


def _split_cells(cells: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in cells:
        if out and c["x0"] - out[-1]["x1"] <= 18.0:
            out[-1]["text"] = f"{out[-1]['text']} {c['text']}".strip()
            out[-1]["x1"] = max(out[-1]["x1"], c["x1"])
        else:
            out.append({"text": c["text"], "x0": c["x0"], "x1": c["x1"]})
    return out


def _row_has_numbers(row: dict) -> int:
    return sum(1 for c in row["gap_cells"][1:]
               if any(ch.isdigit() for ch in c["text"]) and len(c["text"]) <= 30)


def table_regions(page) -> list[tuple[float, float, float, float]]:
    """Bounding boxes of table regions.

    PyMuPDF's line detector only returns the ruled fragments of these borderless tables (three
    rows of eight), so regions are driven by the rows themselves: a row with a label cell and at
    least two numeric cells is table evidence, consecutive evidence is one table, and the header
    rows above it are pulled in because they do not run to the right margin like prose does.
    """
    rows = page_rows(page)
    marks = [i for i, r in enumerate(rows)
             if len(r["gap_cells"]) >= 3 and _row_has_numbers(r) >= 2]
    if len(marks) < 2:
        return []
    groups: list[list[int]] = []
    for i in marks:
        if groups and rows[i]["y"] - rows[groups[-1][-1]]["y"] <= 46:
            groups[-1].append(i)
        else:
            groups.append([i])
    regions = []
    for g in groups:
        if len(g) < 2:
            continue
        first, last = g[0], g[-1]
        # header rows above the first numeric row: they are short of the right margin like the
        # table, where a prose line runs the full measure
        steps = 0
        while first > 0 and steps < 4 and rows[first]["y"] - rows[first - 1]["y"] <= 26 \
                and len(rows[first - 1]["gap_cells"]) >= 2 and rows[first - 1]["x1"] < 460:
            first -= 1
            steps += 1
        band = rows[first:last + 1]
        regions.append((min(r["x0"] for r in band), band[0]["y"],
                        max(r["x1"] for r in band), band[-1]["y"]))
    try:
        for t in page.find_tables(strategy="lines").tables:
            x0, y0, x1, y1 = t.bbox
            for k, (a, b, c, d) in enumerate(regions):
                if not (y1 < b - 6 or y0 > d + 6) and min(x1, c) - max(x0, a) > 40:
                    regions[k] = (min(a, x0), min(b, y0), max(c, x1), max(d, y1))
    except Exception:
        pass
    return regions


def tables_of(page) -> list[dict]:
    """Tables on a page as {'y0','y1','x0','x1','markdown'}."""
    regions = table_regions(page)
    if not regions:
        return []
    rows = page_rows(page)
    out = []
    for x0, y0, x1, y1 in regions:
        inside = [r for r in rows if y0 - 2 <= r["y"] <= y1 + 2 and r["x0"] >= x0 - 6]
        inside = [r for r in inside if r["x0"] <= x1]
        if not inside:
            continue
        seen: dict[float, int] = {}
        for r in inside:
            for c in r["gap_cells"][1:]:
                if any(ch.isdigit() for ch in c["text"]) and len(c["text"]) <= 30:
                    key = next((e for e in seen if abs(c["x1"] - e) <= COL_TOL), c["x1"])
                    seen[key] = seen.get(key, 0) + 1
        # a column has to hold at least two values; a lone header figure is not its own column
        cols = sorted(e for e, n in seen.items() if n >= 2) or sorted(seen)
        if not cols:
            continue
        grid: list[list[str]] = []
        for r in inside:
            row = [""] * (1 + len(cols))
            for pos, c in enumerate(r["gap_cells"]):
                # the label column is left-aligned text at the left margin; every later cell
                # sits under one of the numeric columns, header text included
                idx = 0 if pos == 0 else 1 + min(range(len(cols)), key=lambda k: abs(c["x1"] - cols[k]))
                txt = re.sub(r"\s+\)", ")", c["text"])
                row[idx] = f"{row[idx]} {txt}".strip() if row[idx] else txt
            grid.append([_tidy(v) for v in row])
        if len(grid) < 2:
            continue
        if _all_numbered(grid[0]):
            head, body = [""] * len(grid[0]), grid
        else:
            head, body = grid[0], grid[1:]
        md = ["| " + " | ".join(h.replace("|", "/") for h in head) + " |",
              "|" + "|".join("---" for _ in head) + "|"]
        for row in body:
            md.append("| " + " | ".join(v.replace("|", "/") for v in row) + " |")
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "markdown": "\n".join(md), "rows": len(grid)})
    return out


def _all_numbered(row: list[str]) -> bool:
    vals = [v for v in row if v]
    return bool(vals) and all(NUM.match(v) or v.replace(" ", "").replace("-", "").isdigit() for v in vals)


def _note_threshold(rows: list[dict]) -> float:
    """Type size below which a row on this page is a footnote.

    A footnote is smaller than the body of the page it sits on, so the threshold is relative: the
    Annex reprints the previous Article 7 in 7.4 pt, and that is body text there, not a footnote.
    """
    sizes = [round(r["size"], 1) for r in rows if r["size"] >= 7.2]
    if not sizes:
        return NOTE_MAX_SIZE
    dom = max(sorted(set(sizes)), key=sizes.count)
    return min(NOTE_MAX_SIZE, dom - 0.15)


def note_blocks(page, regions: list[tuple[float, float, float, float]] | None = None) -> list[dict]:
    """Footnote blocks as {'y0','y1','text'}, so the ingest can place each one in reading order."""
    rows = page_rows(page)
    if regions is None:
        regions = table_regions(page)
    thr = _note_threshold(rows)
    blocks: list[dict] = []
    active = False
    for r in rows:
        if any(y0 - 2 <= r["y"] <= y1 + 2 for _, y0, _, y1 in regions):
            active = False  # a table cell beginning with digits is not a footnote
            continue
        if FURNITURE.search(r["text"]):
            active = False
            continue
        small = r["size"] <= thr
        if small and NOTE_START.match(r["text"]) and len(r["text"]) > 8:
            blocks.append({"y0": r["y"], "y1": r["y"], "text": r["text"]})
            active = True
        elif active and small and len(r["gap_cells"]) == 1 and not NOTE_START.match(r["text"]):
            blocks[-1]["text"] = f"{blocks[-1]['text']} {r['text']}".strip()
            blocks[-1]["y1"] = r["y"]
        else:
            active = False
    return blocks


def notes_of(page, regions: list[tuple[float, float, float, float]] | None = None) -> list[str]:
    """Footnote blocks on a page: a small-type line opening with '1.' plus its continuations."""
    return [b["text"] for b in note_blocks(page, regions)]
