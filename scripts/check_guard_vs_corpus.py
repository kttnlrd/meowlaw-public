#!/usr/bin/env python3.12
"""Guard of the guard: refuse a rebuild whose source is OLDER than the corpus it replaces.

WHY: `check_source_compilation.py` answers "is the source the compilation this build would
stamp?". It cannot see the corpus. So the script's own remedy (b) — "change the
--compilation-no to the compilation the source actually is" — would pass that check while
replacing a newer corpus with an older one, silently, because the produced file and the
stamp would then agree with each other and disagree with what was already published.

For ITAA 1997 that remedy is explicitly rejected (see rebuild.sh's 1b comment): rebuild.sh:87-90
option (b) would rebuild the real comp-266 corpus from the comp-263 PDFs and delete at least 26
comp-266-only sections (40-291A, Div 119, 112-155..185).

This script is the second reading. It compares the number the build is about to stamp with the
`compilation_no` recorded in the section frontmatter already on disk:

  * corpus older than the stamp  -> OK. That is the normal direction: the source is newer than
    what is published, so the rebuild upgrades it.
  * corpus equal to the stamp    -> OK. Same compilation; the rebuild is idempotent.
  * corpus NEWER than the stamp  -> FAIL, unless the act has no corpus sections at all. The
    rebuild would downgrade published law text under a number that no longer claims to be current.

Usage:

    check_guard_vs_corpus.py --act itaa-1997 --expected 266 [--corpus-dir DIR] [--quiet]
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

COMP_RE = re.compile(r"^compilation_no:\s*\"?(\d+)", re.M)
DATE_RE = re.compile(r"^compilation_date:\s*\"?([0-9-]+)", re.M)


def read_frontmatter(path: pathlib.Path) -> str:
    """The frontmatter block only, so a body line can never be mistaken for metadata."""
    head = path.open(encoding="utf-8", errors="replace").read(4000)
    m = re.match(r"^---\n(.*?)\n---\s*\n", head, re.S)
    return m.group(1) if m else ""


def corpus_numbers(act: str, corpus_dir: pathlib.Path) -> tuple[dict[int, int], str | None]:
    """-> ({compilation_no: file count}, most common compilation_date)."""
    counts: dict[int, int] = {}
    dates: dict[str, int] = {}
    sections = corpus_dir / act / "sections"
    if not sections.is_dir():
        return counts, None
    for f in sections.rglob("*.md"):
        fm = read_frontmatter(f)
        m = COMP_RE.search(fm)
        if m:
            counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
        m2 = DATE_RE.search(fm)
        if m2:
            dates[m2.group(1)] = dates.get(m2.group(1), 0) + 1
    best_date = max(dates, key=lambda d: dates[d]) if dates else None
    return counts, best_date


def check(act: str, expected: int, corpus_dir: pathlib.Path,
          stamp_date: str = "") -> tuple[bool, str]:
    counts, corpus_date = corpus_numbers(act, corpus_dir)
    if not counts:
        return True, (f"no {act} section frontmatter on disk (nothing to downgrade) — "
                      f"stamp {expected} may proceed")
    newest = max(counts)
    n = sum(counts.values())
    where = ", ".join(f"{c} ({n_} file{'s' if n_ != 1 else ''})" for c, n_ in sorted(counts.items()))
    note = ""
    if stamp_date and corpus_date and stamp_date != corpus_date:
        note = (f" [note: corpus date {corpus_date}, stamp date {stamp_date} — same compilation, "
                f"label only]")
    if newest > expected:
        return False, (f"REJECTED: the corpus is Compilation No. {where} but this build would "
                       f"stamp {expected} across {n} section file(s). The source is OLDER than "
                       f"the law already published: proceeding would downgrade {act} without "
                       f"changing the number anyone compares against{note}")
    if newest < expected:
        return True, (f"corpus is Compilation No. {where}; the source ({expected}) is newer — "
                      f"this rebuild upgrades {act}{note}")
    return True, (f"corpus is already Compilation No. {expected} ({n} file(s)) — "
                  f"idempotent rebuild{note}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--act", required=True)
    ap.add_argument("--expected", type=int, required=True)
    ap.add_argument("--corpus-dir", default=str(pathlib.Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--stamp-date", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    ok, msg = check(args.act, args.expected, pathlib.Path(args.corpus_dir), args.stamp_date)
    if args.quiet and ok:
        return 0
    print(("  OK   " if ok else "  STOP ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
