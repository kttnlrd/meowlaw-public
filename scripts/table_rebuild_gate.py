#!/usr/bin/env python3
"""CDN-0193 Phase 0: fidelity gate for a rebuilt table section file.

Six gates.  A candidate ships only if every one PASSes.  The gates are
deliberately unforgiving: an earlier automated pass at this corruption class
was lossy and had to be reverted, so "cannot silently lose text" is the
requirement, not "looks structurally fine".

  G1 token provenance  bidirectional multiset equality, PDF table region vs
                       candidate table rows.  100%.  No threshold.
  G2 prose preservation  every non-'|' line byte-identical to the original
                       (the only permitted addition is a table note line whose
                       tokens G1 already accounted for).
  G3 structural        row count not reduced, header matches the PDF, row
                       identifiers well-formed / ordered / unique.
  G4 notes             '*To find definitions of asterisked terms…' sits below
                       the table, never inside a cell.
  G5 glyph ban         formula-font junk in a rebuilt table = human job.
  G6 coherence         scan_table_coherence (the C11 detectors) scores 0 on
                       the candidate.

Library: gate_file(...) -> dict.  CLI: see --help.

Run under /usr/bin/python3.12 (python3.11 has no fitz).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import extract_itaa_tables_pdf as EX  # noqa: E402


def _load_scanner():
    """Import scan_corpus_error_classes for its C11 detectors (G6).

    Imported, never copied — the gate must track the detector, not a fork of
    it.  The module scans a corpus root held in a module global, so G6 points
    that global at a one-file throwaway tree.
    """
    spec = importlib.util.spec_from_file_location(
        "scan_corpus_error_classes", SCRIPTS / "scan_corpus_error_classes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCANNER = _load_scanner()
GLYPH_RE = SCANNER.EXTRACT_GLYPH
NOTE_RE = EX.NOTE_LINE_RE
SMART = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                       "ʼ": "'", "´": "'"})
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
SEP_RE = re.compile(r"^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$")


# ── normalisation ───────────────────────────────────────────────────────────
def norm_tokens(text: str) -> list[str]:
    """Whitespace tokens after the equivalences G1 is allowed to assume."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("­", "")            # soft hyphen
    text = re.sub(r"-[ \t]*\n[ \t]*", "", text)  # line-end hyphenation
    text = LINK_RE.sub(r"\1", text)              # [label](url) -> label
    text = text.translate(SMART)                 # smart quotes -> straight
    text = re.sub(r"\*\*|__", "", text)          # markdown bold, keep the text
    out = []
    for t in text.split():
        t = t.strip("*_")                        # emphasis markers, keep the text
        if t:
            out.append(t)
    return out


def is_row(line: str) -> bool:
    return line.startswith("|") and not SEP_RE.match(line)


# ── gates ───────────────────────────────────────────────────────────────────
def g1_token_provenance(pdf_tokens: list[str], cand_lines: list[str]) -> dict:
    md = []
    for ln in cand_lines:
        if is_row(ln):
            md += norm_tokens(ln.strip().strip("|").replace("|", " "))
        elif NOTE_RE.match(ln.strip()):
            md += norm_tokens(ln)
    a, b = Counter(norm_tokens(" ".join(pdf_tokens))), Counter(md)
    missing, extra = a - b, b - a
    ok = not missing and not extra
    reason = "" if ok else (
        f"{sum(missing.values())} token(s) in the PDF table region are absent from the "
        f"candidate: {sorted(missing.elements())[:25]}; "
        f"{sum(extra.values())} token(s) in the candidate are absent from the PDF: "
        f"{sorted(extra.elements())[:25]}")
    return {"pass": ok, "reason": reason,
            "pdf_tokens": sum(a.values()), "md_tokens": sum(b.values())}


def g2_prose_preservation(orig_lines: list[str], cand_lines: list[str]) -> dict:
    o = [l for l in orig_lines if not l.startswith("|")]
    c = [l for l in cand_lines if not l.startswith("|")]
    if o == c:
        return {"pass": True, "reason": ""}
    # the single permitted difference: added table-note lines (R6/G4)
    added = [l for l in c if l not in o]
    slim = [l for l in c if not (NOTE_RE.match(l.strip()) and l not in o)]
    if slim == o and all(NOTE_RE.match(l.strip()) for l in added):
        return {"pass": True, "reason": "",
                "added_note_lines": added}
    diff = next(((i, x, y) for i, (x, y) in enumerate(zip(o, c)) if x != y),
                (min(len(o), len(c)), "<eof>", "<eof>"))
    return {"pass": False, "reason":
            f"non-table lines differ: {len(o)} original vs {len(c)} candidate; "
            f"first difference at non-table line {diff[0]}: {diff[1]!r} -> {diff[2]!r}"}


