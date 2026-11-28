#!/usr/bin/env python3
"""
repair_integrity_defects.py — Fix the two critical defect classes found by
verify_data_integrity.py (seed 42 baseline):

1. TOC-dump injection: page-header fragments ("Subdivision X—Title Table of
   sections 326-230 Indexing of amounts ...") concatenated onto the end of
   body lines. Truncates at the "Subdivision X—Title Table of sections"
   boundary, preserving the real body text.

2. Duplicate anchor IDs: the anchor generator dropped parent context
   (e.g. s61F-i reused under both (a)(i) and (c)(i); s118-580-2-a reused in
   the main text and the Note). Content is distinct — only the IDs collide,
   which breaks getElementById deep-links. First occurrence keeps its ID;
   later occurrences get a numeric suffix (-2, -3, ...).

Usage:
  python3 scripts/repair_integrity_defects.py            # dry run
  python3 scripts/repair_integrity_defects.py --apply    # write changes
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"

# Matches appended TOC-dump junk tails. Two shapes:
#   1. "...text. Subdivision 326-M—Indexation Table of sections 326-230 ..."
#   2. "...text. Table of sections Operative provisions 396-5 Statement ..."
TOC_JUNK_RE = re.compile(
    r"(?:Subdivision\s+\d+[A-Z]*-?[A-Z]*\d*\s*[—–-]\s*[^.\n]{0,80}?\s*)?"
    r"Table of sections\s+(?:\d+-\d+|\S+)\s+.+$"
)

ANCHOR_RE = re.compile(r'(<a id="([^"]+)"></a>)')


def repair_toc(text: str) -> tuple[str, int]:
    """Truncate TOC-dump tails. Returns (new_text, lines_changed)."""
    changed = 0
    out = []
    for ln in text.splitlines():
        m = TOC_JUNK_RE.search(ln)
        if m and m.start() > 0:  # only when there is real content before it
            out.append(ln[: m.start()].rstrip())
            changed += 1
        else:
            out.append(ln)
    return "\n".join(out), changed


def repair_anchors(text: str) -> tuple[str, int]:
    """Suffix duplicate anchor IDs (-2, -3 ...) keeping the first occurrence."""
    counts = Counter(m.group(2) for m in ANCHOR_RE.finditer(text))
    seen: dict[str, int] = {}
    changed = 0

    def repl(m: re.Match) -> str:
        nonlocal changed
        aid = m.group(2)
        if counts[aid] <= 1:
            return m.group(0)
        n = seen.get(aid, 0) + 1
        seen[aid] = n
        if n == 1:
            return m.group(0)
        changed += 1
        return f'<a id="{aid}-{n}"></a>'

    return ANCHOR_RE.sub(repl, text), changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--acts", nargs="*", default=None, help="limit to acts (default: all)")
    args = ap.parse_args()

    acts = [d.name for d in sorted(DATA.iterdir()) if (d / "sections").is_dir()]
    if args.acts:
        acts = [a for a in acts if a in args.acts]

    tot_toc = tot_toc_files = tot_anchor = tot_anchor_files = 0
    print(f"{'DRY RUN' if not args.apply else 'APPLYING'} — acts: {len(acts)}")
    for act in acts:
        sec = DATA / act / "sections"
        for p in sorted(sec.rglob("*.md")):
            orig = p.read_text(encoding="utf-8", errors="replace")
            t, toc_ch = repair_toc(orig)
            t, anc_ch = repair_anchors(t)
            if (toc_ch or anc_ch) and args.apply:
                p.write_text(t, encoding="utf-8")
            tot_toc += toc_ch
            tot_toc_files += 1 if toc_ch else 0
            tot_anchor += anc_ch
            tot_anchor_files += 1 if anc_ch else 0

    print(f"TOC lines truncated:  {tot_toc}  (files: {tot_toc_files})")
    print(f"Anchor IDs suffixed:  {tot_anchor}  (files: {tot_anchor_files})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
