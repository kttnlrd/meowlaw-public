#!/usr/bin/env python3
"""CDN-0171: rebuild mangled markdown tables in ITAA 1997 section files.

Uses PyMuPDF word positions to reconstruct table cells column-by-column,
eliminating the interleaving artifacts of pdftotext -layout.

Method:
  1. Locate the section's pages (find "Section X" body headers, fall back to
     running headers).
  2. On each page, find table blocks geometrically: runs of lines whose text
     runs respect a common set of column gutters (prose crosses them).
  3. Split each block into header / rows, re-joining rows split by a page
     break and keeping table note lines out of the cells.

CDN-0193 Phase 0 rewrote steps 2-3; see the geometry section below.
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


def words_for_page(page) -> list[dict]:
    """All words on a page sorted by (baseline y1, x0).

    Different fonts (asterisked terms) have different ascents, so y0 differs
    for the same visual line; baselines (y1) align across fonts.
    """
    out = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "t": t})
    out.sort(key=lambda w: (round(w["y1"], 2), w["x0"]))
    return out


def find_section_pages(doc, section: str) -> list[int]:
    """Pages containing a body header 'Section X', a running header, or the
    section's own heading line (e.g. '40-190 Second element of cost').
    TOC entries (leader dots + page numbers) are excluded.
    """
    pages = []
    pat_body = re.compile(rf"^Section\s+{re.escape(section)}\s*$", re.MULTILINE)
    pat_run = re.compile(rf"Section\s+{re.escape(section)}\s*$", re.MULTILINE)
    pat_own = re.compile(rf"^\s*{re.escape(section)}\s+\S", re.MULTILINE)
    for pno in range(doc.page_count):
        page = doc[pno]
        txt = page.get_text("text")
        # skip pages whose only mention is a TOC entry (leader dots)
        if pat_body.search(txt) or pat_run.search(txt):
            pages.append(pno)
            continue
        for ln in txt.splitlines():
            if ".." in ln:
                continue  # TOC leader line
            if re.match(rf"^\s*{re.escape(section)}\s+\S", ln):
                pages.append(pno)
                break
    # A table may start on the page BEFORE the first section marker (the
    # section body continues from the previous page) and may flow onto the
    # page after the last marker. Include both neighbours.
    if pages:
        first = pages[0]
        if first - 1 >= 0 and first - 1 not in pages:
            pages.insert(0, first - 1)
        last = pages[-1]
        nxt = last + 1
        if nxt < doc.page_count and nxt not in pages:
            pages.append(nxt)
    return pages


# ─────────────────────────────────────────────────────────────────────────────
# Geometry-driven table detection (CDN-0193 Phase 0 rewrite).
#
# Replaces the 'Item'-keyed block detection (R3), the static item_y+55 row cap
# (R5), the blanket y>=600 footer drop (R6) and the header-equality-only page
# merge (R4).  The unit of detection is a *text run*: words on one baseline
# separated by less than RUN_GAP.  Prose lines are one long run that crosses
# the table's column gutters; table lines are several runs that respect them.
# ─────────────────────────────────────────────────────────────────────────────

RUN_GAP = 6.0    # pt: word gap that ends a text run (column gutters are wider)
COL_TOL = 4.0    # pt: x tolerance when matching a run start to a column
ROW_Y_TOL = 3.0  # pt: baseline jitter between columns of the same visual row

# Table metadata that is part of the table's vertical flow but is NOT a cell
# (R6).  Previously dropped by FOOTER_Y_MIN.
NOTE_LINE_RE = re.compile(
    r"^\*?\s*To find definitions of asterisked terms|^\*?\s*To find definitions of \*",
    re.I,
)

# A column-0 token that identifies a row: 12, 12A, 3.2, (iv), (b), 43A.
IDENT_RE = re.compile(r"^\(?(?:\d+(?:[A-Za-z]{1,2}|\.\d+)*|[ivxlcdm]{1,6}|[A-Za-z])\)?$")


Y_JITTER = 0.5   # pt: baselines this close are the same visual line


def page_lines(page) -> list[dict]:
    """Words grouped into baseline lines: [{y, words}] sorted top→bottom.

    Baselines are clustered, not rounded: fonts on one visual line can differ
    by a fraction of a point (gst-1999 38-355 item 6 has 'tax zone' at 293.1
    and its own continuation at 292.9).  Exact-key bucketing made those two
    separate lines and emitted the right-hand fragment FIRST, scrambling the
    sentence — a reordering no gate can see, because every token is present.
    Line spacing is ~10pt, so Y_JITTER cannot merge different lines.
    """
    out: list[dict] = []
    for w in words_for_page(page):
        if out and w["y1"] - out[-1]["y"] <= Y_JITTER:
            out[-1]["words"].append(w)
        else:
            out.append({"y": w["y1"], "words": [w]})
    for ln in out:
        ln["words"].sort(key=lambda w: w["x0"])
    return out


def line_runs(line: dict) -> list[dict]:
    """Split a line's words into runs separated by gaps >= RUN_GAP."""
    runs = []
    cur = [line["words"][0]]
    for w in line["words"][1:]:
        if w["x0"] - cur[-1]["x1"] >= RUN_GAP:
            runs.append(cur)
            cur = [w]
        else:
            cur.append(w)
    runs.append(cur)
    return [{"x0": r[0]["x0"], "x1": r[-1]["x1"], "words": r} for r in runs]


