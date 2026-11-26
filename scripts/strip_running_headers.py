#!/usr/bin/env python3
"""strip_running_headers.py — remove ATO PDF running-header noise from section .md files.

Defect: inline running headers concatenated onto legitimate text lines, e.g.
    "...the asset's *market value; or International aspects of income tax
     Chapter 4 General Part 4-5 Capital gains and foreign residents
     Division 855 Section 855-35"
The verifier's PAGE_HEADER_RE only catches fixed phrases at line start, so this
variant (variable titles, inline at line end) slipped through. Manual review of
a randomised integrity sample caught it (450 files across 6 acts).

Safety: only strips when the matched header contains the file's OWN
division_title / part_title from frontmatter — a legit cross-reference can't
contain the file's own part/division titles in running-header order.

Usage:
  python3 scripts/strip_running_headers.py --dry-run   # report only
  python3 scripts/strip_running_headers.py             # apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
ACTS = ["itaa-1997", "itaa-1936", "taa-1953", "gst-1999", "fbt-1986", "sis-1993",
        "corporations-act-2001", "aml-ctf-2006", "nz-it-2007"]

FM_RE = re.compile(r"^---\n(.*?)\n---", re.S)
HEADER_RE = re.compile(
    r"(?P<header>"
    r"(?:[A-Z][^.;:]{3,120}?\s+Chapter\s+\d+\s+[A-Za-z]+\s+)?"   # chapter-title "Chapter N x" (ITAA97)
    r"(?:[A-Z][^.;:]{3,150}?\s+Part\s+[IVX\d-]+\s+)?"            # part-title "Part <id>"
    r"[A-Z][^.;:]{2,150}?\s+Division\s+[\w-]+\s+Section\s+[\w-]+"
    r")\s*$",
    re.M,
)


def frontmatter(text: str) -> dict:
    m = FM_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip('"')
    return out


def strip_file(path: Path) -> tuple[int, list[str]]:
    text = path.read_text(encoding="utf-8")
    fm = frontmatter(text)
    div_title = fm.get("division_title", "")
    part_title = fm.get("part_title", "")
    markers = [t for t in (div_title, part_title) if len(t) >= 8]
    if not markers:
        return 0, []
    removed = []
    new_text, n = HEADER_RE.subn(lambda m: _replace(m, markers, removed), text)
    if n:
        path.write_text(new_text, encoding="utf-8")
    return n, removed


def _replace(m: re.Match, markers: list[str], removed: list[str]) -> str:
    header = m.group("header")
    if any(mk in header for mk in markers):
        removed.append(header[-100:])
        return ""
    return m.group(0)  # leave untouched — not our running header


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total_files = total_lines = 0
    for act in ACTS:
        base = DATA / act / "sections"
        if not base.exists():
            continue
        for f in sorted(base.rglob("*.md")):
            if args.dry_run:
                text = f.read_text(encoding="utf-8")
                fm = frontmatter(text)
                markers = [t for t in (fm.get("division_title", ""), fm.get("part_title", "")) if len(t) >= 8]
                if not markers:
                    continue
                hits = [m for m in HEADER_RE.finditer(text) if any(mk in m.group("header") for mk in markers)]
                if hits:
                    total_files += 1
                    total_lines += len(hits)
                    print(f"{f}  ({len(hits)} line(s))")
                    for h in hits[:1]:
                        print(f"    ...{h.group('header')[-110:]}")
            else:
                n, removed = strip_file(f)
                if n:
                    total_files += 1
                    total_lines += n
                    print(f"{f}  {n} line(s) stripped")
    print(f"\n{'WOULD strip' if args.dry_run else 'STRIPPED'}: {total_files} files, {total_lines} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
