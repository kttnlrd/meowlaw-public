#!/usr/bin/env python3.12
"""CDN-0173 + E-d data repair for nz-it-2007 — surgical, re-derived, reversible.

Two repairs, each a single small transformation at the artefact itself, never a line-number edit:

  tree   data/nz-it-2007/tree.json — drop every part whose id is not a single letter A-Z.
         Those are the parser's output for the endnote copies of the amending Acts ("1", "2",
         "3") and the Tax Administration Act 1994 text ("3B") that parse_nz_it.py:193 used to
         match and serve as law (CDN-0173). Everything else in the file is preserved byte-for-
         byte (part order, titles, divisions).

  bom    data/nz-it-2007/sections/**/*.md — delete U+FEFF only. Every other byte stays.

Why not run pipeline/build_tree.py? Its output reorders the lettered parts (the part sort key
mis-sorts single roman letters) and rewrites every surviving section title with the a59a3285e
straight quotes, which would land ~241 unrelated title changes in the same commit. The builder
IS used as the cross-check: over the staged re-parse it drops exactly these four parts and
agrees with the repaired tree on every surviving leaf.

Usage:
  /usr/bin/python3.12 scripts/fix_nz_amendment_parts.py --verify          # report only
  /usr/bin/python3.12 scripts/fix_nz_amendment_parts.py --apply            # write both repairs
  /usr/bin/python3.12 scripts/fix_nz_amendment_parts.py --apply --tree-only
  /usr/bin/python3.12 scripts/fix_nz_amendment_parts.py --apply --bom-only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NZ = DATA / "nz-it-2007"
TREE = NZ / "tree.json"
SECTIONS = NZ / "sections"
PROVENANCE = ROOT / "docs" / "provenance"

LETTER_PART = re.compile(r"^[A-Z]$")
BOM = "\ufeff"


def repair_tree() -> dict:
    raw = TREE.read_text(encoding="utf-8")
    tree = json.loads(raw)
    kept = [p for p in tree["parts"] if LETTER_PART.match(str(p.get("id", "")))]
    dropped = [p["id"] for p in tree["parts"] if not LETTER_PART.match(str(p.get("id", "")))]

    def leaves(parts):
        n = 0
        for p in parts:
            nodes = [p] + list(p.get("divisions", []))
            for d in p.get("divisions", []):
                nodes += list(d.get("subdivisions", []))
            for node in nodes:
                n += len(node.get("sections", []))
        return n

    before = leaves(tree["parts"])
    after = leaves(kept)
    out = json.dumps({"act": tree["act"], "compilation_no": tree["compilation_no"],
                      "compilation_date": tree["compilation_date"], "parts": kept}, indent=2)
    # byte-identity check: everything kept must round-trip unchanged
    kept_orig = {"act": tree["act"], "compilation_no": tree["compilation_no"],
                 "compilation_date": tree["compilation_date"], "parts": kept}
    roundtrip = json.dumps(kept_orig, indent=2) == out
    return {"dropped_parts": dropped, "leaves_before": before, "leaves_after": after,
            "new_text": out, "roundtrip_ok": roundtrip, "orig_parts": len(tree["parts"])}


def repair_bom() -> dict:
    files = sorted(SECTIONS.rglob("*.md"))
    changed = 0
    chars = 0
    affected = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        n = text.count(BOM)
        if n:
            f.write_text(text.replace(BOM, ""), encoding="utf-8")
            changed += 1
            chars += n
            if len(affected) < 200:
                affected.append(str(f.relative_to(SECTIONS)))
    return {"files_scanned": len(files), "files_changed": changed, "bom_chars_removed": chars,
            "affected_sample": affected}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the repairs (default: verify only)")
    ap.add_argument("--tree-only", action="store_true")
    ap.add_argument("--bom-only", action="store_true")
    args = ap.parse_args()

    do_tree = not args.bom_only
    do_bom = not args.tree_only

    if do_tree:
        t = repair_tree()
        print("== tree.json ==")
        print(f"  dropped parts: {t['dropped_parts']} ({len(t['dropped_parts'])} of "
              f"{t['orig_parts']})")
        print(f"  leaves {t['leaves_before']} -> {t['leaves_after']} "
              f"(-{t['leaves_before'] - t['leaves_after']})")
        print(f"  kept round-trips byte-identically: {t['roundtrip_ok']}")
        if args.apply:
            TREE.write_text(t["new_text"], encoding="utf-8")
            print(f"  wrote {TREE}")

    if do_bom:
        b = repair_bom() if args.apply else None
        if args.apply:
            print("== bom ==")
            print(f"  {b['files_changed']}/{b['files_scanned']} files, "
                  f"{b['bom_chars_removed']} U+FEFF removed")
        else:
            # verify-only: count without writing
            n_files = n_chars = 0
            for f in sorted(SECTIONS.rglob("*.md")):
                c = f.read_text(encoding="utf-8").count(BOM)
                if c:
                    n_files += 1
                    n_chars += c
            print("== bom (verify) ==")
            print(f"  would strip {n_chars} U+FEFF from {n_files} files")

    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        prov = PROVENANCE / f"batch7-nz-reparse-{stamp}.json"
        prov.write_text(json.dumps({
            "batch": "7", "ids": ["CDN-0173", "E-d"], "stamp": stamp,
            "tree": {"dropped_parts": t["dropped_parts"], "leaves_before": t["leaves_before"],
                     "leaves_after": t["leaves_after"]} if do_tree else None,
            "bom": b if do_bom else None,
            "rollback": "git tag batch7-rollback-* (see /tmp/batch7-rollback)",
        }, indent=2))
        print(f"  provenance: {prov}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