# Rows the corrupt files captured inside the table that are PDF *page
# furniture*, not table content: the rule above the asterisk footnote, and the
# footnote itself (split mid-word by the column boundaries, so it cannot be
# reassembled from the cells).  A rebuild drops them deliberately.  Notes that
# the extractor found INSIDE the table band are a different thing — those are
# re-emitted below the table and G1/G4 account for them.
FURNITURE_ROW_RE = re.compile(r"^[\s_\-—–]*$")


def _is_furniture_row(line: str) -> bool:
    text = line.strip().strip("|").replace("|", " ").strip()
    if FURNITURE_ROW_RE.match(text):
        return True
    # the footnote is split mid-word by the cell boundaries
    # ('*To find definitions of aste | risked terms,'), so match on the text
    # with all whitespace removed rather than on the words.
    return re.sub(r"\s+", "", text).lower().startswith("*tofinddefinitionsof")


# Australian drafting inserts items between 4 and 4A as 4AA, so the real order
# is 4, 4AAA, 4AA, 4AB, 4A, 4B — a suffix that is a PREFIX of another sorts
# after it.  Plain string order gets this backwards and flagged legitimate
# tables (gst-1999 132-5, 19-5) as scrambled.
_LAST = chr(0x10FFFF)


def _ident_sort_key(m: re.Match) -> tuple:
    num, suffix = int(m.group(1)), m.group(2)
    return (num, 0, ()) if not suffix else (num, 1, tuple(suffix) + (_LAST,))


def _row_tokens(rows: list[str]) -> set[str]:
    out = set()
    for l in rows:
        out |= {t.lower() for t in norm_tokens(l.strip().strip("|").replace("|", " "))}
    return out


def g3_structural(orig_lines, cand_lines, tables) -> dict:
    tables = [tables] if isinstance(tables, dict) else list(tables)
    problems = []
    # DATA rows only: in markdown the line before a '---' separator is a header
    # row, and the corrupt files repeat that header once per source page.
    # Collapsing 12 repeats into 1 must not be read as losing 11 rows.
    def data_rows(lines, drop_furniture=False):
        return [l for i, l in enumerate(lines) if is_row(l)
                and not (i + 1 < len(lines) and SEP_RE.match(lines[i + 1]))
                and not (drop_furniture and _is_furniture_row(l))]
    o_rows = len(data_rows(orig_lines, drop_furniture=True))
    c_rows = len(data_rows(cand_lines))

    # CONTENT CONTAINMENT, not a row count.  The old rule compared markdown
    # row counts corpus-vs-candidate and was wrong in both directions: the
    # corrupt files put a '| --- |' separator after data rows (so those rows
    # were miscounted as headers and the count ran LOW), and they capture page
    # furniture as rows (so it also ran HIGH).  It passed fbt-1986 65J, whose
    # candidate silently deleted three formula blocks from later subsections,
    # and it could never see the candidate being built from the WRONG table.
    # Both are token-level facts, so check tokens.
    # ALL '|' rows, not just data_rows: a corrupt file puts a '| --- |'
    # separator after real data rows, so data_rows misreads them as headers.
    # For a token SET the repeated headers are harmless — their tokens are in
    # the candidate's own header row.
    def all_rows(lines, drop_furniture=False):
        return [l for l in lines if is_row(l)
                and not (drop_furniture and _is_furniture_row(l))]
    o_tok = _row_tokens(all_rows(orig_lines, drop_furniture=True))
    c_tok = _row_tokens(all_rows(cand_lines))
    if not o_tok:
        problems.append("original table region has no content tokens — "
                        "nothing to prove the rebuilt table is the right table")
    else:
        gone = o_tok - c_tok
        if gone:
            problems.append(
                f"{len(gone)} of {len(o_tok)} distinct token(s) in the original "
                f"table region are absent from the candidate's table "
                f"(a different table, or '|' content outside it was deleted): "
                f"{sorted(gone)[:20]}")

    # every identifier-shaped row id in the original must still be a row id
    o_ids = {c for l in all_rows(orig_lines, drop_furniture=True)
             if EX.IDENT_RE.match(c := l.strip().strip("|").split("|")[0].strip())}
    c_ids = {l.strip().strip("|").split("|")[0].strip()
             for l in all_rows(cand_lines)}
    if o_ids - c_ids:
        problems.append(f"row identifiers dropped: {sorted(o_ids - c_ids)}")

    pdf_header = [tables[0].get("id_label", "Item")] + list(tables[0]["header"])
    cand_header = next((l for l in cand_lines if is_row(l)), "")
    got = [c.strip() for c in cand_header.strip().strip("|").split("|")]
    if got != [h.strip() for h in pdf_header]:
        problems.append(f"header cells {got} != PDF header cells {pdf_header}")

    # the header repeats on every page of a multi-page table; a merge that
    # swallowed row text into a header shows up as a header that disagrees
    # with its own repeats.
    for t in tables:
        heads = t.get("block_headers") or []
        if len({tuple(h) for h in heads}) > 1:
            problems.append(f"PDF header differs between pages of the same table: {heads}")

    ids = [r["item"] for t in tables for r in t["rows"]]
    if len(ids) != len(set(ids)):
        dup = [i for i, n in Counter(ids).items() if n > 1]
        problems.append(f"duplicate row identifiers: {dup}")
    bad = [i for i in ids if not EX.IDENT_RE.match(i)]
    if bad:
        problems.append(f"malformed row identifiers: {bad}")
    nums = [_ident_sort_key(m) for i in ids
            if (m := re.match(r"^(\d+)([A-Za-z]*)$", i))]
    if nums != sorted(nums):
        problems.append(f"row identifiers out of order: {ids}")

    # A candidate may hold MORE THAN ONE table, so "row 0 is the header" no longer
    # holds: take every row that is not itself a header (a header is the row
    # immediately above a separator).  One-table candidates are unaffected.
    md_ids = [l.strip().strip("|").split("|")[0].strip()
              for i, l in enumerate(cand_lines) if is_row(l)
              and not (i + 1 < len(cand_lines) and SEP_RE.match(cand_lines[i + 1]))]
    if set(md_ids) != set(ids):
        problems.append(
            f"candidate identifier set != PDF identifier set; "
            f"only in candidate {sorted(set(md_ids) - set(ids))}, "
            f"only in PDF {sorted(set(ids) - set(md_ids))}")
    return {"pass": not problems, "reason": "; ".join(problems),
            "orig_rows": o_rows, "cand_rows": c_rows}


