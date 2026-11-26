#!/usr/bin/env python3
"""Splice scriptkitty's clean section bodies into MeowLaw's files.

MeowLaw's ITAA-1997 sections have broken ``ingest-*`` fences (tables/formulas
that the PDF extractor dumped as raw fixed-width blocks). Scriptkitty
(legislation-explorer-db) has the SAME sections with the tables already
rendered correctly. We keep MeowLaw's frontmatter (full part/division/
subdivision titles) and take scriptkitty's body (clean tables + prose).

Usage:
  python scripts/splice_scriptkitty_tables.py --dry-run
  python scripts/splice_scriptkitty_tables.py --write
"""
from __future__ import annotations
import os

import argparse
import re
import subprocess
import sys
from pathlib import Path

MEOWLAW = Path(__file__).resolve().parent.parent
SCRIPTKITTY = Path(os.environ["LEGISLATION_EXPLORER_DB_DIR"])
SECTIONS = "data/itaa-1997/sections"

FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.S)


def affected_files() -> list[str]:
    out = subprocess.check_output(
        ["git", "grep", "-l", "ingest-", "HEAD", "--", f"{SECTIONS}/"],
        cwd=MEOWLAW,
    ).decode()
    files = []
    for line in out.splitlines():
        p = line.split(":", 1)[1] if line.startswith("HEAD:") else line
        files.append(p)
    return files


def frontmatter(text: str) -> str:
    m = FRONTMATTER_RE.match(text)
    return m.group(0) if m else ""


def body(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--file", help="single file path (relative to meowlaw repo)")
    args = ap.parse_args()

    if args.file:
        files = [args.file]
    else:
        files = affected_files()

    changed = 0
    missing = 0
    for rel in files:
        sk_path = SCRIPTKITTY / rel
        if not sk_path.exists():
            print(f"[missing] {rel}")
            missing += 1
            continue
        meow = (MEOWLAW / rel).read_text(encoding="utf-8")
        sk = sk_path.read_text(encoding="utf-8")
        out = frontmatter(meow) + body(sk)
        if out != meow:
            changed += 1
            if args.write:
                (MEOWLAW / rel).write_text(out, encoding="utf-8")
                print(f"[spliced] {rel}")
        else:
            print(f"[unchanged] {rel}")

    print(f"\n{'[WROTE]' if args.write else '[DRY-RUN]'} {changed} files changed, {missing} missing scriptkitty counterparts, {len(files)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
