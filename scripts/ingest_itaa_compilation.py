#!/usr/bin/env python3
"""CDN-0193 Phase 2: the missing PDF -> section-markdown ingester.

The corpus is a PDF -> markdown conversion whose converter was never
committed.  Compilation 266 (commit 091945207) landed 4,666 section files with
no tooling and no check; the retro-run of scripts/corpus_change_guard.py over
that commit BLOCKs 199 of them and 106 are still flagged by C11 today.  This
script is the missing half: a conversion that can be re-run, inspected and
proven not to lose text.

The corruption signature it exists to make impossible: a region that is NOT a
table emitted as a pipe table with its content lost.  data/itaa-1997/.../
82-150.md shipped the section's legal formula as

    | termination payment | Employment | Days to |
    | --- | --- | --- |
     where: days to retirement is the number of days from ...

- the formula's own tokens gone, the following prose glued under the
separator.  Here, a region that cannot be PROVEN to be a well-formed table is
emitted verbatim inside a fenced ``ingest-formula`` / ``ingest-unclassified``
block, and every token on the section's pages is checked back out of the
emitted markdown before the file is written.

Invariants (checked, not asserted)
  I1  no token loss   every non-furniture token on the section's pages is
                      present in the emitted markdown.  A section that fails
                      is written with status INCOMPLETE and listed in the
                      report's "unrepresented"; it never ships silently.
  I2  no fake tables  a pipe table is emitted only with a header separator,
                      >= 2 data rows, a constant cell count, no G5 formula
                      glyphs, and G1 token provenance (imported from
                      scripts/table_rebuild_gate.py) passing against the PDF
                      table band it was built from.
  I3  compilation     compilation_no comes from each volume's OWN footer
                      ("Compilation No. NNN").  A volume whose footer
                      disagrees with --comp ABORTS the run.
  I4  determinism     same input -> byte-identical output.

Output goes to --out, a staging tree OUTSIDE the repo worktree.  Nothing here
writes under data/.  See --stage for the handoff into
scripts/table_rebuild_staging.py.

Run under /usr/bin/python3.12 (python3.11 has no fitz).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import extract_itaa_tables_pdf as EX          # noqa: E402  geometry, block_rows
import table_rebuild_gate as GATE             # noqa: E402  G1/G5 semantics

GLYPH_RE = GATE.GLYPH_RE
norm_tokens = GATE.norm_tokens


class IngestAbort(RuntimeError):
    """The run cannot be trusted; nothing was written."""


# ── page geometry ───────────────────────────────────────────────────────────
# Measured on the comp-266 volumes: running headers end at y~107, body runs
# 120..600, the footer rule sits at y~608.  The bands are only ever used
# TOGETHER with the pattern tests below — a line outside the body band that
# matches no furniture pattern is a hard error, not a silent drop.
BODY_Y_MIN = 118.0
BODY_Y_MAX = 600.0


def body_band(page) -> tuple[float, float]:
    """The body band for THIS page.

    The constants above were measured on the 841.9pt portrait page.  A
    landscape page is 841.9 x 595.3, so BODY_Y_MAX is off the bottom of it and
    the whole page — footer included — lands inside the "body" band.  The band
    therefore stops at the page edge, and on such a page the footer is peeled
    off by CONTENT (peel_footer), not by a y-coordinate: the rule sits at
    y=474.7 in vol01 and y=418.1 in vol02 while body text on vol01 runs to
    y=435.3, so no single landscape cut-off can do the job without dropping
    real text.  Portrait pages are untouched: min(600, 841.9) == 600.
    """
    return BODY_Y_MIN, min(BODY_Y_MAX, page.rect.height)


def is_footer(text: str) -> bool:
    return any(p.search(text) for p in FOOTER_PATS)


def peel_footer(body: list[dict]) -> list[dict]:
    """Move trailing furniture lines off the end of a short page's body.

    Bottom-up and only while each line matches a furniture pattern, so the
    first line that is not provably furniture stops the peel and stays in the
    body.  Text is never dropped on a guess.
    """
    peeled: list[dict] = []
    while body and is_footer(line_text(body[-1]).strip()):
        peeled.insert(0, body.pop())
    return peeled

HEADER_PATS = [
    re.compile(r"^Section\s+\S+$"),
    re.compile(r"^(Chapter|Part|Division|Subdivision)\s+[\w-]+\b"),
    re.compile(r"\b(Chapter|Part|Division|Subdivision)\s+[\w-]+$"),
]
FOOTER_PATS = [
    re.compile(r"^[_\-—–]{6,}$"),
    EX.NOTE_LINE_RE,
    re.compile(r"^Compilation No\.\s*\d+"),
    re.compile(r"^Authorised Version\b"),
    re.compile(r"^\d+\s+Income Tax Assessment Act\b"),
    re.compile(r"^Income Tax Assessment Act .*\d+$"),
    re.compile(r"^\d+$"),
]

COMP_NO_RE = re.compile(r"Compilation No\.\s*(\d+)")
COMP_DATE_RE = re.compile(r"Compilation date:\s*(\d{2})/(\d{2})/(\d{4})")
SECTION_ID_RE = r"\d+[A-Z]*(?:-\d+[A-Z]*)+"
STRUCT_RE = re.compile(r"^(Chapter|Part|Division|Subdivision)\s+(\S+?)[—–]\s*(.*)$")

STALE = re.compile(r"C2026C00122VOL", re.I)


def line_text(ln: dict) -> str:
    return " ".join(w["t"] for w in ln["words"])


def classify_lines(page) -> tuple[list[dict], list[dict], list[dict]]:
    """-> (body, furniture, suspect).

    The bands are only trusted when the page PROVES it has the expected
    furniture: the header band must contain a running header line and the
    footer band the 'Compilation No.' line.  A band that proves nothing is
    returned as `suspect`, which makes the section INCOMPLETE rather than
    dropping text on a guess.  Wrapped furniture lines ('payments' under
    'Part 2-40 Rules affecting ...') are then furniture by band, which is what
    they are — no per-line pattern can recognise them.
    """
    lo_y, hi_y = body_band(page)
    body, head, foot = [], [], []
    for ln in EX.page_lines(page):
        if not line_text(ln).strip():
            continue
        (body if lo_y <= ln["y"] <= hi_y
         else head if ln["y"] < lo_y else foot).append(ln)
    if hi_y < BODY_Y_MAX:           # landscape: the band cannot see the footer
        foot = peel_footer(body) + foot
    suspect = []
    if head and not any(p.search(line_text(l).strip()) for l in head for p in HEADER_PATS):
        suspect += head
        head = []
    if foot and not any(p.search(line_text(l).strip()) for l in foot for p in FOOTER_PATS):
        suspect += foot
        foot = []
    return body, head + foot, suspect


# ── volume level ────────────────────────────────────────────────────────────
def verify_compilation(doc, pdf: Path, comp: int, comp_date: str | None) -> dict:
    """I3: the compilation number comes from the PDF's own footer."""
    nos, dates = Counter(), Counter()
    for pno in range(doc.page_count):
        txt = doc[pno].get_text("text")
        for m in COMP_NO_RE.finditer(txt):
            nos[int(m.group(1))] += 1
        for m in COMP_DATE_RE.finditer(txt):
            dates[f"{m.group(3)}-{m.group(2)}-{m.group(1)}"] += 1
    if not nos:
        raise IngestAbort(f"{pdf.name}: no 'Compilation No.' footer found — "
                          f"cannot verify the source compilation")
    if len(nos) > 1:
        raise IngestAbort(f"{pdf.name}: footer disagrees with itself: {dict(nos)}")
    found = next(iter(nos))
    if found != comp:
        raise IngestAbort(
            f"{pdf.name}: footer says Compilation No. {found} but --comp is {comp} — "
            f"ABORTING (this is exactly how a stale volume gets ingested over a "
            f"newer corpus)")
    found_date = next(iter(dates)) if len(dates) == 1 else None
    if comp_date and found_date and found_date != comp_date:
        raise IngestAbort(f"{pdf.name}: footer compilation date {found_date} "
                          f"!= --comp-date {comp_date} — ABORTING")
    return {"compilation_no": found, "compilation_date": found_date or comp_date}