def _cluster_xs(xs: list[float], tol: float) -> list[float]:
    out = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [min(c) for c in out]


def line_fits_columns(runs: list[dict], cols: list[float], strict: bool = False) -> bool:
    """True if every run starts in a column band and stays inside it.

    This is the prose/table discriminator: a prose line's single run starts
    somewhere in column i and runs on past column i+1's x, so it fails.

    The last column has no right-hand neighbour, so the loose test accepts any
    run indented past it.  With 3+ columns the earlier columns still pin the
    block down; with 2 columns almost every indented prose line "fits" and a
    block swallows whole neighbouring sections (fbt-1986 s58Q, gst-1999 s79-95).
    strict=True therefore demands the run START on a column, not merely after
    one.  Used only by the 2-run seeding pass.
    """
    for r in runs:
        idx = None
        for i, cx in enumerate(cols):
            if r["x0"] >= cx - COL_TOL:
                idx = i
        if idx is None:
            return False
        if strict and abs(r["x0"] - cols[idx]) > COL_TOL:
            return False
        if idx + 1 < len(cols) and r["x1"] > cols[idx + 1] + COL_TOL:
            return False
    return True


MIN_2COL_LINES = 4   # a 2-run seed only makes a block if this many lines agree


def detect_blocks(page) -> list[dict]:
    """Find table blocks on a page without assuming any header wording (R3).

    Seed on the line with the most runs (>=3 — prose tops out at 2 runs:
    a marker plus its indented text), take its run starts as the column set,
    then grow up and down while lines respect those columns.

    CDN-0193 Phase 1: a second pass seeds on 2-run lines, so two-column tables
    (`| Item | This term: |`, gst-1999 s3-5) are detected at all.  A 2-run seed
    is ambiguous with an indented marker+text prose line, so a block grown from
    one must be at least MIN_2COL_LINES lines long — (a)/(b)/Note: markers do
    not sustain two agreeing column starts for that many consecutive lines.
    """
    lines = page_lines(page)
    for ln in lines:
        ln["runs"] = line_runs(ln)
    used = [False] * len(lines)
    blocks = []

    def grow(seed, strict=False):
        cols = [r["x0"] for r in lines[seed]["runs"]]
        lo = hi = seed
        # two passes: grow, refine columns from all fitting multi-run lines, regrow
        for _ in range(2):
            lo = hi = seed
            while lo - 1 >= 0 and not used[lo - 1] and line_fits_columns(lines[lo - 1]["runs"], cols, strict):
                lo -= 1
            while hi + 1 < len(lines) and not used[hi + 1] and line_fits_columns(lines[hi + 1]["runs"], cols, strict):
                hi += 1
            starts = [r["x0"] for i in range(lo, hi + 1) for r in lines[i]["runs"]
                      if len(lines[i]["runs"]) >= 2]
            refined = _cluster_xs(starts, COL_TOL)
            # keep only column candidates seen on >= 2 lines (drop stray indents)
            support = Counter()
            for i in range(lo, hi + 1):
                for r in lines[i]["runs"]:
                    for cx in refined:
                        if abs(r["x0"] - cx) <= COL_TOL:
                            support[cx] += 1
                            break
            newcols = [c for c in refined if support[c] >= 2]
            if len(newcols) < 2 or newcols == cols:
                break
            cols = newcols
        return lo, hi, cols

    for min_runs in (3, 2):
        while True:
            seed = None
            for i, ln in enumerate(lines):
                if used[i] or len(ln["runs"]) < min_runs:
                    continue
                if seed is None or len(ln["runs"]) > len(lines[seed]["runs"]):
                    seed = i
            if seed is None:
                break

            lo, hi, cols = grow(seed, strict=min_runs == 2)
            for i in range(lo, hi + 1):
                used[i] = True
            nlines = hi - lo + 1
            if nlines < 3 or len(cols) < 2:
                continue
            if len(cols) == 2 and nlines < MIN_2COL_LINES:
                continue
            blocks.append({
                "page": page.number,
                "cols": cols,
                "lines": lines[lo:hi + 1],
                "tail": lines[hi + 1] if hi + 1 < len(lines) else None,
            })

    blocks.sort(key=lambda b: b["lines"][0]["y"])
    return blocks