def g4_notes(cand_lines: list[str], tables) -> dict:
    tables = [tables] if isinstance(tables, dict) else list(tables)
    notes = [n for t in tables for n in (t.get("notes") or [])]
    problems = []
    for ln in cand_lines:
        if is_row(ln) and NOTE_RE.search(ln):
            problems.append(f"note line swallowed into a table cell: {ln.strip()[:90]}")
    last_row = max((i for i, l in enumerate(cand_lines) if is_row(l)), default=-1)
    for n in notes:
        at = [i for i, l in enumerate(cand_lines) if norm_tokens(l) == norm_tokens(n)]
        if not at:
            problems.append(f"PDF table note missing from candidate: {n[:80]!r}")
        elif all(i < last_row for i in at):
            problems.append(f"PDF table note is above the table, not below: {n[:80]!r}")
    return {"pass": not problems, "reason": "; ".join(problems), "notes": notes}


def g5_glyph_ban(cand_lines: list[str]) -> dict:
    hits = []
    for i, ln in enumerate(cand_lines, 1):
        if not is_row(ln):
            continue
        m = GLYPH_RE.search(ln)
        if m and not SCANNER._is_natural_word_accent(ln, m):
            hits.append(f"line {i}: {m.group(0)!r} in {ln.strip()[:80]}")
    return {"pass": not hits,
            "reason": ("formula-font glyphs in the rebuilt table — needs human "
                       "reconstruction: " + "; ".join(hits[:5])) if hits else "",
            "hits": len(hits)}


def g6_coherence(act: str, cand_text: str) -> dict:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / act / "sections" / "candidate.md"
        f.parent.mkdir(parents=True)
        f.write_text(cand_text, encoding="utf-8")
        old_data, old_findings = SCANNER.DATA, list(SCANNER.findings)
        SCANNER.DATA = Path(td)
        SCANNER.findings.clear()
        try:
            SCANNER.scan_table_coherence()
            found = [f"{x['class']}: {x['detail'][:110]}" for x in SCANNER.findings]
        finally:
            SCANNER.DATA = old_data
            SCANNER.findings[:] = old_findings
    return {"pass": not found, "reason": "; ".join(found[:6]), "findings": len(found)}