def index_volume(doc) -> dict:
    """One sequential pass: structure headings and section headings, in order.

    Section headings are 12pt bold and start on the left margin; the running
    header 'Section 82-150' is 12pt but NOT bold, and sits above the body
    band.  Structure headings ('Part 2-40—Rules affecting ...') are >=13pt and
    wrap onto following lines at the same size, which the corpus's own
    frontmatter truncates away; the full title is joined here.
    """
    anchors: list[dict] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        spans: list[dict] = []
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    if s["text"].strip():
                        spans.append({"y": round(s["bbox"][3], 1), "size": round(s["size"], 1),
                                      "bold": "Bold" in s["font"], "x": s["bbox"][0],
                                      "t": s["text"]})
        band_lo, band_hi = body_band(page)
        spans.sort(key=lambda s: (s["y"], s["x"]))
        # group spans into visual lines
        vlines: list[dict] = []
        for s in spans:
            if vlines and abs(s["y"] - vlines[-1]["y"]) <= 1.0:
                # Two spans on one baseline are a bold/roman split, not a word
                # break — except when welding them would invent a word that is
                # not in the PDF ("pre-July 83" + "segment" -> "83segment" in
                # 82-155's title).  A space goes in only for alnum|alnum; every
                # other boundary (emphasis "*business", hyphen "pre-" + "July",
                # punctuation) carries its own spacing already.
                if vlines[-1]["t"][-1:].isalnum() and s["t"][:1].isalnum():
                    vlines[-1]["t"] += " "
                vlines[-1]["t"] += s["t"]
                vlines[-1]["size"] = max(vlines[-1]["size"], s["size"])
                vlines[-1]["bold"] |= s["bold"]
            else:
                vlines.append(dict(s))
        i = 0
        while i < len(vlines):
            v = vlines[i]
            txt = v["t"].strip()
            if v["y"] < band_lo or v["y"] > band_hi:
                i += 1
                continue
            m = STRUCT_RE.match(txt)
            if m and v["size"] >= 13.0:
                title, j = m.group(3).strip(), i + 1
                while j < len(vlines) and abs(vlines[j]["size"] - v["size"]) < 0.1 \
                        and vlines[j]["y"] - vlines[j - 1]["y"] < 22 \
                        and not vlines[j]["t"].strip().startswith("Guide to") \
                        and not STRUCT_RE.match(vlines[j]["t"].strip()) \
                        and not re.match(rf"^{SECTION_ID_RE}\s", vlines[j]["t"].strip()):
                    title += " " + vlines[j]["t"].strip()
                    j += 1
                anchors.append({"kind": m.group(1).lower(), "id": m.group(2).strip(),
                                "title": " ".join(title.split()), "page": pno, "y": v["y"]})
                i = j
                continue
            m = re.match(rf"^({SECTION_ID_RE})\s+(\S.*)$", txt)
            if m and v["bold"] and 11.5 <= v["size"] <= 12.5:
                title, j = m.group(2).strip(), i + 1
                while j < len(vlines) and vlines[j]["bold"] \
                        and abs(vlines[j]["size"] - v["size"]) < 0.1 \
                        and vlines[j]["y"] - vlines[j - 1]["y"] < 20:
                    title += " " + vlines[j]["t"].strip()
                    j += 1
                anchors.append({"kind": "section", "id": m.group(1),
                                "title": " ".join(title.split()), "page": pno,
                                "y": v["y"], "head_end_y": vlines[j - 1]["y"]})
                i = j
                continue
            i += 1
    # attach the structural context each section inherits
    ctx: dict[str, tuple[str, str]] = {}
    sections: dict[str, dict] = {}
    for n, a in enumerate(anchors):
        if a["kind"] != "section":
            ctx[a["kind"]] = (a["id"], a["title"])
            if a["kind"] == "part":
                ctx.pop("division", None)
                ctx.pop("subdivision", None)
            if a["kind"] == "division":
                ctx.pop("subdivision", None)
            continue
        nxt = anchors[n + 1] if n + 1 < len(anchors) else None
        if a["id"] in sections:
            continue  # first occurrence wins; a repeat is a TOC/notes echo
        sections[a["id"]] = {
            "id": a["id"], "title": a["title"], "page": a["page"], "y": a["y"],
            "head_end_y": a["head_end_y"],
            "end_page": nxt["page"] if nxt else doc.page_count - 1,
            "end_y": nxt["y"] if nxt else 1e9,
            "ctx": {k: v for k, v in ctx.items()},
        }
    return sections


