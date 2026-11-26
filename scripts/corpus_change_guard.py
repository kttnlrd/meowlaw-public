#!/usr/bin/env python3
"""CDN-0193: guard on CHANGED corpus section files.

The corpus is damaged by its own update process: a compilation update lands
thousands of section .md files with no committed pipeline and no check on the
result.  s 82-150 (ITAA 1997 compilation 266) is the signature — a legal
formula ingested as a 3-cell pipe block with the 'where:' prose glued under
the separator.  This guard runs over the diff, not the corpus, and refuses to
let that shape land silently.

  G-A  fake-table signature on a NEWLY ADDED pipe block   -> BLOCK
  G-B  prose loss                                         -> WARN (BLOCK when
       non-pipe content collapses while pipe content grows)
  G-C  newly introduced C11 findings                      -> BLOCK

Needs no fitz.  Library: check_file(old, new, path) -> dict.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The C11 detectors and the G5 glyph set, imported — never forked.  (G5 in
# table_rebuild_gate.py is itself SCANNER.EXTRACT_GLYPH; importing the scanner
# gets the same object without dragging in fitz.)
SCANNER = _load("scan_corpus_error_classes")
GLYPH_RE = SCANNER.EXTRACT_GLYPH

CORPUS_RE = re.compile(r"(?:^|/)data/[^/]+/sections/.*\.md$")
SEP_RE = re.compile(r"^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
# Lines that legitimately sit flush under a table: markdown structure, not prose.
STRUCTURAL = re.compile(r"^\s*(#|>|<|\*|-{3,}|\d+\.|\[|!\[|\||$)")

PROSE_SHRINK = 0.40      # >40% collapse in non-pipe content ...
MIN_PROSE_CHARS = 200    # ... only judged on files with real prose to lose


def body_lines(text: str) -> list[str]:
    """Lines with the YAML frontmatter blanked out (numbering preserved).

    Frontmatter values contain '|' often enough to look like a table row.
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return [""] * (i + 1) + lines[i + 1:]
    return lines


def pipe_blocks(lines: list[str]) -> list[tuple[int, list[str], str | None]]:
    """Maximal runs of pipe lines -> (1-based start, block lines, next line)."""
    out, i, n = [], 0, len(lines)
    while i < n:
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            out.append((i + 1, lines[i:j], lines[j] if j < n else None))
            i = j
        else:
            i += 1
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def fake_table_reasons(start: int, block: list[str], nxt: str | None) -> list[tuple[str, str]]:
    """The 82-150 shape.  Applied to newly added blocks only. -> [(level, why)]"""
    r: list[tuple[str, str]] = []
    sep_at = [k for k, ln in enumerate(block) if SEP_RE.match(ln)]
    if not any(k < 2 for k in sep_at):
        r.append(("BLOCK", f"line {start}: pipe block has no '| --- |' separator in its first two lines"))
    if nxt is not None and nxt.strip() and not STRUCTURAL.match(nxt):
        r.append(("BLOCK", f"line {start + len(block)}: prose glued to the table with no blank line: {nxt.strip()[:70]!r}"))
    for k, ln in enumerate(block):
        m = next((m for m in GLYPH_RE.finditer(ln)
                  if not SCANNER._is_natural_word_accent(ln, m)), None)
        if m:
            r.append(("BLOCK", f"line {start + k}: formula glyph {m.group(0)!r} in a table row: {ln.strip()[:70]}"))
            break
    data = [ln for k, ln in enumerate(block) if k not in sep_at]
    nrows = len(data) - (1 if sep_at else 0)   # minus the header row
    # A table with no rows at all is always wreckage.  A one-row table is
    # suspicious (in this corpus it is nearly always half of a split table)
    # but legitimate single-item tables exist, so it warns rather than blocks.
    if nrows < 1:
        r.append(("BLOCK", f"line {start}: pipe block has no data rows ({len(block)} lines total)"))
    elif nrows < 2:
        r.append(("WARN", f"line {start}: pipe block has only 1 data row"))
    widths = {len(_cells(ln)) for ln in data}
    if len(widths) > 1 and not sep_at:
        r.append(("BLOCK", f"line {start}: cell counts vary across rows ({sorted(widths)}) with no separator row"))
    return r


def _split(text: str) -> tuple[list[str], list[str]]:
    lines = body_lines(text)
    pipe = [l for l in lines if l.lstrip().startswith("|")]
    return pipe, [l for l in lines if not l.lstrip().startswith("|")]


