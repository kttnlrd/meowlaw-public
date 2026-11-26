#!/usr/bin/env python3
"""Split ASIC Regulatory Guide texts into numbered paragraphs.

Reads data/regulatory-guides/texts/RG_xxx.txt (raw PDF text) and splits into
paragraphs keyed by their RG paragraph number (e.g. RG 103.15), so each
paragraph is individually searchable/navigable.

Output: data/regulatory-guides/paragraphs/RG_xxx.json
  {"rg": "RG 103", "title": "...", "paragraphs": [{"num": "103.1", "text": "..."}]}

Usage:
    python3 scripts/split_rg_paragraphs.py [--rg 103] [--all]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TEXTS_DIR = Path(__file__).resolve().parent.parent / "data" / "regulatory-guides" / "texts"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "regulatory-guides" / "paragraphs"

# Paragraph marker: "RG 103.1" (leading whitespace ok), optional "PS 103.1"
MARKER_RE = re.compile(r'^\s*RG\s+(\d+)\.(\d+)\b')

# Lines that are page furniture / not content
FURNITURE = re.compile(
    r'^\s*(?:'
    r'©\s+Australian Securities|'
    r'REGULATORY GUIDE\s+\d+|'
    r'Page\s+\d+|'
    r'^\s*Contents\s*$|'
    r'^\s*$'
    r')',
    re.IGNORECASE,
)

# Characters that immediately after "RG N.M" mark a cross-reference, not a header:
# punctuation (e.g. "RG 166.4–RG 166.6);", "RG 262.73 (and its note)") or a
# lowercase continuation word ("RG 262.73 to reflect ..."). Note: "]" is NOT
# listed — ASIC PDFs leave stray "]" after real headers (e.g. "RG 144.1]").
_REF_PUNCT = set('–—-,.;()')


def _looks_like_reference(rest: str) -> bool:
    """True if the text after the 'RG N.M' marker reads as a cross-reference,
    not the start of the paragraph's own text."""
    if not rest:
        return False
    first = rest[0]
    return first.islower() or first in _REF_PUNCT


def parse_rg(text: str, rg_num: int) -> dict:
    paragraphs = []
    current = None
    lines = text.split("\n")
    title = ""
    expected = 1  # next expected paragraph sub-number (ASIC numbers are dense 1..N)

    for line in lines:
        stripped = line.strip()

        # title: first non-empty line before any marker
        if not title and stripped and not MARKER_RE.match(line) and not FURNITURE.match(stripped):
            # skip obvious header noise
            if re.match(r'^(REGULATORY GUIDE|ASIC Law|Issued|Updated|About this guide|Editor)', stripped, re.I):
                continue
            title = stripped
            continue

        m = MARKER_RE.match(line)
        if m:
            para_rg = int(m.group(1))
            para_num = int(m.group(2))
            if para_rg != rg_num:
                continue
            rest = MARKER_RE.sub("", line, count=1).strip()
            # A header is only accepted when it reads as a paragraph start (not a
            # cross-reference like "see RG 262.106" or "RG 166.4–RG 166.6);") AND
            # its number is the next expected one (dense sequential 1..N),
            # tolerating a single-number gap for RGs whose numbering skips.
            if (not _looks_like_reference(rest)
                    and (para_num == expected or para_num == expected + 1)):
                if current:
                    paragraphs.append(current)
                current = {"num": f"{rg_num}.{para_num}", "text": [rest] if rest else []}
                expected = para_num + 1
            elif current is not None:
                # cross-reference / out-of-order marker: keep it as body text
                current["text"].append(stripped)
        else:
            if current is not None:
                # continuation line — skip furniture
                if FURNITURE.match(line) or FURNITURE.match(stripped):
                    continue
                current["text"].append(stripped)

    if current:
        paragraphs.append(current)

    # join continuation lines, drop leading/trailing blanks
    for p in paragraphs:
        txt = [t for t in p["text"] if t]
        p["text"] = "\n".join(txt).strip()

    return {"rg": f"RG {rg_num}", "title": title, "paragraphs": paragraphs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rg", type=int, help="single RG number to split")
    ap.add_argument("--all", action="store_true", help="split all RG texts")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.rg:
        files = [TEXTS_DIR / f"RG_{args.rg:03d}.txt"]
    elif args.all:
        files = sorted(TEXTS_DIR.glob("RG_*.txt"))
    else:
        files = sorted(TEXTS_DIR.glob("RG_*.txt"))

    done = 0
    for f in files:
        if not f.exists():
            print(f"SKIP (missing): {f.name}")
            continue
        rg_num = int(f.name.replace("RG_", "").replace(".txt", ""))
        text = f.read_text(encoding="utf-8", errors="replace")
        result = parse_rg(text, rg_num)
        out = OUT_DIR / f"RG_{rg_num:03d}.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        done += 1
        print(f"{f.name}: {len(result['paragraphs'])} paragraphs -> {out.name}")

    print(f"\nDone. {done} RGs split.")


if __name__ == "__main__":
    main()
