#!/usr/bin/env python3
"""CDN-0192 (C11_table_midword): re-join words the PDF->markdown section parser
cut across adjacent pipe-table cells.

The parser inserted a cell boundary in the middle of a word whenever the source
PDF wrapped a line inside a table cell, e.g.

    | *To find definitions of ast | erisked terms, see the Dictionary, ... |

The repair is character-preserving: it only removes the spurious cell boundary
between a word's two halves ('ast' + 'erisked' -> 'asterisked'). No text is
invented, reordered or dropped, so it cannot regress the corpus the way a full
table rebuild could.

Scope guard (default): only files whose ONLY C11 table-coherence finding is a
mid-word split are repaired, so every file the script touches is fully clean on
re-scan. Files that also carry truncation/glyph damage are left to the
source-PDF table rebuild (see .hermes/plans/2026-09-07_table-corruption-rebuild.md).

Usage:
  python3 scripts/repair_table_midword_splits.py [--apply] [--all-files] [--report PATH]
"""
from __future__ import annotations
import os

import argparse
import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
SCANNER = REPO / "scripts" / "scan_corpus_error_classes.py"

try:
    _WORDS = set(open("/usr/share/dict/words", encoding="utf-8").read().split())
except Exception:  # pragma: no cover
    _WORDS = None


def _scanner():
    spec = importlib.util.spec_from_file_location("scan_corpus_error_classes", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tail_word(cell: str) -> str:
    parts = cell.strip().split()
    return parts[-1].strip(",*'\"()") if parts else ""


def head_word(cell: str) -> str:
    parts = cell.strip().split()
    return parts[0].strip(",*'\"()") if parts else ""


def split_positions(cells: list[str]) -> list[int]:
    """Indices i where cells[i]/cells[i+1] are two halves of one word."""
    out = []
    if _WORDS is None:
        return out
    for i in range(len(cells) - 1):
        tw, hw = tail_word(cells[i]).lower(), head_word(cells[i + 1]).lower()
        if not tw or not hw:
            continue
        joined = tw + hw
        if joined in _WORDS and not (tw in _WORDS and hw in _WORDS):
            out.append(i)
    return out


def repair_row(line: str, verifier=None) -> tuple[str, list[str], list[str]]:
    """Merge every word split across cells in one pipe-table row.

    With a `verifier` (callable: merged-token -> bool), merges that cannot be
    confirmed against the authoritative source text are left untouched.
    """
    merges: list[str] = []
    skipped: list[str] = []
    if not line.startswith("|") or re.match(r"^\|\s*---", line):
        return line, merges, skipped
    trailing_nl = line.endswith("\n")
    body = line.rstrip("\n")
    cells = body.strip().strip("|").split("|")
    while True:
        pos = split_positions(cells)
        if not pos:
            break
        i = pos[0]
        merged = f"{tail_word(cells[i])}|{head_word(cells[i + 1])}"
        if verifier is not None and not verifier(merged):
            skipped.append(merged)
            # don't retry this boundary forever
            break
        merges.append(merged)
        cells[i] = cells[i].rstrip() + cells[i + 1].lstrip()
        del cells[i + 1]
    if not merges:
        return line, merges, skipped
    return "|" + "|".join(cells) + "|" + ("\n" if trailing_nl else ""), merges, skipped


def clean_scope() -> list[str]:
    """Files whose only C11 finding is a mid-word split."""
    sc = _scanner()
    sc.scan_table_coherence()
    per: dict[str, set[str]] = {}
    for f in sc.findings:
        per.setdefault(f["path"], set()).add(f["class"])
    return sorted(p for p, cls in per.items() if cls == {"C11_table_midword"})


STAGING = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data"))
_SRC_CACHE: dict[str, str] = {}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u2019", "'").replace("\u2018", "'")).lower()


def source_text(act: str) -> str:
    """Normalised authoritative layout text for an act (repo raw + staging raw)."""
    if act in _SRC_CACHE:
        return _SRC_CACHE[act]
    chunks = []
    for base in (DATA / act / "raw", STAGING / act / "raw"):
        if base.is_dir():
            for p in sorted(base.glob("*.txt")):
                try:
                    chunks.append(p.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    pass
    _SRC_CACHE[act] = _norm("\n".join(chunks))
    return _SRC_CACHE[act]


def verify_merge(act: str, merged: str) -> bool:
    """True when the re-joined word appears unbroken in the source layout text."""
    src = source_text(act)
    if not src:
        return False
    # the merge record is 'left|right'; rebuild the joined token
    left, _, right = merged.partition("|")
    joined = _norm(left + right)
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(joined)}(?![a-z0-9])", src))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--all-files", action="store_true",
                    help="repair every mid-word file, not just the clean-scope subset")
    ap.add_argument("--no-verify", dest="verify", action="store_false", default=True,
                    help="skip the authoritative-source verification gate")
    ap.add_argument("--report", default="/tmp/repair_midword_splits.json")
    args = ap.parse_args()

    if args.all_files:
        sc = _scanner()
        sc.scan_table_coherence()
        targets = sorted({f["path"] for f in sc.findings if f["class"] == "C11_table_midword"})
    else:
        targets = clean_scope()

    report = []
    total_merges = total_skipped = 0
    for rel in targets:
        act = rel.split("/")[0]
        p = DATA / rel
        verifier = (lambda m, a=act: verify_merge(a, m)) if args.verify else None
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        out, edits = [], []
        for i, ln in enumerate(lines, 1):
            new, merges, skipped = repair_row(ln, verifier)
            if merges or skipped:
                edits.append({"line": i, "merges": merges, "unverified_skipped": skipped,
                              "before": ln.strip()[:140], "after": new.strip()[:140]})
            out.append(new)
        if any(e["merges"] for e in edits):
            total_merges += sum(len(e["merges"]) for e in edits)
            total_skipped += sum(len(e["unverified_skipped"]) for e in edits)
            if args.apply:
                p.write_text("".join(out), encoding="utf-8")
            report.append({"file": rel, "edits": edits})

    Path(args.report).write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: {len(report)} file(s), "
          f"{total_merges} word split(s) re-joined, {total_skipped} unverified skipped")
    for r in report:
        for e in r["edits"]:
            v = "OK" if not e["unverified_skipped"] else f"UNVERIFIED {e['unverified_skipped']}"
            print(f"  {r['file']}:{e['line']} [{' '.join(e['merges'])}] {v}")
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
