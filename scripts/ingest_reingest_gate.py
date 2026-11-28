#!/usr/bin/env python3
"""CDN-0193 Phase 2: fidelity gate for a WHOLE-SECTION RE-INGEST.

scripts/table_rebuild_gate.py certifies an IN-PLACE TABLE REBUILD: it models a
section as one largest table and compares the candidate's prose against the
corpus file byte for byte.  A whole-section re-ingest fails that model for
reasons that are not defects — the corpus prose is REPLACED by design, the
section may hold several tables, and the corruption being fixed (a rendered
formula emitted as a fake pipe table, data/itaa-1997/.../82-150.md) moves
tokens out of pipes and into a preserved fence.

So this gate drops the corpus file as a reference entirely and compares the
ingested output against the SOURCE PDF.

  R1  PDF TOKEN RECALL       every token on the section's PDF pages, inside
                             the section's y-band, is present in the output —
                             tables, prose and preserved regions alike — except
                             lines matching the explicitly enumerated furniture
                             DROP-LIST below.  ANY other missing token FAILs.
                             This is the anti-loss floor.  Never relaxed.
  R2  NO INVENTION           every token in the output exists in that band,
                             except the explicitly enumerated ALLOW-LIST
                             (frontmatter, fence marker attributes, anchors,
                             table separators, the generated 'Last updated'
                             footer).  This is the anti-fabrication gate; it is
                             the one that catches a fabricated 'Column 2'
                             header cell.
  R3  TABLE WELL-FORMEDNESS  every emitted pipe table: header separator
                             present, >= 2 data rows, constant cell count, no
                             G5 formula glyphs in a pipe row, no empty header
                             cell.
  R4  PRESERVED-REGION       every fenced region carries an 'ingest-<kind>
      ACCOUNTING             source=<pdf> page=<n>' marker, is non-empty, does
                             not overlap another region or a table, and every
                             token in it is on the page its marker declares.
                             Glyphs inside a preserved region are LEGAL — that
                             is what the region is for.  Glyphs in pipes are
                             not (R3).
  R5  COHERENCE              scan_corpus_error_classes.table_coherence_findings
                             (the C11 detectors) reports 0 findings.
  R6  MULTI-TABLE /          every table and every preserved region is located
      MULTI-REGION           on the PDF pages it claims: each of its lines must
                             be fully present on a single page of the band.
                             Reports each unit with its page span.  Several
                             tables and several regions in one section are
                             normal here, not a reason to reject.

Verdict
  ACCEPTED  iff every gate passes.
  needs_review: true  when the output contains at least one preserved region.
      RULE: a preserved region is text the ingester could not PROVE was a
      table, so it kept it verbatim.  R1/R2 prove the text is all there and
      all real — the bytes are safe to ship — but no machine has said what the
      structure MEANT, so a human should read it.  Losslessness and
      readability are different claims; only the first is gateable, and
      conflating them is what produced the 82-150 corruption in the first
      place.
  REJECTED  otherwise, naming the gate and the offending tokens.

Imported, never copied: norm_tokens/is_row/SEP_RE/GLYPH_RE and the G5 glyph
semantics from table_rebuild_gate, table_coherence_findings from
scan_corpus_error_classes, page geometry from extract_itaa_tables_pdf, and the
section index from ingest_itaa_compilation.

CLI
  /usr/bin/python3.12 scripts/ingest_reingest_gate.py --act itaa-1997 \\
      --section 82-150 --ingested OUT.md --pdf vol03.pdf [--json report.json]
  /usr/bin/python3.12 scripts/ingest_reingest_gate.py --act itaa-1997 \\
      --ingested-root DIR --pdf vol03.pdf [--sections a,b] --json batch.json
  /usr/bin/python3.12 scripts/ingest_reingest_gate.py --selfcheck

Run under /usr/bin/python3.12 (python3.11 has no fitz).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import extract_itaa_tables_pdf as EX          # noqa: E402  page geometry
import table_rebuild_gate as GATE             # noqa: E402  norm/token + G5
import ingest_itaa_compilation as INGEST      # noqa: E402  section index

norm_tokens = GATE.norm_tokens
is_row = GATE.is_row
SEP_RE = GATE.SEP_RE
GLYPH_RE = GATE.GLYPH_RE
SCANNER = GATE.SCANNER

# ── the furniture DROP-LIST (R1) ────────────────────────────────────────────
# A line is dropped from the PDF band ONLY if it matches one of these, or is a
# wrapped continuation of one (see _furniture below).  Everything else is body text
# that must appear in the output.  This is deliberately NOT the ingester's
# band-based furniture rule: the ingester drops by y-position, the gate drops
# by pattern, so a body line the ingester mistook for furniture is caught here.
# The zone restricts WHERE a pattern may drop a line, and it is NOT a
# y-coordinate: "header" applies only to the unbroken run of header-shaped
# lines at the TOP of a page, "footer" only to the unbroken run of
# footer-shaped lines at the BOTTOM, "any" anywhere.  No page-geometry
# constant appears in R1 at all — which matters, because the ingester's fixed
# body band (118..600pt) is wrong on the landscape pages that carry the wide
# tables, and a gate sharing that constant could not see it.
DROP_PATS: list[tuple[str, str, re.Pattern]] = [
    ("running_header_section",   "head", re.compile(r"^Section\s+\S+$")),
    ("running_header_structure", "head",
     re.compile(r"^(Chapter|Part|Division|Subdivision)\s+[\w-]+\b")),
    ("running_header_structure", "head",
     re.compile(r"^.*\b(Chapter|Part|Division|Subdivision)\s+[\w-]+$")),
    ("compilation_no",           "foot", re.compile(r"^Compilation No\.\s*\d+.*$")),
    ("authorised_version",       "foot", re.compile(r"^Authorised Version\b.*$")),
    # The page number always sits beside the act's own title.  Looser forms
    # ('^\d+$', '^\d+ ... \d{4}$') ate real text: a table cell whose last line
    # is '1936', and a row reading '20 September 1985'.
    ("page_number",              "foot", re.compile(r"^\d+\s+\S.*\bAct\s+\d{4}$")),
    ("page_number",              "foot", re.compile(r"^\S.*\bAct\s+\d{4}\s+\d+$")),
    ("footer_rule",              "any",    re.compile(r"^[_\-—–]{4,}$")),
    ("asterisked_definitions",   "any",    EX.NOTE_LINE_RE),
]
WRAP_GAP = 15.0   # pt: a header/footer line this close below a matched
                  # furniture line is that line's wrap ('Part 2-40 Rules
                  # affecting ... / payments'), not body text.

# ── the invention ALLOW-LIST (R2) ───────────────────────────────────────────
ALLOW_LIST = [
    "yaml frontmatter block (--- ... ---) — generated metadata",
    "generated '# <id>  <title>' heading marker '#'",
    "anchor tags <a id=\"...\"></a>",
    "markdown table separator rows (| --- | --- |)",
    "preserved-region fence lines incl. marker attributes "
    "(```ingest-formula source=vol03.pdf page=69)",
    "blockquote '>' and pipe '|' structure characters",
    "the generated footer '*Last updated: <date> (Compilation <n>)*' "
    "(or '… (Compilation <n> as amended by <Act>)*' for a hand-applied amending Act)",
]
FM_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
ANCHOR_RE = re.compile(r'<a id="[^"]*"></a>')
FENCE_RE = re.compile(r"^(`{3,})(.*)$")
MARKER_RE = re.compile(r"^ingest-(formula|unclassified)\s+source=(\S+)\s+page=(\d+)\s*$")
FOOTER_RE = re.compile(r"^\*Last updated: .*\(Compilation \d+(?: as amended by [^)]+)?\)\*$")


# ── PDF side ────────────────────────────────────────────────────────────────
def _line_text(ln: dict) -> str:
    return " ".join(w["t"] for w in ln["words"]).strip()


def _match(text: str, zone: str) -> str | None:
    for name, where, pat in DROP_PATS:
        if where in (zone, "any") and pat.match(text):
            return name
    return None


def _furniture(lines: list[dict]) -> dict[int, str]:
    """-> {index in lines: why it is furniture} for ONE page.

    Head: the leading run of header-shaped lines, plus a wrapped continuation
    that sits within WRAP_GAP of the line it wraps ('Part 2-40 Rules affecting
    ... / payments').  Foot: the trailing run of footer-shaped lines, with NO
    wrap rule — body leading is ~12.6pt and the footer rule sits ~8pt below the
    last body line, so a wrap rule at the bottom would eat real text.  A
    wrapped footer therefore FAILs R1 rather than being dropped on a guess.
    """
    out: dict[int, str] = {}
    for i, ln in enumerate(lines):
        why = _match(_line_text(ln), "head")
        if why is None and i and (i - 1) in out and \
                0 < ln["y"] - lines[i - 1]["y"] <= WRAP_GAP:
            why = "wrapped_furniture_line"
        if why is None:
            break
        out[i] = why
        if why == "running_header_section":
            # 'Section 115-34' is the LAST line of the running header on every
            # body page.  Stopping there matters: a section sub-heading at the
            # top of a continuation page ('Relationship with Subdivision
            # 109-A') matches the structure pattern too, and swallowing it
            # would be R1 dropping real text.
            break
    for i in range(len(lines) - 1, -1, -1):
        if i in out:
            break
        why = _match(_line_text(lines[i]), "foot")
        if why is None:
            break
        out[i] = why
    for i, ln in enumerate(lines):
        if i not in out and (why := _match(_line_text(ln), "any")):
            out[i] = why
    return out


def section_band(doc, meta: dict) -> dict:
    """The section's PDF band: every line on its pages, split kept/dropped.

    Kept and dropped are decided per LINE by DROP_PATS, never by y-position —
    y only bounds the band (the section heading down to the next section's
    heading).  The section's own heading lines are KEPT: the output carries
    them as '# <id>  <title>', so a truncated title is a token loss.
    """
    kept: list[dict] = []           # {page (1-based), text}
    dropped: list[dict] = []
    for pno in range(meta["page"], meta["end_page"] + 1):
        lo = meta["y"] - 0.5 if pno == meta["page"] else -1e9
        hi = meta["end_y"] - 0.5 if pno == meta["end_page"] else 1e9
        page_lines = [l for l in EX.page_lines(doc[pno]) if _line_text(l)]
        furn = _furniture(page_lines)
        for i, ln in enumerate(page_lines):
            if not (lo <= ln["y"] <= hi):
                continue
            rec = {"page": pno + 1, "text": _line_text(ln)}
            if i in furn:
                dropped.append({**rec, "why": furn[i]})
            else:
                kept.append(rec)
    tokens: Counter = Counter()
    per_page: dict[int, Counter] = {}
    for k in kept:
        t = norm_tokens(k["text"])
        tokens.update(t)
        per_page.setdefault(k["page"], Counter()).update(t)
    return {"kept": kept, "dropped": dropped, "tokens": tokens, "per_page": per_page,
            "pages": f"{meta['page'] + 1}-{meta['end_page'] + 1}"}


# ── output side ─────────────────────────────────────────────────────────────
def parse_output(text: str) -> dict:
    """Split the ingested markdown into prose / tables / preserved regions."""
    body = FM_RE.sub("", text)
    had_fm = body != text
    lines = body.splitlines()
    regions: list[dict] = []
    tables: list[dict] = []
    prose: list[str] = []
    problems: list[str] = []      # structural faults R4 reports

    i, n = 0, len(lines)
    while i < n:
        m = FENCE_RE.match(lines[i])
        if m:
            fence, info = m.group(1), m.group(2).strip()
            start = i
            i += 1
            body_lines = []
            while i < n and not lines[i].startswith(fence):
                body_lines.append(lines[i])
                i += 1
            if i >= n:
                problems.append(f"unterminated fence opened at output line {start + 1}")
                regions.append({"line": start + 1, "info": info, "body": body_lines,
                                "marker": None, "terminated": False})
                break
            i += 1
            mk = MARKER_RE.match(info)
            regions.append({
                "line": start + 1, "info": info, "body": body_lines, "terminated": True,
                "marker": ({"kind": mk.group(1), "source": mk.group(2),
                            "page": int(mk.group(3))} if mk else None)})
            continue
        if is_row(lines[i]) or SEP_RE.match(lines[i]):
            start = i
            block = []
            while i < n and (is_row(lines[i]) or SEP_RE.match(lines[i])):
                block.append(lines[i])
                i += 1
            tables.append({"line": start + 1, "lines": block})
            continue
        prose.append(lines[i])
        i += 1
    return {"frontmatter": had_fm, "prose": prose, "tables": tables,
            "regions": regions, "problems": problems, "lines": lines}


def _row_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def output_tokens(parsed: dict) -> Counter:
    """Every token the output ASSERTS, with allow-listed constructs removed.

    Per line, exactly like the PDF side: norm_tokens de-hyphenates across a
    newline, so a whole-text compare welds a '| --- |' separator onto the next
    row and reports phantom losses (learned the hard way in the ingester).
    """
    out: Counter = Counter()
    for ln in parsed["prose"]:
        s = ANCHOR_RE.sub(" ", ln)
        if FOOTER_RE.match(s.strip()) or s.strip() == "---":
            continue
        # '>' is blanked only in MARKER position (line-leading), never mid-line.
        # Blanking every '>' erased real inequalities from the comparison — e.g.
        # "Total incidental shipping income > 0.25% of total core shipping income"
        # read as a lost token — and, worse, made a genuinely DROPPED '>' undetectable.
        s = re.sub(r"^(?:\s*>)+\s?", " ", s)
        if s.startswith("#"):
            s = s.lstrip("#")
        out.update(norm_tokens(s))
    for t in parsed["tables"]:
        for ln in t["lines"]:
            if SEP_RE.match(ln):
                continue
            out.update(norm_tokens(" ".join(_row_cells(ln))))
    for r in parsed["regions"]:
        for ln in r["body"]:
            out.update(norm_tokens(ln))
    return out


# ── gates ───────────────────────────────────────────────────────────────────
def r1_pdf_token_recall(band: dict, got: Counter) -> dict:
    missing = band["tokens"] - got
    drops = Counter(d["why"] for d in band["dropped"])
    return {"pass": not missing,
            "reason": "" if not missing else
                      f"{sum(missing.values())} token(s) on the section's PDF pages are "
                      f"absent from the ingested output: {sorted(missing.elements())[:25]}",
            "pdf_tokens": sum(band["tokens"].values()),
            "output_tokens": sum(got.values()),
            "missing": sorted(missing.elements())[:50],
            "drop_list_applied": dict(drops)}


def r2_no_invention(band: dict, got: Counter) -> dict:
    extra = got - band["tokens"]
    return {"pass": not extra,
            "reason": "" if not extra else
                      f"{sum(extra.values())} token(s) in the ingested output do not exist "
                      f"in the section's PDF band: {sorted(extra.elements())[:25]}",
            "invented": sorted(extra.elements())[:50],
            "allow_list": ALLOW_LIST}


def r3_table_wellformedness(parsed: dict) -> dict:
    problems: list[str] = []
    for t in parsed["tables"]:
        ln0, block = t["line"], t["lines"]
        if len(block) < 2 or not SEP_RE.match(block[1]):
            problems.append(f"table at line {ln0}: no header separator row")
            continue
        header = _row_cells(block[0])
        if any(not c for c in header):
            problems.append(f"table at line {ln0}: empty header cell(s) in {header}")
        data = [l for l in block[2:] if is_row(l)]
        if len(data) < 2:
            problems.append(f"table at line {ln0}: {len(data)} data row(s), need >= 2")
        for l in block:
            if SEP_RE.match(l):
                continue
            cells = _row_cells(l)
            if len(cells) != len(header):
                problems.append(f"table at line {ln0}: {len(cells)} cells != "
                                f"{len(header)} header cells: {l.strip()[:80]}")
            m = GLYPH_RE.search(l)
            if m and not SCANNER._is_natural_word_accent(l, m):
                problems.append(f"table at line {ln0}: formula glyph {m.group(0)!r} in a "
                                f"pipe row (G5): {l.strip()[:80]}")
        t["rows"] = len(data)
        t["cols"] = len(header)
    return {"pass": not problems, "reason": "; ".join(problems[:6]),
            "tables": len(parsed["tables"])}


def r4_preserved_regions(parsed: dict, band: dict) -> dict:
    problems = list(parsed["problems"])
    for r in parsed["regions"]:
        at = f"region at line {r['line']}"
        if not r["marker"]:
            problems.append(f"{at}: fenced block without an 'ingest-<kind> source=... "
                            f"page=<n>' marker (info string {r['info']!r})")
            continue
        if not [b for b in r["body"] if b.strip()]:
            problems.append(f"{at}: preserved region is empty")
            continue
        page = r["marker"]["page"]
        have = band["per_page"].get(page)
        if have is None:
            problems.append(f"{at}: marker declares page {page}, which is not in the "
                            f"section's PDF band (pages {band['pages']})")
            continue
        want: Counter = Counter()
        for b in r["body"]:
            want.update(norm_tokens(b))
        off = want - have
        if off:
            problems.append(f"{at}: {sum(off.values())} token(s) are not on the declared "
                            f"page {page}: {sorted(off.elements())[:10]}")
        if any(is_row(b) for b in r["body"]):
            problems.append(f"{at}: contains pipe-table rows — a preserved region and a "
                            f"table must not overlap")
    return {"pass": not problems, "reason": "; ".join(problems[:6]),
            "regions": len(parsed["regions"])}


def r5_coherence(parsed: dict, relpath: str) -> dict:
    found = SCANNER.table_coherence_findings(parsed["lines"], relpath)
    return {"pass": not found,
            "reason": "; ".join(f"{f['class']}: {f['detail'][:110]}" for f in found[:6]),
            "findings": len(found)}


def _page_span(unit_lines: list[str], band: dict) -> tuple[list[int] | None, str]:
    """Pages of the band on which each line is FULLY present, or why not."""
    pages: list[int] = []
    for l in unit_lines:
        toks = Counter(norm_tokens(l))
        if not toks:
            continue
        hits = [p for p, have in sorted(band["per_page"].items()) if not toks - have]
        if not hits:
            return None, f"no single PDF page in the band holds {l.strip()[:70]!r}"
        pages.append(hits[0] if len(hits) == 1 else
                     min(hits, key=lambda p: abs(p - (pages[-1] if pages else hits[0]))))
    return sorted(set(pages)), ""


def r6_multi_unit(parsed: dict, band: dict) -> dict:
    units, problems = [], []
    for t in parsed["tables"]:
        rows = [" ".join(_row_cells(l)) for l in t["lines"] if not SEP_RE.match(l)]
        span, why = _page_span(rows, band)
        if span is None:
            problems.append(f"table at line {t['line']}: {why}")
        units.append({"kind": "table", "line": t["line"], "rows": t.get("rows"),
                      "cols": t.get("cols"), "pages": span})
    for r in parsed["regions"]:
        span, why = _page_span(r["body"], band)
        if span is None:
            problems.append(f"region at line {r['line']}: {why}")
        units.append({"kind": "region:" + (r["marker"]["kind"] if r["marker"] else "?"),
                      "line": r["line"], "pages": span,
                      "declared_page": r["marker"]["page"] if r["marker"] else None})
    return {"pass": not problems, "reason": "; ".join(problems[:6]), "units": units}


# ── driver ──────────────────────────────────────────────────────────────────
# ── page-verified operator credit ────────────────────────────────────────────
# An operator set in a Symbol subset whose ToUnicode maps it to a space leaves no token behind: the
# page shows "Assessable income = Deductions", the text layer yields a gap, so writing the '=' was
# called an invention by R2 while R1 could not demand it either.  The resolver reads such a glyph off
# the section's OWN pages - glyph name inside its own embedded subset, bounded to the fence's y-band
# on that page - and the proposal records, per line, exactly which characters it placed and which
# glyph justifies each.  That evidence is folded into the band's tokens here, so:
#
#   * R2 permits a character only where a page-derived glyph justifies it, ONE OCCURRENCE AT A TIME:
#     writing two multiplication signs where the page draws one is still rejected;
#   * R1 now DEMANDS the character, so the printed gap stops being quietly acceptable;
#   * a character the tokeniser erases is never credited - norm_tokens('*') is empty, so no output
#     could satisfy the demand and the credit could never be spent.
#
# Keyed by SECTION, never by (font family, glyph id): glyph ids are per-volume ordinals into a
# subsetted font, so id 0x2B is a different character in a different volume.  An earlier attempt
# keyed a table that way and was reverted.
OPERATOR_EVIDENCE: dict[tuple[str, int], Counter] = {}
EVIDENCE_REFUSED: list[dict] = []


def load_operator_evidence(path: Path) -> dict[tuple[str, int], Counter]:
    """Per-section, per-page operator credit from a resolver proposal.

    Keyed by (section, page) because the credit has to reach the PAGE's token set as well as the
    section's: a formula fence is a preserved region, and R4/R6 validate every region token against
    the page it declares - so a credited operator missing from per_page fails those gates even
    though the band as a whole knows about it.

    Credited as TOKENS (through norm_tokens), not raw characters, so the credit is in the same
    normal form as the comparison: norm_tokens('´') is [\"'\"], and crediting the acute accent as
    itself would never match the output.
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    ev: dict[tuple[str, int], Counter] = {}
    refused: list[dict] = []
    for fence in doc.get("fences", []):
        if fence.get("status") != "change-needed":
            continue
        section, page = fence.get("section"), fence.get("page")
        page_glyphs = {(g.get("glyph_name"), g.get("char"))
                       for g in fence.get("glyphs", []) if g.get("kind") == "operator"}
        for line in fence.get("lines", []):
            for ins in line.get("insertions", []):
                if ins.get("skipped") or ins.get("kind") != "operator":
                    continue
                ch, name = ins.get("char"), ins.get("glyph_name")
                if not ch:
                    continue
                if (name, ch) not in page_glyphs:
                    refused.append({"section": section, "page": page, "char": ch, "glyph": name,
                                    "why": "no page-verified glyph in this fence justifies it"})
                    continue
                toks = norm_tokens(ch)
                if not toks:
                    refused.append({"section": section, "page": page, "char": ch, "glyph": name,
                                    "why": "the tokeniser erases this character, so no output can "
                                           "satisfy a demand for it"})
                    continue
                bucket = ev.setdefault((section, page), Counter())
                for t in toks:
                    bucket[t] += 1
    OPERATOR_EVIDENCE.clear()
    OPERATOR_EVIDENCE.update(ev)
    EVIDENCE_REFUSED.clear()
    EVIDENCE_REFUSED.extend(refused)
    return ev


def credit_for(section: str) -> tuple[Counter, dict[int, Counter]]:
    """(section-wide credit, per-page credit) for one section."""
    total: Counter = Counter()
    per_page: dict[int, Counter] = {}
    for (sec, page), c in OPERATOR_EVIDENCE.items():
        if sec != section:
            continue
        total.update(c)
        per_page.setdefault(page, Counter()).update(c)
    return total, per_page


def band_with_credit(band: dict, section: str) -> dict:
    """The band, its page token sets, plus the operators a page-derived glyph justifies here."""
    total, per_page = credit_for(section)
    if not total:
        return band
    out = dict(band)
    out["tokens"] = Counter(band["tokens"]) + total
    new_pages = dict(band.get("per_page") or {})
    for page, c in per_page.items():
        if page in new_pages:
            new_pages[page] = Counter(new_pages[page]) + c
    out["per_page"] = new_pages
    out["operator_credit"] = dict(total)
    return out


def gate_ingested(act: str, section: str, ingested_text: str, band: dict,
                  relpath: str | None = None) -> dict:
    band = band_with_credit(band, section)
    parsed = parse_output(ingested_text)
    got = output_tokens(parsed)
    gates = {
        "R1_pdf_token_recall": r1_pdf_token_recall(band, got),
        "R2_no_invention": r2_no_invention(band, got),
        "R3_table_wellformedness": r3_table_wellformedness(parsed),
        "R4_preserved_region_accounting": r4_preserved_regions(parsed, band),
        "R5_coherence": r5_coherence(parsed, relpath or f"{act}/{section}.md"),
        "R6_multi_table_multi_region": r6_multi_unit(parsed, band),
    }
    ok = all(g["pass"] for g in gates.values())
    return {
        "act": act, "section": section, "pdf_pages": band["pages"],
        "verdict": "ACCEPTED" if ok else "REJECTED",
        # a preserved region is proven-complete text of unproven structure
        "needs_review": bool(parsed["regions"]) if ok else None,
        "frontmatter": parsed["frontmatter"],
        "operator_credit": band.get("operator_credit", {}),
        "gates": gates,
    }


_INDEX_CACHE: dict[str, dict] = {}


def open_band(pdf: Path, section: str):
    """(doc, band) for one section of one volume; the index is cached."""
    import fitz
    key = str(pdf.resolve())
    doc = _INDEX_CACHE.get(key + ":doc") or fitz.open(pdf)
    _INDEX_CACHE[key + ":doc"] = doc
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = INGEST.index_volume(doc)
    meta = _INDEX_CACHE[key].get(section)
    if meta is None:
        return doc, None
    return doc, section_band(doc, meta)


def _print(rep: dict) -> None:
    flag = "  needs_review" if rep.get("needs_review") else ""
    print(f"{rep['verdict']}{flag}  {rep['act']} {rep['section']}  pp.{rep['pdf_pages']}")
    for name, g in rep["gates"].items():
        print(f"  {'PASS  ' if g['pass'] else 'REJECT'} {name}"
              + (f"\n         {g['reason'][:400]}" if g["reason"] else ""))
    units = rep["gates"]["R6_multi_table_multi_region"]["units"]
    for u in units:
        print(f"         {u['kind']:20s} output line {u['line']:<5d} pdf pages {u['pages']}")


def selfcheck() -> int:
    """Synthetic band + output: prove each gate fires on its own defect."""
    band = {
        "pages": "1-1",
        "dropped": [],
        "kept": [],
        "tokens": Counter(norm_tokens(
            "6-1 A heading here prose line one Item Provision Regulator "
            "1 Part 2A APRA 2 Section 29JCA ASIC alpha ´ beta")),
        "per_page": {1: Counter(norm_tokens(
            "6-1 A heading here prose line one Item Provision Regulator "
            "1 Part 2A APRA 2 Section 29JCA ASIC alpha ´ beta"))},
    }
    good = ("---\nsection: \"6-1\"\n---\n# 6-1  A heading here\n\nprose line one\n\n"
            "| Item | Provision | Regulator |\n| --- | --- | --- |\n"
            "| 1 | Part 2A | APRA |\n| 2 | Section 29JCA | ASIC |\n\n"
            "```ingest-formula source=vol03.pdf page=1\nalpha ´ beta\n```\n\n"
            "---\n*Last updated: 2026-07-01 (Compilation 266)*\n")
    cases = {
        "baseline":        (good, "ACCEPTED", None),
        "dropped token":   (good.replace("prose line one", "prose line"), "REJECTED", "R1_pdf_token_recall"),
        "invented token":  (good.replace("prose line one", "prose line one gamma"), "REJECTED", "R2_no_invention"),
        "no separator":    (good.replace("| --- | --- | --- |\n", ""), "REJECTED", "R3_table_wellformedness"),
        "glyph in pipes":  (good.replace("```ingest-formula source=vol03.pdf page=1\nalpha ´ beta\n```",
                                         "| alpha | ´ |\n| --- | --- |\n| beta | x |"),
                            "REJECTED", "R3_table_wellformedness"),
        "marker stripped": (good.replace("```ingest-formula source=vol03.pdf page=1", "```"),
                            "REJECTED", "R4_preserved_region_accounting"),
        "wrong page":      (good.replace("page=1", "page=9"), "REJECTED", "R4_preserved_region_accounting"),
        "empty header":    (good.replace("| Item | Provision | Regulator |", "|  | Provision | Regulator |"),
                            "REJECTED", "R3_table_wellformedness"),
    }
    bad = 0
    for name, (text, want, gate) in cases.items():
        rep = gate_ingested("selfcheck", "6-1", text, band)
        ok = rep["verdict"] == want and (gate is None or not rep["gates"][gate]["pass"])
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:18s} -> {rep['verdict']}"
              + ("" if ok else "  " + "; ".join(
                  f"{k}={g['pass']}:{g['reason'][:80]}" for k, g in rep["gates"].items())))
    nr = gate_ingested("selfcheck", "6-1", good, band)["needs_review"]
    bad += not nr
    print(f"  {'ok  ' if nr else 'FAIL'} {'needs_review flag':18s} -> {nr}")
    clean = good.replace("```ingest-formula source=vol03.pdf page=1\nalpha ´ beta\n```\n\n", "")
    band2 = dict(band)
    band2["tokens"] = Counter(norm_tokens(
        "6-1 A heading here prose line one Item Provision Regulator "
        "1 Part 2A APRA 2 Section 29JCA ASIC"))
    band2["per_page"] = {1: band2["tokens"]}
    rep = gate_ingested("selfcheck", "6-1", clean, band2)
    ok = rep["verdict"] == "ACCEPTED" and rep["needs_review"] is False
    bad += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {'clean no-region':18s} -> {rep['verdict']} "
          f"needs_review={rep['needs_review']}")
    # ── page-verified operator credit ────────────────────────────────────────
    # The band has no '×'.  Credit exactly one, from a glyph read off the page, and prove the credit
    # is bounded in BOTH directions: it can be spent once, it must be spent, and it cannot be
    # overdrawn.  A credit that only ever permitted things would be a check that cannot fail.
    print("  -- operator credit --")
    saved = dict(OPERATOR_EVIDENCE)
    OPERATOR_EVIDENCE.clear()
    OPERATOR_EVIDENCE[("6-1", 1)] = Counter({"×": 1})         # one drawn multiplication glyph, p1
    band_nox = dict(band)
    band_nox["tokens"] = Counter({t: n for t, n in band["tokens"].items() if t != "×"})
    band_nox["per_page"] = {1: band_nox["tokens"]}
    # '×' is ADDED to the fence body; the band's own '´' demand must stay satisfied, because
    # norm_tokens('´') is ["'"] - replacing it instead of adding would fail R1 for the wrong reason.
    for name, text, want, gate in (
        ("credit spent once",     good.replace("alpha ´ beta", "alpha ´ × beta"),   "ACCEPTED", None),
        ("credit unspent (R1)",   good,                                            "REJECTED", "R1_pdf_token_recall"),
        ("credit overdrawn (R2)", good.replace("alpha ´ beta", "alpha ´ × × beta"), "REJECTED", "R2_no_invention"),
    ):
        rep = gate_ingested("selfcheck", "6-1", text, band_nox)
        ok = rep["verdict"] == want and (gate is None or not rep["gates"][gate]["pass"])
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:20s} -> {rep['verdict']}"
              + ("" if ok else "  " + "; ".join(
                  f"{k}={g['pass']}:{g['reason'][:60]}" for k, g in rep["gates"].items())))
    OPERATOR_EVIDENCE.clear()
    OPERATOR_EVIDENCE.update(saved)

    # the loader's own refusals: an unjustified glyph, and a character no output could satisfy
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "proposal.json"
        p.write_text(json.dumps({"fences": [{
            "section": "9-9", "page": 1, "status": "change-needed",
            "glyphs": [{"glyph_name": "uniF0B4", "char": "×", "kind": "operator"},
                       {"glyph_name": "uniF02A", "char": "*", "kind": "operator"}],
            "lines": [{"insertions": [
                {"char": "×", "glyph_name": "uniF0B4", "kind": "operator", "skipped": False},
                {"char": "×", "glyph_name": "uniF999", "kind": "operator", "skipped": False},
                {"char": "*", "glyph_name": "uniF02A", "kind": "operator", "skipped": False},
                {"char": "×", "glyph_name": "uniF0B4", "kind": "operator", "skipped": True},
            ]}]}]}), encoding="utf-8")
        ev = load_operator_evidence(p)
        got = ev.get(("9-9", 1), Counter())
        ok = (got == Counter({"×": 1}) and len(EVIDENCE_REFUSED) == 2
              and any(r["char"] == "*" and "eras" in r["why"] for r in EVIDENCE_REFUSED)
              and any(r["char"] == "×" and "justif" in r["why"] for r in EVIDENCE_REFUSED))
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {'loader refusals':20s} -> credited={dict(got)} "
              f"refused={[(r['char'], r['why'][:22]) for r in EVIDENCE_REFUSED]}")
    OPERATOR_EVIDENCE.clear()
    OPERATOR_EVIDENCE.update(saved)

    print("selfcheck:", "PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


def main() -> int:
    if "--selfcheck" in sys.argv:
        return selfcheck()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--act", required=True)
    ap.add_argument("--section", help="one section id")
    ap.add_argument("--ingested", type=Path, help="the ingested .md for --section")
    ap.add_argument("--ingested-root", type=Path,
                    help="batch: a tree of ingested .md files (named <section>.md)")
    ap.add_argument("--sections", help="batch: comma-separated subset")
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--operator-evidence", type=Path,
                    help="a resolver proposal.json whose page-verified operators are credited, "
                         "per section and per page, so R1 demands them and R2 permits exactly the "
                         "occurrences the page shows")
    a = ap.parse_args()

    if a.operator_evidence:
        ev = load_operator_evidence(a.operator_evidence)
        total = sum(sum(c.values()) for c in ev.values())
        print(f"operator credit: {total} operator token(s) across "
              f"{len({s for s, _p in ev})} section(s), from {a.operator_evidence.name}")
        for r in EVIDENCE_REFUSED:
            print(f"  refused {r['char']!r} in {r['section']} p{r['page']}: {r['why']}")

    jobs: list[tuple[str, Path]] = []
    if a.ingested_root:
        want = set(a.sections.split(",")) if a.sections else None
        for p in sorted(a.ingested_root.rglob("*.md")):
            if want is None or p.stem in want:
                jobs.append((p.stem, p))
    elif a.section and a.ingested:
        jobs = [(a.section, a.ingested)]
    else:
        ap.error("need --section with --ingested, or --ingested-root")

    reports = []
    for section, path in jobs:
        _doc, band = open_band(a.pdf, section)
        if band is None:
            rep = {"act": a.act, "section": section, "verdict": "REJECTED",
                   "needs_review": None, "pdf_pages": None,
                   "gates": {"R0_section_located": {
                       "pass": False,
                       "reason": f"section {section} not found in {a.pdf.name}"}}}
        else:
            rep = gate_ingested(a.act, section, path.read_text(encoding="utf-8"), band,
                                relpath=str(path))
        rep["ingested"] = str(path)
        reports.append(rep)
        if len(jobs) == 1:
            _print(rep)

    if len(jobs) > 1:
        by = Counter(("ACCEPTED-needs_review" if r["verdict"] == "ACCEPTED" and r["needs_review"]
                      else r["verdict"]) for r in reports)
        print(f"{len(reports)} section(s): " + "  ".join(f"{k}={v}" for k, v in sorted(by.items())))
        hist: Counter = Counter()
        for r in reports:
            for name, g in r["gates"].items():
                if not g["pass"]:
                    hist[name] += 1
        for name, n in hist.most_common():
            print(f"  {n:4d}  {name}")
        for r in reports:
            if r["verdict"] != "REJECTED":
                continue
            fails = [f"{k}: {g['reason'][:160]}" for k, g in r["gates"].items() if not g["pass"]]
            print(f"  REJECTED {r['section']:10s} " + " | ".join(fails)[:400])

    if a.json:
        a.json.write_text(json.dumps(reports if len(reports) > 1 else reports[0],
                                     indent=2, ensure_ascii=False) + "\n")
    return 0 if all(r["verdict"] == "ACCEPTED" for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