# ── region classification ───────────────────────────────────────────────────
def _glyph_hits(text: str) -> list[str]:
    return [m.group(0) for m in GLYPH_RE.finditer(text)
            if not GATE.SCANNER._is_natural_word_accent(text, m)]


def table_regions(page, lo_y: float, hi_y: float) -> list[dict]:
    """PDF table bands on this page that fall inside [lo_y, hi_y].

    EX.detect_blocks is the geometry the fidelity gate already trusts; a band
    is a *candidate* table here, never a conclusion.
    """
    out = []
    band_lo, band_hi = body_band(page)
    for b in EX.detect_blocks(page):
        ys = [l["y"] for l in b["lines"]]
        if min(ys) < max(lo_y, band_lo) or max(ys) > min(hi_y, band_hi):
            continue
        out.append({"lo": min(ys), "hi": max(ys), "block": b})
    return sorted(out, key=lambda r: r["lo"])


PROSE_MARKER_RE = re.compile(
    r"^([•·▪]$|\(\d+[A-Z]*\)|\([a-z]{1,4}\)|\([ivxlcdm]{1,6}\)|"
    r"(Notes?|Examples?|Exceptions?|Step|Method|Table)\b)")


def band_is_prose(lines: list[dict], cols: list[float]) -> bool:
    """A 2-column 'table' whose left column is only prose markers is prose.

    Legislative prose indents a marker — (a), Note 1:, Step 2. — and its text
    at two constant x positions, which is geometrically indistinguishable from
    a two-column table (EX.detect_blocks says as much in MIN_2COL_LINES).  The
    left column's CONTENT tells them apart: a real table's first column holds
    row identifiers and text, never 'Note'/'Step'.
    """
    if len(cols) != 2:
        return False
    firsts = []
    for ln in lines:
        runs = EX.line_runs(ln)
        if runs and EX._col_of(runs[0]["x0"], cols) == 0:
            firsts.append(" ".join(w["t"] for w in runs[0]["words"]).strip())
    ok = lambda f: PROSE_MARKER_RE.match(f) or re.match(rf"^{SECTION_ID_RE}$", f)
    return bool(firsts) and all(ok(f) for f in firsts)


