#!/usr/bin/env python3.12
"""Refuse to build a corpus from source volumes that are not the compilation being claimed.

WHY: rebuild.sh extracted the then-vendored PDFs (Compilation No. 263, C2026C00122) into
data/itaa-1997/raw and then ran the parser with `--compilation-no 266 --compilation-date
2026-07-01`. The result was a corpus of comp-263 content stamped 266 — it gets OLDER while claiming
to be current, and comp-266-only provisions (e.g. 40-291A) vanish with no error. Nothing in the
build noticed, because nothing compared the source to the claim.

Current state (S1, batch 5, 2026-09-26): source/itaa-1997/ vendors the comp-266 set
(C2026C00324VOL01-12.pdf), so this guard now passes for all four guarded acts, and
scripts/test_rebuild_guards.py pins this script's expectations against both the PDF footer and
the corpus frontmatter. scripts/check_guard_vs_corpus.py is the companion check: this script
cannot see the corpus, so it cannot notice a rebuild that downgrades published law text.

This is the comparison. It reads the compilation number off the source volume itself (not its
filename, which can lie) and exits non-zero when it disagrees with the number the build is about
to stamp. Usage:

    check_source_compilation.py --pdf-dir DIR --expected 266 [--stamp-date 2026-07-01] [--quiet]
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

COMP_RE = re.compile(r"Compilation No\.?\s*(\d+)", re.I)
AUTH_RE = re.compile(r"Authorised Version\s+(C\d{4}C\d+)", re.I)


def read_front_matter(pdf: pathlib.Path, pages: int = 3) -> str:
    try:
        import fitz
    except ImportError:
        return ""
    try:
        doc = fitz.open(pdf)
    except Exception:
        return ""
    text = []
    for i in range(min(pages, doc.page_count)):
        text.append(doc[i].get_text())
    doc.close()
    return "\n".join(text)


def check(pdf_dir: pathlib.Path, expected: int) -> tuple[bool, str]:
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        return False, f"no source PDFs found in {pdf_dir} — the build has nothing to read"
    found: dict[int, list[str]] = {}
    for pdf in pdfs:
        text = read_front_matter(pdf)
        if not text:
            continue
        hits = {int(m) for m in COMP_RE.findall(text)}
        if not hits:
            hits = set()
        for n in hits:
            found.setdefault(n, []).append(pdf.name)
    if not found:
        return (False, f"could not read a 'Compilation No. N' from any of {len(pdfs)} source PDF(s) "
                       f"in {pdf_dir} — refusing rather than trusting the filename")
    nums = sorted(found)
    if nums == [expected]:
        return True, (f"source is Compilation No. {expected} — matches the number the build stamps "
                      f"({len(pdfs)} volume(s))")
    return False, (f"source is Compilation No. {', '.join(map(str, nums))} but the build stamps "
                   f"{expected}: the result would be older content labelled newer, and provisions "
                   f"introduced in {expected} would be missing. Volumes: "
                   f"{sorted({v for vs in found.values() for v in vs})[:4]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--expected", type=int, required=True)
    ap.add_argument("--stamp-date", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    ok, msg = check(pathlib.Path(args.pdf_dir), args.expected)
    if args.quiet and ok:
        return 0
    print(("  OK   " if ok else "  STOP ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
