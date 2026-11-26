#!/usr/bin/env python3
"""Strip leading markdown blockquote markers ('>', '> >', '> > >') from section md files.

These are PDF->markdown ingestion artifacts wrapping every paragraph and anchor tag
(e.g. '> > **(i)** a Part X Australian resident'). The rendered body carries them
into get_section responses and FTS snippets. NZ IT 2007 was ingested via a clean
pipeline and has zero such lines — confirming they are not intrinsic to the text.

Only line-START '>' runs are stripped (safe: '->', '=>', 'A > B' are untouched).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# line start: optional indent, then a run of '>' tokens (space-separated ok), then optional space
_LEAD = re.compile(r"^[ \t]*((?:>[ \t]*)+)")

ACTS = [
    "itaa-1997", "itaa-1936", "gst-1999", "taa-1953", "nz-it-2007",
    "corporations-act-2001", "aml-ctf-2006", "aml-ctf-rules-2007",
    "fbt-1986", "super-1993",
]


def strip_file(path: Path) -> tuple[int, int]:
    """Return (lines_changed, lines_removed)."""
    src_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    changed = 0
    removed = 0
    out: list[str] = []
    for line in src_lines:
        m = _LEAD.match(line)
        if m:
            rest = line[m.end():]
            # whole line was blockquote marker(s) -> drop the line
            if not rest.strip():
                removed += 1
                continue
            out.append(rest)
            changed += 1
        else:
            out.append(line)
    if changed or removed:
        path.write_text("\n".join(out) + ("\n" if src_lines else ""), encoding="utf-8")
    return changed, removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--acts", nargs="*", default=ACTS, help="act dirs to process")
    ap.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = ap.parse_args()

    total_files = total_lines = total_removed = 0
    for act in args.acts:
        secdir = DATA / act / "sections"
        if not secdir.is_dir():
            print(f"skip {act}: no sections dir", file=sys.stderr)
            continue
        files = list(secdir.rglob("*.md"))
        act_files = act_lines = act_removed = 0
        for f in files:
            if args.dry_run:
                n = sum(1 for ln in f.read_text(errors="replace").splitlines() if _LEAD.match(ln))
                if n:
                    act_files += 1
                    act_lines += n
                continue
            c, r = strip_file(f)
            if c or r:
                act_files += 1
                act_lines += c
                act_removed += r
        print(f"{act:<24} files={act_files:>5} lines_fixed={act_lines:>7} lines_removed={act_removed:>6}")
        total_files += act_files
        total_lines += act_lines
        total_removed += act_removed

    print(f"TOTAL files={total_files} lines_fixed={total_lines} lines_removed={total_removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