# A rendered formula or figure puts several baselines inside one visual line,
# so its line gaps are far below the body's single-line leading (12.6-13.0pt;
# small-print Note paragraphs are 10.3pt).  That is the geometric signature of
# the 82-150 corruption class, and it is what detect_blocks mistook for a
# table.
SUBLEADING = 8.0    # pt: a gap this small is not a line break
LEADING_MIN = 11.0  # pt: grow the band while gaps stay under normal leading


def formula_bands(lines: list[dict]) -> list[tuple[float, float]]:
    """Maximal runs of lines whose baselines are packed tighter than prose."""
    seeds = set()
    for i in range(1, len(lines)):
        if lines[i]["y"] - lines[i - 1]["y"] < SUBLEADING:
            seeds |= {i - 1, i}
    for i, ln in enumerate(lines):
        if _glyph_hits(line_text(ln)):
            seeds.add(i)
    bands, i = [], 0
    while i < len(lines):
        if i not in seeds:
            i += 1
            continue
        lo = hi = i
        while lo - 1 >= 0 and lines[lo]["y"] - lines[lo - 1]["y"] < LEADING_MIN:
            lo -= 1
        while hi + 1 < len(lines) and lines[hi + 1]["y"] - lines[hi]["y"] < LEADING_MIN:
            hi += 1
        bands.append((lines[lo]["y"], lines[hi]["y"]))
        i = hi + 1
    return bands


def build_table(block: dict) -> tuple[list[str] | None, str]:
    """I2.  -> (markdown lines, "") or (None, reason it is not a table)."""
    try:
        b = EX.block_rows(block)
    except Exception as exc:                       # geometry can't parse it
        return None, f"block_rows failed: {exc}"
    if b["ncols"] < 2:
        return None, f"only {b['ncols']} column(s)"
    if not b["header"] or not all(h.strip() for h in b["header"]):
        # No invented headers.  A blank header cell means the geometry did not
        # read the header, and a placeholder would be text this script made up
        # — which is exactly the kind of fabrication G1 exists to refuse.
        return None, f"header cell(s) empty: {b['header']}"
    if len(b["rows"]) < 2:
        return None, f"{len(b['rows'])} data row(s), need >= 2"
    if b["lead_cells"]:
        return None, "block starts mid-row (unresolved page-split continuation)"
    header = [h.strip() for h in b["header"]]
    rows = [[r["item"]] + [r["cols"].get(i, "") for i in range(1, b["ncols"])]
            for r in b["rows"]]
    if any(len(r) != len(header) for r in rows):
        return None, "inconsistent cell counts"
    if any(not any(c.strip() for c in r) for r in rows):
        return None, "empty data row"
    # A bullet list or a run of 'Note n:' paragraphs is two constant x
    # positions, i.e. geometrically a two-column table.  It is not one, and
    # emitting it as pipe syntax is the 82-150 corruption in another costume
    # (corpus_change_guard G-B/G-C catch it, which is how this check exists).
    items = [header[0]] + [r[0].strip() for r in rows]
    if all(PROSE_MARKER_RE.match(i) for i in items if i):
        return None, f"first column is prose markers, not row ids: {items[:4]}"
    # A Guide's 'Table of sections' listing has no header: its first line is
    # already data, so emitting it as a table promotes a row to a header and
    # loses the fact that it was one.
    if re.match(rf"^{SECTION_ID_RE}$", header[0]) or EX.IDENT_RE.match(header[0]):
        return None, f"header row is itself a data row: {header}"
    cells = " ".join(header) + " " + " ".join(c for r in rows for c in r)
    if "|" in cells:
        return None, "cell content contains a '|'"
    hits = _glyph_hits(cells)
    if hits:
        return None, f"formula glyph(s) {sorted(set(hits))} in the cells (G5)"
    md = ["| " + " | ".join(header) + " |",
          "| " + " | ".join(["---"] * len(header)) + " |"]
    md += ["| " + " | ".join(c.strip() for c in r) + " |" for r in rows]
    g1 = GATE.g1_token_provenance(b["header_words"] + b["region_words"], md)
    if not g1["pass"]:
        return None, f"G1 token provenance failed: {g1['reason'][:200]}"
    # Last word to the C11 detectors that measure the corpus backlog: a cell
    # cut off mid-sentence means the band ended inside a row (a sub-paragraph's
    # indentation breaks the column geometry).  Such a table is wrong even
    # though every token in the BAND is accounted for, so it is preserved
    # verbatim instead.
    c11 = GATE.SCANNER.table_coherence_findings(md, "ingest-candidate")
    if c11:
        return None, "C11 coherence: " + "; ".join(f["detail"][:90] for f in c11[:2])
    return md, ""