def g0_boundary(table: dict) -> dict:
    """Guard against the one loss G1 cannot see.

    G1 proves nothing was dropped from INSIDE the detected table region, but
    the region's own end is decided by the extractor.  If the line just below
    the table is not recognisably prose, the table may have been cut short —
    refuse rather than guess.
    """
    tail = (table.get("tail_text") or "").strip()
    prose = (not tail
             or re.match(r"^(Note|Notes?:|Example|Subsection|Division|Part|Section)\b", tail)
             # subsection/paragraph markers: (3), (3A), (a), (ca), (iv)
             or re.match(r"^\(\d+[A-Za-z]*\)|^\([A-Za-z]{1,4}\)", tail)
             # the rule above the asterisk footnote ends every GST page
             or re.match(r"^[_\-—–]{6,}$", tail)
             # the next section's heading ends the table region (gst-1999 s3-5
             # ends on '3-10 Identifying the defined term in a definition')
             or re.match(r"^\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?\s+[A-Z]", tail)
             # the next section's heading ends the table region (gst-1999 s3-5
             # ends on '3-10 Identifying the defined term in a definition')
             or re.match(r"^\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?\s+[A-Z]", tail)
             or "Compilation No" in tail or "Authorised Version" in tail
             or re.match(r"^\d*\s*[A-Z][\w’'—-]+( [\w’'(),.—-]+)* \d+$", tail)
             or NOTE_RE.match(tail))
    return {"pass": bool(prose), "reason":
            "" if prose else f"line below the table is not recognisably prose, the "
                             f"table may be cut short: {tail[:100]!r}"}


# ── driver ──────────────────────────────────────────────────────────────────
def gate_file(act: str, original: Path, candidate: Path, tables) -> dict:
    """Gate a candidate against EVERY table the extractor found for the section.

    Accepting a list is the point: the old single-table signature is what let a
    section with two real tables be rebuilt as one, losing law.  G1 is the
    backstop - it compares the union of all detected table tokens against the
    candidate's table tokens, so a candidate that renders only some of the
    source tables fails on the missing tokens, whatever the caller believed.
    """
    tables = [tables] if isinstance(tables, dict) else list(tables)
    orig_lines = original.read_text(encoding="utf-8").splitlines()
    cand_text = candidate.read_text(encoding="utf-8")
    cand_lines = cand_text.splitlines()
    pdf_tokens = [w for t in tables for w in t["region_words"]]

    g0s = [g0_boundary(t) for t in tables]
    gates = {
        "G0_boundary": {"pass": all(g["pass"] for g in g0s),
                        "reason": " | ".join(g["reason"] for g in g0s if not g["pass"])},
        "G1_token_provenance": g1_token_provenance(pdf_tokens, cand_lines),
        "G2_prose_preservation": g2_prose_preservation(orig_lines, cand_lines),
        "G3_structural": g3_structural(orig_lines, cand_lines, tables),
        "G4_notes": g4_notes(cand_lines, tables),
        "G5_glyph_ban": g5_glyph_ban(cand_lines),
        "G6_coherence": g6_coherence(act, cand_text),
    }
    return {"act": act, "original": str(original), "candidate": str(candidate),
            "tables": len(tables),
            "verdict": "ACCEPTED" if all(g["pass"] for g in gates.values()) else "REJECTED",
            "gates": gates}


def dedup_tables(tables: list[dict]) -> list[dict]:
    """Collapse identical page-repeat copies; KEEP genuinely distinct tables.

    The corrupt sources re-detect the same table once per page (the header
    repeats), so collapsing repeats is right.  Collapsing everything is what
    silently ate sections holding two REAL tables (fbt-1986 s103).  Identical
    row content = a repeat; different rows = different law.
    """
    seen, out = set(), []
    for t in tables:
        key = tuple((r.get("item", ""),
                     tuple(r.get("cols", {}).get(str(i), "") for i in range(1, t["cols"])))
                    for r in t["rows"])
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def extract_tables(pdf: Path, section: str) -> list[dict]:
    """EVERY distinct table for the section — the real unit of a rebuild."""
    import fitz
    doc = fitz.open(pdf)
    return dedup_tables(EX.collect_tables(doc, section, detail=True))


def extract_table(pdf: Path, section: str) -> dict | None:
    """The section's largest table.  Kept for single-table callers/tests;
    gate_file itself takes the full list."""
    tables = extract_tables(pdf, section)
    return max(tables, key=lambda t: len(t["rows"])) if tables else None


