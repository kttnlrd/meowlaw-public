#!/usr/bin/env python3
"""Split ATO ruling texts into numbered paragraphs.

Reads data/rulings/<SERIES>_<year>_<num>.txt (raw ATO print-view text) and
splits each ruling into paragraphs keyed by their paragraph number (1, 2, 3...)
so each paragraph is individually searchable/navigable.

CRITICAL: the ATO ruling .txt files are a SINGLE LINE — there are no newlines,
so every line-anchored regex returns zero matches. Paragraph markers are inline,
e.g. "... What this Ruling is about 1. This Ruling considers ... 2. This Ruling
does not consider ...". We therefore scan inline with a look-behind guard and
ACCEPT a candidate number only when it equals the next expected paragraph number
(1, then 2, 3, ...). That dense-sequence test is what rejects the table of
contents (ascending but not dense) and bystander numbers such as "section 6-20."
or "at [50-51]". Footnote markers are bracketed ("[1]") and never match.

Modelled on scripts/split_rg_paragraphs.py (same JSON shape, same dense-numbering
validation idea).

Output: data/rulings/paragraphs/<REF>.json
  {"ruling": "TR 2025/1", "title": "...", "paragraphs": [{"num": "1", "text": "..."}]}

Usage:
    python3 scripts/split_ruling_paragraphs.py [--series TR] [--all]
    python3 scripts/split_ruling_paragraphs.py --file data/rulings/TR_2025_1.txt
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEXTS_DIR = REPO / "data" / "rulings"
OUT_DIR = REPO / "data" / "rulings" / "paragraphs"

# Series left untouched entirely. ATOID/AID, CR and PR were excluded; the user
# lifted that restriction, so they are ingested like every other series now.
EXCLUDE_PREFIXES: set[str] = set()

# Inline paragraph marker: a 1-3 digit number, a full stop and whitespace, not
# preceded by a digit or '$' (so "1997." / "$1." / "6-20." are not candidates
# by the digit rule; the dense-sequence test then rejects the rest).
MARKER_RE = re.compile(r'(?<![0-9$])(\d{1,3})\.\s')

# Text that terminates the "title" region (table of contents / boilerplate).
_BOILER = re.compile(
    r'(?:Table of Contents|Please note that the|FOI status:|Relying on this Ruling|'
    r'This document incorporates revisions|This cover sheet is provided)',
    re.IGNORECASE)
# GSTR/older files open with a "HEAD NOTE ==== Title: ... ====" banner.
_BANNER = re.compile(r'={3,}')
_META_LABEL = re.compile(
    r'\b(?:Title|Topics|Sections Referenced|Legislative References|'
    r'Related Rulings|Division|Subdivision)\s*:', re.IGNORECASE)
# A ruling citation token, e.g. TR 2025/1 or GSTR 1999/1W.
_CITE = re.compile(r'\b[A-Z][A-Z ]{0,16}?\s?\d{2,4}/\d+[A-Z]?\b')
# The document-type phrase that precedes the descriptive title.
_DOC_TYPE = re.compile(
    r'\b(?:Draft\s+)?(?:Taxation Ruling|Taxation Determination|Product Ruling|'
    r'Class Ruling|Law Companion Ruling|Practical Compliance Guideline|'
    r'Miscellaneous Taxation Ruling|Income Tax Ruling|'
    r'Goods and Services Tax Ruling|Superannuation Guarantee Ruling|'
    r'Self Managed Superannuation Funds Ruling|Taxpayer Alert|'
    r'ATO Interpretative Decision)\b', re.IGNORECASE)


def extract_title(text: str, first_marker_start: int, ref: str) -> str:
    """Best-effort descriptive title from the text before the first paragraph."""
    cut = first_marker_start if first_marker_start > 0 else len(text)
    stop = _BOILER.search(text, 0, cut)
    if stop:
        cut = stop.start()
    head = text[:cut]
    head = re.sub(r'HEAD NOTE', ' ', head, flags=re.IGNORECASE)
    head = _BANNER.sub(' ', head)
    head = _META_LABEL.sub(' ', head)
    head = re.sub(r'\s+', ' ', head).strip()
    head = _CITE.sub(' ', head)
    head = _DOC_TYPE.sub(' ', head)
    head = re.sub(r'\s+', ' ', head).strip(' -,:;.')
    return head[:250] or ref


def derive_ref(stem: str):
    """Return (series, reference) from the file stem, or (None, None)."""
    if stem.startswith("PS_LA_"):
        p = stem.split("_")               # ['PS','LA',year,num]
        return "PS LA", f"PS LA {p[2]}/{p[3]}"
    parts = stem.split("_")
    rtype = parts[0]
    if rtype == "PSLA" and len(parts) >= 3:      # PSLA_2003_10 -> PS LA 2003/10
        return "PS LA", f"PS LA {parts[1]}/{parts[2]}"
    if rtype == "TD" and len(parts) == 4 and parts[2] == "D":   # TD_2026_D1
        return "TD", f"TD {parts[1]}/{parts[3]}"
    if rtype == "IT" and len(parts) == 2:        # IT_234 -> IT 234
        return "IT", f"IT {parts[1]}"
    if len(parts) == 3:
        return rtype, f"{rtype} {parts[1]}/{parts[2]}"
    return None, None


def _densify(text: str, seed: int):
    """Accept inline markers only when each equals the running expected number.

    Returns the list of accepted (num, start, end). Starts at `seed`; the
    dense-sequence requirement is what rejects the table of contents (ascending
    but not dense) and bystander numbers. Footnote markers ('[1]') never match.
    """
    out = []
    expected = seed
    for m in MARKER_RE.finditer(text):
        n = int(m.group(1))
        if n == expected:
            out.append((n, m.start(), m.end()))
            expected += 1
    return out


def parse_ruling(text: str, ref: str, strict: bool = False):
    """Split one ruling's single-line text into numbered paragraphs.

    Primary rule (per the brief): accept a marker only when its number is the
    next expected one, starting at 1. Some rulings print no "1." marker at all
    (paragraph 1 is the unlabelled preamble) and begin their numbering at 2;
    for those only, unless --strict is given, fall back to seeding at the first
    candidate that opens a run of >= 3 consecutive numbers, so the real body
    paragraphs are not lost. The fallback is never used when the 1-based pass
    already found a paragraph.
    """
    bounds = _densify(text, 1)
    used_fallback = False
    if not bounds and not strict:
        best = []
        seen = set()
        for m in MARKER_RE.finditer(text):
            seed = int(m.group(1))
            if seed == 1 or seed in seen:
                continue
            seen.add(seed)
            run = _densify(text, seed)
            if len(run) > len(best):
                best = run
        if len(best) >= 3:
            bounds = best
            used_fallback = True

    paragraphs = []
    for i, (n, s, e) in enumerate(bounds):
        end = bounds[i + 1][1] if i + 1 < len(bounds) else len(text)
        body = text[e:end].strip()
        paragraphs.append({"num": str(n), "text": body})

    first_start = bounds[0][1] if bounds else len(text)
    title = extract_title(text, first_start, ref)
    return {"ruling": ref, "title": title, "paragraphs": paragraphs}, used_fallback


def iter_files(series: str | None, single: Path | None):
    if single is not None:
        yield single
        return
    for f in sorted(TEXTS_DIR.glob("*.txt")):
        prefix = f.stem.split("_")[0]
        if prefix in EXCLUDE_PREFIXES:
            continue
        if series and prefix != series:
            continue
        yield f


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", help="only this series prefix (e.g. TR, TD, GSTR)")
    ap.add_argument("--file", help="single ruling .txt path")
    ap.add_argument("--all", action="store_true", help="split all (non-excluded)")
    ap.add_argument("--strict", action="store_true",
                    help="strict start-at-1 only (no starts-at-2 fallback)")
    args = ap.parse_args()

    single = Path(args.file) if args.file else None
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    stats = {"files": 0, "paragraphs": 0, "zero": [], "one": [],
             "skipped": [], "fallback": []}
    per_series = {}

    for f in iter_files(args.series, single):
        stem = f.stem
        prefix = stem.split("_")[0]
        ref_series, ref = derive_ref(stem)
        if ref is None:
            stats["skipped"].append(f"{stem} (unparsed filename)")
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        result, used_fallback = parse_ruling(text, ref, strict=args.strict)
        n_para = len(result["paragraphs"])
        if used_fallback:
            stats["fallback"].append({"ruling": ref, "paragraphs": n_para})
        out = OUT_DIR / f"{stem}.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                       encoding="utf-8")

        stats["files"] += 1
        stats["paragraphs"] += n_para
        per_series[prefix] = per_series.get(prefix, 0) + n_para
        if n_para == 0:
            stats["zero"].append(ref)
        elif n_para == 1:
            stats["one"].append(ref)

    print(f"Files split: {stats['files']}")
    print(f"Total paragraphs: {stats['paragraphs']}")
    print("Per series paragraphs:", dict(sorted(per_series.items())))
    print(f"Zero-paragraph rulings: {len(stats['zero'])}")
    print(f"One-paragraph rulings: {len(stats['one'])}")
    print(f"Recovered by starts-at-2 fallback: {len(stats['fallback'])}")
    if stats["skipped"]:
        print("Skipped (unparsed filename):", stats["skipped"])
    # machine-readable summary for the report
    (OUT_DIR / "_split_stats.json").write_text(
        json.dumps({"files": stats["files"], "paragraphs": stats["paragraphs"],
                    "per_series": per_series, "zero": stats["zero"],
                    "one": stats["one"], "skipped": stats["skipped"],
                    "fallback": stats["fallback"]}, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()