def _col_of(x: float, cols: list[float]) -> int:
    idx = 0
    for i, cx in enumerate(cols):
        if x >= cx - COL_TOL:
            idx = i
    return idx


def split_block(block: dict, wraps: set | None = None) -> dict:
    """Separate a block into note lines, header lines and body lines (R6).

    Note lines ('*To find definitions of asterisked terms…') sit in the
    table's vertical flow but belong below the table, not in a cell.
    """
    notes, content = [], []
    for ln in block["lines"]:
        txt = " ".join(w["t"] for w in ln["words"])
        if NOTE_LINE_RE.match(txt):
            notes.append(txt)
        else:
            content.append(ln)

    cols = block["cols"]
    # The header is line 0 plus its wrap lines.  A wrap line has no column-0
    # run and still spans nearly every column ('Provisions Topic Regulator');
    # the lines that follow a page break carry only ONE column's worth of text
    # (the tail of the previous page's last row) and must stay in the body so
    # the continuation re-join can find them (R4).
    need = max(2, len(cols) - 1)
    hdr_end = 1 if content else 0
    while hdr_end < len(content):
        ln = content[hdr_end]
        touched = {_col_of(r["x0"], cols) for r in ln["runs"]}
        text = " ".join(w["t"] for w in ln["words"])
        if 0 in touched:
            break
        # a wrap line spans nearly every column, OR collect_tables has proved
        # by cross-page repetition that this exact line is part of the header
        if not (wraps and text in wraps) and len(touched) < need:
            break
        if ln["y"] - content[hdr_end - 1]["y"] > 16:
            break
        hdr_end += 1
    return {"notes": notes, "header": content[:hdr_end], "body": content[hdr_end:]}


def header_labels(header_lines: list[dict], cols: list[float]) -> list[str]:
    """Column labels, joining wrapped header lines per column."""
    parts: list[list[str]] = [[] for _ in cols]
    for ln in header_lines:
        for r in ln["runs"]:
            parts[_col_of(r["x0"], cols)].append(" ".join(w["t"] for w in r["words"]))
    return [" ".join(p) for p in parts]


def block_rows(block: dict, wraps: set | None = None) -> dict:
    """Rows for a block, plus the words that precede its first row.

    Those leading words are the tail of a row that started on the previous
    page (R4) — the old merge dropped them.  The last row now runs to the
    end of the detected block instead of item_y+55 (R5).
    """
    cols = block["cols"]
    parts = split_block(block, wraps)
    body = parts["body"]
    starts = [i for i, ln in enumerate(body)
              if any(_col_of(r["x0"], cols) == 0 for r in ln["runs"])]

    def cells_of(lines_slice):
        cells = [[] for _ in cols]
        for ln in lines_slice:
            for r in ln["runs"]:
                cells[_col_of(r["x0"], cols)].extend(w["t"] for w in r["words"])
        return cells

    # A column-3 cell often starts a hair ABOVE its own item number's
    # baseline (different font ascent), so a row claims lines from
    # ROW_Y_TOL above its own start line.
    def adj(i):
        y = body[i]["y"]
        j = i
        while j - 1 >= 0 and body[j - 1]["y"] >= y - ROW_Y_TOL and \
                not any(_col_of(r["x0"], cols) == 0 for r in body[j - 1]["runs"]):
            j -= 1
        return j

    bounds = [adj(i) for i in starts]
    lead = body[:bounds[0]] if bounds else body
    rows = []
    for n, b in enumerate(bounds):
        end = bounds[n + 1] if n + 1 < len(bounds) else len(body)
        cs = cells_of(body[b:end])
        rows.append({"item": " ".join(cs[0]), "cols": {i: " ".join(cs[i]) for i in range(1, len(cols))}})
    return {
        "header": header_labels(parts["header"], cols),
        "notes": parts["notes"],
        "rows": rows,
        "lead_cells": cells_of(lead) if lead else None,
        "ncols": len(cols),
        "tail_text": (" ".join(w["t"] for w in block["tail"]["words"]) if block["tail"] else ""),
        # G1 provenance: the header repeats on every page of a multi-page
        # table but is emitted once, so header and body words are kept apart
        # and only the first block's header counts.
        "header_words": [w["t"] for ln in parts["header"] for w in ln["words"]],
        "region_words": [w["t"] for ln in parts["body"] for w in ln["words"]],
    }