def selfcheck() -> int:
    """Prove each gate fires on the loss it exists to catch.

    The failure mode being defended against is a rebuild that looks fine and
    is quietly missing text, so the only check worth having is one that
    corrupts a good candidate and insists on a REJECT.
    """
    table = {
        "page": 1, "cols": 3, "id_label": "Item",
        "header": ["Provision", "Regulator"],
        "block_headers": [["Item", "Provision", "Regulator"]],
        "rows": [{"item": "1", "cols": {"1": "Part 2A", "2": "APRA"}},
                 {"item": "2", "cols": {"1": "Section 29JCA", "2": "ASIC"}}],
        "notes": ["*To find definitions of asterisked terms, see the Dictionary."],
        "tail_text": "Note: something else entirely",
        "region_words": ("Item Provision Regulator 1 Part 2A APRA 2 Section 29JCA ASIC "
                         "*To find definitions of asterisked terms, see the Dictionary.").split(),
    }
    good = ("---\nsection: \"6\"\n---\n\n# 6  T\n\nprose line\n\n"
            "| Item | Provision | Regulator |\n| --- | --- | --- |\n"
            "| 1 | Part 2A | APRA |\n| 2 | Section 29JCA | ASIC |\n\n"
            "*To find definitions of asterisked terms, see the Dictionary.\n")
    cases = {
        "baseline": (good, "ACCEPTED", None),
        "dropped row":        (good.replace("| 2 | Section 29JCA | ASIC |\n", ""), "REJECTED", "G1_token_provenance"),
        "dropped cell words": (good.replace("| 1 | Part 2A | APRA |", "| 1 | Part | APRA |"), "REJECTED", "G1_token_provenance"),
        "edited prose":       (good.replace("prose line", "prose lyne"), "REJECTED", "G2_prose_preservation"),
        "note in a cell":     (good.replace("| 2 | Section 29JCA | ASIC |\n\n*To find definitions of asterisked terms, see the Dictionary.",
                                            "| 2 | Section 29JCA | ASIC *To find definitions of asterisked terms, see the Dictionary. |"),
                               "REJECTED", "G4_notes"),
        "formula glyph":      (good.replace("APRA |", "ç APRA ´ |"), "REJECTED", "G5_glyph_ban"),
        "truncated cell":     (good.replace("| 2 | Section 29JCA | ASIC |", "| 2 | Section 29JCA | ASIC and |"), "REJECTED", "G6_coherence"),
        "midword split":      (good.replace("| 1 | Part 2A | APRA |", "| 1 | Part 2A ast | erisked APRA |"), "REJECTED", "G6_coherence"),
    }
    # the two losses the old row-count rule could not see (CDN-0193 Phase 1):
    # the original carries a second '|' block outside the rebuilt table, and
    # the candidate keeps only the table.
    orig_extra = good.replace("| 2 | Section 29JCA | ASIC |\n",
                              "| 2 | Section 29JCA | ASIC |\n| Formula | x + y |\n")
    cases["deleted block outside the table"] = (good, "REJECTED", "G3_structural")
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        orig = Path(td) / "orig.md"
        orig.write_text(good)
        for name, (text, want, gate) in cases.items():
            cand = Path(td) / "cand.md"
            cand.write_text(text)
            src = orig
            if name == "deleted block outside the table":
                src = Path(td) / "orig_extra.md"
                src.write_text(orig_extra)
            rep = gate_file("selfcheck", src, cand, table)
            ok = rep["verdict"] == want and (gate is None or not rep["gates"][gate]["pass"])
            bad += not ok
            print(f"  {'ok  ' if ok else 'FAIL'} {name:20s} -> {rep['verdict']}"
                  + ("" if ok else f"  (wanted {want} via {gate}: "
                                   + "; ".join(f"{k}={g['pass']}" for k, g in rep["gates"].items()) + ")"))
    print("selfcheck:", "PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


def main() -> int:
    if "--selfcheck" in sys.argv:
        return selfcheck()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--act", required=True)
    ap.add_argument("--section", required=True)
    ap.add_argument("--original", required=True, type=Path)
    ap.add_argument("--candidate", required=True, type=Path)
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()

    tables = extract_tables(a.pdf, a.section)
    if not tables:
        print(f"REJECTED {a.act} {a.section}: no table found in {a.pdf}", file=sys.stderr)
        return 1
    rep = gate_file(a.act, a.original, a.candidate, tables)
    print(f"{rep['verdict']}  {a.act} {a.section}")
    for name, g in rep["gates"].items():
        print(f"  {'PASS  ' if g['pass'] else 'REJECT'} {name}"
              + (f"\n         {g['reason']}" if g["reason"] else ""))
    if a.json:
        a.json.write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep["verdict"] == "ACCEPTED" else 1


if __name__ == "__main__":
    sys.exit(main())
