#!/usr/bin/env python3
"""CDN-0193 Phase 1 — post-apply verification of one rebuilt section.

The apply step proves the bytes that landed equal the reviewed bytes. That is
necessary and not sufficient: this is the gate that the *repair* is real and
that nothing else in the file moved.

Checks
  A. BLAST RADIUS — every non-'|' line is byte-identical to the same file at a
     recorded git ref, and the frontmatter block is unchanged: the rebuild may
     only touch markdown table lines.
  B. C11 RESCAN   — the live file scores 0 on the C11 detectors
     (glyph / truncated / midword), run through the detector's own helpers
     (imported, not reimplemented) with the scanner pointed at a copy of the one
     file so the result is about this file only.
  C. SERVED       — the live backend returns 200 for the section and its
     response carries the rebuilt table text (the API reads the corpus live, so
     this is the "does the site show it" check without a browser).

Usage
  python3 scripts/verify_applied_section.py --act sis-1993 --section 6
                                          [--git-ref HEAD] [--api http://localhost:8765]
Exit 0 = all checks pass.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

REPO = SCRIPTS.parent
DATA = REPO / "data"


def rel(act: str, section: str) -> str:
    hits = [p for p in (DATA / act / "sections").rglob(f"{section}.md") if p.stem == section]
    if len(hits) != 1:
        raise SystemExit(f"{act}/{section}: expected exactly 1 corpus file, found {len(hits)}")
    return str(hits[0].relative_to(REPO))


def show(ref: str, path: str) -> str | None:
    p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=REPO,
                       capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def text_lines(text: str) -> list[str]:
    return text.splitlines()


def pipe(text: str) -> list[str]:
    return [l for l in text_lines(text) if l.strip().startswith("|")]


def nonpipe(text: str) -> list[str]:
    return [l for l in text_lines(text) if not l.strip().startswith("|")]


def frontmatter(text: str) -> str:
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return m.group(0) if m else ""


def c11_counts(path: Path, act: str) -> Counter:
    """Run the C11 detectors over a one-file corpus copy."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "scan_corpus_error_classes", SCRIPTS / "scan_corpus_error_classes.py")
    if spec is None or spec.loader is None:
        raise SystemExit("cannot import the C11 detector module")
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dest = tmp / act / "sections" / "x" / path.name
        dest.parent.mkdir(parents=True)
        shutil.copy2(path, dest)
        setattr(S, "DATA", tmp)
        S.findings.clear()
        S.scan_table_coherence()
        return Counter(f["class"] for f in S.findings)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--act", required=True)
    ap.add_argument("--section", required=True)
    ap.add_argument("--git-ref", default="HEAD")
    ap.add_argument("--api", default="http://localhost:8765")
    a = ap.parse_args()

    path = REPO / rel(a.act, a.section)
    cur = path.read_text(encoding="utf-8")
    pre = show(a.git_ref, rel(a.act, a.section))
    bad = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal bad
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:34s} {extra}")

    print(f"== {a.act}/{a.section}  ({rel(a.act, a.section)})")

    # A. blast radius
    if pre is None:
        check("A1 pre-state available", False, f"git show {a.git_ref}:<path> failed")
    else:
        check("A1 prose byte-identical", nonpipe(cur) == nonpipe(pre),
              f"{len(nonpipe(cur))} non-table lines")
        check("A2 frontmatter byte-identical", frontmatter(cur) == frontmatter(pre))
        check("A3 every changed line is a table row",
              set(text_lines(cur)) ^ set(text_lines(pre)) <=
              set(pipe(cur)) | set(pipe(pre)),
              f"{len(pipe(pre))} -> {len(pipe(cur))} pipe lines")
        removed = [l for l in pipe(pre) if l not in pipe(cur)]
        check("A4 not a deletion-only edit", len(pipe(cur)) > 0 and len(removed) < len(pipe(pre)),
              f"{len(removed)} old pipe lines not carried")

    # B. C11 rescan
    counts = c11_counts(path, a.act)
    check("B1 C11 zero on rebuilt file", not counts, f"findings={dict(counts)}")

    # C. served
    try:
        with urllib.request.urlopen(f"{a.api}/api/section/{a.act}/{a.section}", timeout=30) as r:
            body = r.read().decode()
            code = r.status
        check("C1 API 200", code == 200, str(code))
        sample = [l for l in pipe(cur) if re.search(r"\S", l)][3:9]
        probe = " ".join(sample[0].strip().strip("|").split("|")[1].strip().split()[:5])
        check("C2 served text carries rebuilt rows", bool(probe) and probe in body.replace("\\n", "\n"),
              f"probe={probe[:60]!r}")
        served_pipe = [l for l in body.split("\\n") if l.strip().startswith("|")]
        dangle = [l for l in served_pipe if re.search(
            r"\s(the|and|a|an|or|of|to|for|in|on|was|is|you|if|that|which|with|by|as|at|when)\s*\|\s*$", l, re.I)]
        check("C3 served has no dangling cell ends", not dangle, f"{len(dangle)} served rows")
    except Exception as exc:  # noqa: BLE001
        check("C1 API 200", False, f"{type(exc).__name__}: {exc}")

    print("verify:", "PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