def check_file(old: str, new: str, path: str) -> dict:
    blocks_old = {"\n".join(b).strip() for _, b, _ in pipe_blocks(body_lines(old))}
    reasons: list[tuple[str, str]] = []   # (level, text)

    # G-A
    for start, block, nxt in pipe_blocks(body_lines(new)):
        if "\n".join(block).strip() in blocks_old:
            continue
        for level, why in fake_table_reasons(start, block, nxt):
            reasons.append((level, f"G-A {why}"))

    # G-B
    old_pipe, old_prose = _split(old)
    new_pipe, new_prose = _split(new)
    if old.strip():
        lost = Counter(WORD_RE.findall("\n".join(old_prose)))
        lost.subtract(Counter(WORD_RE.findall("\n".join(new_prose))))
        lost = {w: c for w, c in lost.items() if c > 0}
        nlost = sum(lost.values())
        op, np_ = len("".join(old_prose)), len("".join(new_prose))
        collapse = op >= MIN_PROSE_CHARS and np_ < op * (1 - PROSE_SHRINK)
        if collapse and len("".join(new_pipe)) > len("".join(old_pipe)):
            reasons.append(("BLOCK", f"G-B prose collapsed {op}->{np_} chars "
                                     f"({100 * (op - np_) // op}%) while table content grew "
                                     f"{len(''.join(old_pipe))}->{len(''.join(new_pipe))} — prose converted to rows"))
        elif nlost:
            sample = ", ".join(sorted(lost)[:6])
            reasons.append(("WARN", f"G-B {nlost} prose token(s) no longer present: {sample}"))

    # G-C
    def c11(text):
        return {f["class"] + "|" + re.sub(r"^line \d+: ", "", f["detail"])
                for f in SCANNER.table_coherence_findings(body_lines(text), path)}
    for f in sorted(c11(new) - c11(old)):
        reasons.append(("BLOCK", f"G-C new detector finding: {f[:150]}"))

    status = "BLOCK" if any(l == "BLOCK" for l, _ in reasons) else ("WARN" if reasons else "OK")
    return {"path": path, "status": status,
            "reasons": [f"[{l}] {t}" for l, t in reasons]}


# ── git plumbing ────────────────────────────────────────────────────────────
def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def _show(rev: str, path: str) -> str:
    p = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def collect(args) -> list[tuple[str, str, str]]:
    """-> [(path, old, new)] for corpus section files only.

    The default set is staged changes PLUS untracked (never-added) section files.
    Untracked matters: a bulk dump into data/ never touches the index, so a
    staged-only scan walks straight past it. One external workstream landed 1,458
    section files into data/nz-master-tax-guide/ that way and a staged-only guard
    would not have looked at a single one of them.
    """
    if args.paths:
        return [(p, _show("HEAD", p), Path(p).read_text(encoding="utf-8", errors="replace"))
                for p in args.paths if CORPUS_RE.search(p)]
    if args.range:
        a, _, b = args.range.partition("..")
        b = b or "HEAD"
        names = _git("diff", "--name-only", f"{a}..{b}").split()
        return [(p, _show(a, p), _show(b, p)) for p in names if CORPUS_RE.search(p)]
    names = _git("diff", "--cached", "--name-only").split()
    entries = [(p, _show("HEAD", p), _show("", p))
               for p in names if CORPUS_RE.search(p)]
    seen = {p for p, _, _ in entries}
    for p in _git("ls-files", "--others", "--exclude-standard", "--", "data").split():
        if p in seen or not CORPUS_RE.search(p):
            continue
        try:
            new = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entries.append((p, "", new))
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paths", nargs="+", help="explicit files (old side = HEAD)")
    ap.add_argument("--range", help="commit range, e.g. HEAD~1..HEAD")
    ap.add_argument("--json", help="write full report here")
    ap.add_argument("--quiet-ok", action="store_true", help="print only WARN/BLOCK files")
    args = ap.parse_args()

    results = [check_file(old, new, path) for path, old, new in collect(args)]
    blocked = [r for r in results if r["status"] == "BLOCK"]
    warned = [r for r in results if r["status"] == "WARN"]

    for r in results:
        if r["status"] == "OK" and args.quiet_ok:
            continue
        print(f"{r['status']:5} {r['path']}")
        for why in r["reasons"]:
            print(f"        {why}")
    print(f"corpus_change_guard: {len(results)} changed section file(s) — "
          f"{len(results) - len(blocked) - len(warned)} OK, {len(warned)} WARN, {len(blocked)} BLOCK")
    if blocked:
        print("BLOCKED. Fix the files, or set CORPUS_GUARD_BYPASS=1 for deliberate bulk work.")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