# ── prose emission ──────────────────────────────────────────────────────────
SUBSEC_RE = re.compile(r"^\((\d+[A-Z]*)\)$")
PARA_RE = re.compile(r"^\(([a-z]{1,4}|[ivxlcdm]{1,6})\)$")
NOTE_RE = re.compile(r"^(Note|Example|Exception)s?(\s+\d+)?:$")


def _join(words: list[str]) -> str:
    """Join PDF words, gluing the superscript '*' of an asterisked term."""
    out: list[str] = []
    glue = False
    for w in words:
        if w == "*":
            glue = True
            continue
        out.append(("*" + w) if glue else w)
        glue = False
    if glue:
        out.append("*")
    return " ".join(out)


# ── output normalisation: the two defects the corpus repair had to undo (2026-09-19) ──
# 1. duplicate anchors. Every subsec/para paragraph emitted its <a id> unconditionally, so a
#    subsection whose text resumes after an interleaved table emitted the same id twice - 449 of
#    4,629 sections after the full apply, and a fragment URL only ever resolves to the first match.
#    The FIRST occurrence is the one that belongs at the subparagraph's start, so later copies go.
# 2. quotes. The source PDFs carry typographic quotes; copying them verbatim broke this corpus's
#    deliberate straight-quote convention (a59a3285e) in 1,762 files.
# Applied to the assembled text BEFORE the gate runs, so what the gate verifies is what gets written.
CURLY_QUOTES = "\u201c\u201d\u2018\u2019"
STRAIGHT_QUOTES = "\"\"''"
_QUOTE_MAP = {ord(c): s for c, s in zip(CURLY_QUOTES, STRAIGHT_QUOTES)}
_ANCHOR_RE = re.compile(r'<a id="([^"]+)"></a>')


def finalise(text: str) -> str:
    """Enforce the corpus conventions this ingester must not violate (see above)."""
    seen: set[str] = set()
    out_lines: list[str] = []
    for ln in text.splitlines():
        ids = _ANCHOR_RE.findall(ln)
        if ids:
            keep = []
            for aid in ids:
                if aid not in seen:
                    seen.add(aid)
                    keep.append(aid)
            if len(keep) != len(ids):
                if not keep:
                    stripped = _ANCHOR_RE.sub("", ln)
                    if not stripped.strip():
                        continue                  # the line was only the duplicate hook
                    ln = stripped
                else:
                    it = iter(keep)
                    ln = _ANCHOR_RE.sub(lambda m: (m.group(0) if next(it, None) else ""), ln)
        out_lines.append(ln)
    body = "\n".join(out_lines).translate(_QUOTE_MAP)
    return body + ("\n" if text.endswith("\n") else "")


def emit_prose(lines: list[dict], section: str) -> list[str]:
    """Markdown for a run of non-table lines, in the corpus's own shape."""
    paras: list[dict] = []
    for ln in lines:
        words = [w["t"] for w in ln["words"]]
        first = words[0]
        kind, marker, rest = "text", None, words
        if SUBSEC_RE.match(first):
            kind, marker, rest = "subsec", SUBSEC_RE.match(first).group(1), words[1:]
        elif PARA_RE.match(first) and paras:
            kind, marker, rest = "para", PARA_RE.match(first).group(1), words[1:]
        elif first in "•·▪" and len(first) == 1:
            kind, marker, rest = "bullet", None, words[1:]
        elif NOTE_RE.match(" ".join(words[:2]).strip()) or NOTE_RE.match(first):
            n = 2 if NOTE_RE.match(" ".join(words[:2]).strip()) else 1
            kind, marker, rest = "note", " ".join(words[:n]).rstrip(":"), words[n:]
        if kind == "text" and paras:
            paras[-1]["words"] += words
        else:
            paras.append({"kind": kind, "marker": marker, "words": list(rest)})

    out: list[str] = []
    cur_sub = None
    for p in paras:
        text = _join(p["words"]).strip()
        if p["kind"] == "subsec":
            cur_sub = p["marker"]
            out += ["", f'<a id="s{section}-{cur_sub}"></a>',
                    f'**({cur_sub})** {text}'.rstrip()]
        elif p["kind"] == "para":
            aid = f's{section}-{cur_sub}-{p["marker"]}' if cur_sub else f's{section}-{p["marker"]}'
            out += ["", f'> <a id="{aid}"></a>', f'> **({p["marker"]})** {text}'.rstrip()]
        elif p["kind"] == "bullet":
            # the glyph stays: I1 is a mechanical token check and rewriting
            # '•' to '-' would be the first of the exemptions that let the old
            # pipeline lose text.  One blank-line-separated line per bullet.
            out += ["", f"• {text}".rstrip()]
        elif p["kind"] == "note":
            out += ["", f'> **{p["marker"]}:** {text}'.rstrip()]
        elif text:
            out += ["", text]
    return out