def collect_tables(doc, section: str, detail: bool = False) -> list[dict]:
    """All tables for a section.

    Blocks that continue the same logical table across a page break are merged
    and, crucially, a row split by the page break is RE-JOINED (R4): the words
    above the continuation page's first row are the tail of the previous
    page's last row, not a new row and not rubbish to drop.

    detail=True adds the fields the fidelity gate needs (region_words, notes,
    tail_text) alongside the JSON contract used by apply_itaa_table_fixes.py.
    """
    raw = [b for pno in find_section_pages(doc, section) for b in detect_blocks(doc[pno])]

    # A header that wraps onto a second line repeats that line on every page
    # of the table.  A row split by a page break does not — it cannot appear
    # on the table's FIRST page.  That asymmetry tells the two apart, which
    # naive geometry cannot (both are lines under the header with no entry in
    # the identifier column).  Without it the repeated wrap line is re-joined
    # into a row as if it were the tail of a page-split cell.
    def _txt(line):
        return " ".join(w["t"] for w in line["words"])

    wraps: set[str] = set()
    for _ in range(4):
        groups: dict[str, list[str | None]] = {}
        for b in raw:
            parts = split_block(b, wraps)
            if not parts["header"]:
                continue
            body = parts["body"]
            groups.setdefault(_txt(parts["header"][0]), []).append(
                _txt(body[0]) if body else None)
        new = {firsts[0] for firsts in groups.values()
               if firsts[0] and firsts.count(firsts[0]) >= 2} - wraps
        if not new:
            break
        wraps |= new

    out = []
    for block in raw:
            pno = block["page"]
            b = block_rows(block, wraps)
            if not b["rows"] or b["ncols"] < 2:
                continue
            b["page"] = pno + 1
            b["block_headers"] = [b["header"]]
            out.append(b)

    merged: list[dict] = []
    for t in out:
        prev = merged[-1] if merged else None
        same = prev is not None and (
            prev["header"] == t["header"]
            or (prev["ncols"] == t["ncols"] and not t["header"])
        )
        if same:
            if t["lead_cells"] and prev["rows"]:
                # re-join the page-split row (no repeated item number needed)
                last = prev["rows"][-1]
                for i in range(1, t["ncols"]):
                    extra = " ".join(t["lead_cells"][i]) if i < len(t["lead_cells"]) else ""
                    if extra:
                        last["cols"][i] = (last["cols"][i] + " " + extra).strip()
                if t["lead_cells"][0]:
                    last["item"] = (last["item"] + " " + " ".join(t["lead_cells"][0])).strip()
                t["lead_cells"] = None
            prev["rows"].extend(t["rows"])
            prev["notes"].extend(t["notes"])
            prev["region_words"].extend(t["region_words"])
            prev["block_headers"].extend(t["block_headers"])
            prev["tail_text"] = t["tail_text"]
            prev["page"] = f"{str(prev['page']).split('-')[0]}-{t['page']}"
        else:
            if t["lead_cells"]:
                # orphan continuation with nothing to attach to: surface it as a
                # row rather than losing it, and let the gate reject.
                t["rows"].insert(0, {"item": " ".join(t["lead_cells"][0]),
                                     "cols": {i: " ".join(t["lead_cells"][i])
                                              for i in range(1, t["ncols"])}})
                t["lead_cells"] = None
            merged.append(t)

    res = []
    for t in merged:
        d = {
            "page": t["page"],
            "cols": t["ncols"],
            "header": t["header"][1:],  # JSON contract: labels AFTER the id column
            "rows": [{"item": r["item"], "cols": {str(k): v for k, v in r["cols"].items()}}
                     for r in t["rows"]],
        }
        if detail:
            d["id_label"] = t["header"][0] if t["header"] else "Item"
            d["region_words"] = t["header_words"] + t["region_words"]
            d["notes"] = t["notes"]
            d["block_headers"] = t["block_headers"]
            d["tail_text"] = t["tail_text"]
        res.append(d)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("section")
    ap.add_argument("--json", action="store_true", help="emit JSON for the repair driver")
    args = ap.parse_args()

    if fitz is None:
        print("PyMuPDF not available", file=sys.stderr)
        return 2

    doc = fitz.open(args.pdf)
    tables = collect_tables(doc, args.section)
    if not tables:
        print(f"section {args.section}: no tables found", file=sys.stderr)
        return 1

    if args.json:
        import json
        print(json.dumps(tables, ensure_ascii=False))
        return 0

    print(f"section {args.section}: {len(tables)} tables")
    for t in tables:
        print(f"\n=== page {t['page']} cols={t['cols']} rows={len(t['rows'])} ===")
        cells = ["Item"] + list(t["header"])
        print("| " + " | ".join(cells) + " |")
        print("| " + " | ".join(["---"] * len(cells)) + " |")
        for r in t["rows"]:
            row = [r["item"]] + [r["cols"].get(str(i), "") for i in range(1, len(cells))]
            print("| " + " | ".join(row) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