def emit_preserved(lines: list[dict], pdf_name: str, page: int) -> tuple[list[str], str]:
    """(a): everything the ingester cannot PROVE is a table, kept verbatim.

    Never pipe syntax.  A fenced block with an explicit marker, one PDF line
    per output line, in reading order, so the tokens are all there and a human
    can see what the geometry actually was.
    """
    body = [_join([w["t"] for w in ln["words"]]) for ln in lines]
    kind = "formula" if _glyph_hits(" ".join(body)) else "unclassified"
    fence = "```"
    while any(fence in b for b in body):
        fence += "`"
    return ([fence + f"ingest-{kind} source={pdf_name} page={page}"] + body + [fence]), kind


# ── section ingest ──────────────────────────────────────────────────────────
FM_KEYS = ("act", "part", "part_title", "division", "division_title",
           "subdivision", "subdivision_title", "section", "section_title",
           "compilation_no", "compilation_date", "source_pdf")


def _yaml(v) -> str:
    return str(v) if isinstance(v, int) else '"' + str(v).replace('"', '\\"') + '"'


def ingest_section(doc, meta: dict, act_name: str, comp: dict, pdf_name: str) -> dict:
    sec = meta["id"]
    ctx = meta["ctx"]
    lines_by_page: list[tuple[int, list[dict]]] = []
    suspects: list[str] = []
    for pno in range(meta["page"], meta["end_page"] + 1):
        body, _furn, susp = classify_lines(doc[pno])
        lo = meta["y"] - 0.5 if pno == meta["page"] else -1e9
        hi = meta["end_y"] - 0.5 if pno == meta["end_page"] else 1e9
        keep = [l for l in body if lo <= l["y"] <= hi]
        if pno == meta["page"]:
            # the heading is emitted as '# id  title'; keeping its PDF lines as
            # body prose duplicates it into the first paragraph
            keep = [l for l in keep if l["y"] > meta["head_end_y"] + 0.5]
        if keep:
            suspects += [f"p{pno + 1}: {line_text(s)[:80]!r}" for s in susp]
        lines_by_page.append((pno, keep))

    body_md: list[str] = []
    preserved: list[dict] = []
    tables = 0
    pdf_tokens: Counter = Counter()

    for pno, lines in lines_by_page:
        if not lines:
            continue
        for ln in lines:
            pdf_tokens.update(norm_tokens(line_text(ln)))
        bands = table_regions(doc[pno], min(l["y"] for l in lines),
                              max(l["y"] for l in lines))
        outside = [l for l in lines
                   if not any(b["lo"] <= l["y"] <= b["hi"] for b in bands)]
        fbands = formula_bands(outside)
        idx, buf = 0, []
        while idx < len(lines):
            y = lines[idx]["y"]
            band = next((b for b in bands if b["lo"] <= y <= b["hi"]), None)
            if band is None:
                fb = next((f for f in fbands if f[0] <= y <= f[1]), None)
                if fb is None:
                    buf.append(lines[idx])
                    idx += 1
                    continue
                run = [l for l in lines if fb[0] <= l["y"] <= fb[1]]
                if buf:
                    body_md += emit_prose(buf, sec)
                    buf = []
                chunk, kind = emit_preserved(run, pdf_name, pno + 1)
                body_md += [""] + chunk
                preserved.append({"page": pno + 1, "kind": kind, "lines": len(run),
                                  "reason": "baselines packed tighter than body "
                                            "leading — rendered formula or figure, "
                                            "not a table"})
                idx = lines.index(run[-1]) + 1
                continue
            run = [l for l in lines if band["lo"] <= l["y"] <= band["hi"]]
            md, why = build_table(band["block"])
            if md is None and band_is_prose(run, band["block"]["cols"]):
                buf += run                       # geometry said table, content says prose
            else:
                if buf:
                    body_md += emit_prose(buf, sec)
                    buf = []
                if md:
                    body_md += [""] + md
                    tables += 1
                else:
                    block_lines = [l for l in run
                                   if not EX.NOTE_LINE_RE.match(line_text(l))]
                    chunk, kind = emit_preserved(block_lines, pdf_name, pno + 1)
                    body_md += [""] + chunk
                    preserved.append({"page": pno + 1, "kind": kind, "reason": why,
                                      "lines": len(block_lines)})
            idx = lines.index(run[-1]) + 1
        if buf:
            body_md += emit_prose(buf, sec)

    # the section's own heading line is prose-emitted above; strip that copy
    # and use the indexed title, so the '# id  title' heading is canonical.
    head = f"{sec}  {meta['title']}"
    body_md = [l for l in body_md if l.strip() != f"{sec} {meta['title']}"]

    fm = {
        "act": act_name,
        "part": ctx.get("part", ("", ""))[0], "part_title": ctx.get("part", ("", ""))[1],
        "division": ctx.get("division", ("", ""))[0],
        "division_title": ctx.get("division", ("", ""))[1],
        "subdivision": ctx.get("subdivision", ("", ""))[0],
        "subdivision_title": ctx.get("subdivision", ("", ""))[1],
        "section": sec, "section_title": meta["title"],
        "compilation_no": comp["compilation_no"],
        "compilation_date": comp["compilation_date"],
        "source_pdf": pdf_name,
    }
    out = ["---"] + [f"{k}: {_yaml(fm[k])}" for k in FM_KEYS] + ["---", f"# {head}"]
    out += body_md
    out += ["", "---",
            f"*Last updated: {comp['compilation_date']} "
            f"(Compilation {comp['compilation_no']})*"]
    text = "\n".join(l.rstrip() for l in out).replace("\n\n\n", "\n\n") + "\n"
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")

    text = finalise(text)      # dedupe anchors + straighten quotes BEFORE the gate below, so the
                               # gate's verdict describes the bytes that get written

    # I1: every PDF token back out of the markdown
    checked = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    checked = re.sub(r'<a id="[^"]*"></a>', " ", checked)
    # Blank markup-role characters only. '|' is always a cell delimiter, but '>' and
    # '#' are markers only when line-leading: blanking every '>' erased real
    # inequalities ("> 0.25% of total core shipping income") from the comparison, so a
    # present one read as lost and a dropped one was undetectable.
    def _blank_markup(text: str) -> str:
        out = []
        for ln in text.splitlines():
            ln = re.sub(r"^(?:\s*>)+\s?", " ", ln)
            ln = re.sub(r"^\s*#+\s?", " ", ln)
            out.append(ln.replace("|", " "))
        return "\n".join(out)

    checked = _blank_markup(checked)
    checked = re.sub(r"^```.*$", " ", checked, flags=re.M)
    # Per LINE, exactly like the PDF side.  norm_tokens de-hyphenates across a
    # newline, and a '| --- |' separator whose pipes have just been blanked
    # ends in '-', so a whole-text compare silently welds the separator onto
    # the first cell of the next row and reports phantom losses.
    md_tokens = Counter()
    for ln in checked.splitlines():
        md_tokens.update(norm_tokens(ln))
    missing = pdf_tokens - md_tokens
    status = "OK"
    if missing or suspects:
        status = "INCOMPLETE"
    elif preserved:
        status = "NEEDS_REVIEW"
    return {
        "section": sec, "status": status, "text": text,
        "pages": f"{meta['page'] + 1}-{meta['end_page'] + 1}",
        "tables": tables, "preserved_regions": preserved,
        "unrepresented": sorted(missing.elements())[:50],
        "unrepresented_count": sum(missing.values()),
        "off_band_lines": suspects,
        "pdf_tokens": sum(pdf_tokens.values()),
    }


# ── driver ──────────────────────────────────────────────────────────────────
def section_path(act: str, sec: dict, fm_ctx: dict) -> str:
    part = fm_ctx.get("part", ("", ""))[0] or "unknown"
    div = fm_ctx.get("division", ("", ""))[0] or "unknown"
    return f"{act}/sections/part-{part}/division-{div}/{sec['id']}.md"


def run(args) -> dict:
    import fitz
    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    if STALE.search(str(pdf_dir)) or any(STALE.search(p.name) for p in pdf_dir.glob("*.pdf")):
        raise IngestAbort(f"{pdf_dir} holds the STALE comp-263 volumes "
                          f"(C2026C00122VOL*) — refusing to ingest them")
    vols = sorted(pdf_dir.glob("*.pdf"))
    if args.volume:
        vols = [p for p in vols if p.name == args.volume]
        if not vols:
            raise IngestAbort(f"--volume {args.volume} not found in {pdf_dir}")
    if not vols:
        raise IngestAbort(f"no PDFs in {pdf_dir}")

    want = set(args.sections.split(",")) if args.sections else None
    out_root = Path(args.out).expanduser().resolve()
    if out_root == REPO or REPO in out_root.parents:
        raise IngestAbort(f"--out {out_root} is inside the repo worktree — "
                          f"ingest output is staged OUTSIDE the live corpus")

    t0 = time.time()
    results, files = [], {}
    for pdf in vols:
        doc = fitz.open(pdf)
        comp = verify_compilation(doc, pdf, args.comp, args.comp_date)
        index = index_volume(doc)
        todo = [s for s in index.values() if want is None or s["id"] in want]
        todo.sort(key=lambda s: (s["page"], s["y"]))
        if args.limit:
            todo = todo[:args.limit]
        for meta in todo:
            r = ingest_section(doc, meta, args.act_name, comp, pdf.name)
            r["volume"] = pdf.name
            rel = section_path(args.act, meta, meta["ctx"])
            r["path"] = rel
            files[rel] = r.pop("text")
            results.append(r)
        doc.close()

    for rel, text in sorted(files.items()):
        p = out_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    elapsed = time.time() - t0
    by = Counter(r["status"] for r in results)
    report = {
        "act": args.act, "comp": args.comp, "pdf_dir": str(pdf_dir),
        "volumes": [p.name for p in vols], "out": str(out_root),
        "sections": len(results), "status_counts": dict(by),
        "sections_per_minute": round(len(results) / elapsed * 60, 1) if elapsed else None,
        "elapsed_seconds": round(elapsed, 1),
        "results": sorted(results, key=lambda r: r["path"]),
    }
    return report


def stage_all(report: dict, out_root: Path, corpus: Path, act: str) -> list[str]:
    """(e) Documented handoff into scripts/table_rebuild_staging.py.

    One staged candidate per ingested section: source = the live corpus file,
    output = the ingested file.  A section with any preserved region is staged
    with an explicit risk flag so the convention BLOCKs it pending review.
    Sections with no live corpus counterpart are skipped (nothing to diff).
    NOTE: this is the per-TABLE handoff — it can only produce a
    table_rebuild_gate report, and that gate needs a PDF table band, so
    table-less sections land here as FAIL.  For a whole-section re-ingest run
    (the ~4,100 sections with no table at all), gate with
    scripts/ingest_reingest_gate.py --json and stage with
    `table_rebuild_staging.py stage-run --gate-json DIR`, which takes the
    re-ingest certificate instead.
    """
    import table_rebuild_staging as ST
    gate_dir = out_root / "_gate"
    gate_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for r in report["results"]:
        src = corpus / r["path"]
        if not src.is_file():
            continue
        gate_json = None
        if r["tables"]:
            pdf = Path(report["pdf_dir"]) / r["volume"]
            try:
                table = GATE.extract_table(pdf, r["section"])
            except Exception:
                table = None
            if table is not None:
                rep = GATE.gate_file(act, src, out_root / r["path"], table)
                gate_json = gate_dir / f"{r['section']}.json"
                gate_json.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
        flags = []
        if any(x["kind"] == "formula" for x in r["preserved_regions"]):
            flags.append("formula_glyphs")
        if r["preserved_regions"]:
            flags.append("ambiguous_page_mapping")
        ST.stage(act, r["section"], src, out_root / r["path"],
                 gate_report=gate_json, flags=flags, force=True)
        done.append(f"{act}/{r['section']}")
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--act", required=True, help="corpus act slug, e.g. itaa-1997")
    ap.add_argument("--act-name", default="ITAA 1997", help="frontmatter 'act' value")
    ap.add_argument("--comp", required=True, type=int)
    ap.add_argument("--comp-date", help="YYYY-MM-DD, cross-checked against the footer")
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--out", required=True, help="staging tree OUTSIDE the worktree")
    ap.add_argument("--sections", help="comma-separated section ids")
    ap.add_argument("--volume", help="single volume file name, e.g. vol03.pdf")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--report", help="write the JSON report here")
    ap.add_argument("--stage", action="store_true",
                    help="hand the output to scripts/table_rebuild_staging.py")
    a = ap.parse_args()

    try:
        report = run(a)
    except IngestAbort as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2

    if a.stage:
        report["staged"] = stage_all(report, Path(a.out).expanduser().resolve(),
                                     REPO / "data", a.act)
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    print(f"ingested {report['sections']} section(s) from {len(report['volumes'])} volume(s) "
          f"in {report['elapsed_seconds']}s  ({report['sections_per_minute']} sections/min)")
    for k in ("OK", "NEEDS_REVIEW", "INCOMPLETE"):
        if report["status_counts"].get(k):
            print(f"  {k:13s} {report['status_counts'][k]}")
    for r in report["results"]:
        if r["status"] == "OK":
            continue
        print(f"  {r['status']:13s} {r['section']:10s} pp.{r['pages']:9s} "
              f"tables={r['tables']} preserved={len(r['preserved_regions'])} "
              f"lost_tokens={r['unrepresented_count']}")
        for p in r["preserved_regions"]:
            print(f"        preserved {p['kind']} p.{p['page']}: {p['reason'][:90]}")
        if r["unrepresented"]:
            print(f"        UNREPRESENTED: {r['unrepresented'][:15]}")
        for s in r["off_band_lines"][:3]:
            print(f"        off-band line: {s}")
    return 1 if report["status_counts"].get("INCOMPLETE") else 0


if __name__ == "__main__":
    sys.exit(main())
