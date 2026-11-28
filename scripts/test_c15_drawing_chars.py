#!/usr/bin/env python3.12
"""C15 must fire on a drawing character inside a fence and stay silent outside one.

A detector that cannot fail is decoration, so this points the scanner at a scratch corpus and
asserts both directions.  It also proves the positive control the class was written for is a real
hit rather than a pattern that matches everything.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

CASES = {
    "inside_fence": "```ingest-formula source=vol06.pdf page=295\n  ç   Entity's average units ÷\n```\n",
    "outside_fence": "The dwelling was sold for $10,000.\n",
    "fence_clean": "```ingest-formula source=vol03.pdf page=82\n  Base amount  ×  Years of service\n```\n",
    # CDN-0211: '÷' is a legitimate drawn operator (CDN-0202), not a lost-bracket piece.
    "division_only": "```ingest-formula source=vol07.pdf page=146\n  =300%  ÷  4  =  75%\n```\n",
}

failures = 0
with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    (data / "test-act" / "sections").mkdir(parents=True)
    for name, body in CASES.items():
        (data / "test-act" / "sections" / f"{name}.md").write_text(body, encoding="utf-8")
    S.DATA = data
    S.findings.clear()
    S.scan_drawing_characters()
    got = {f["path"].rsplit("/", 1)[-1].removesuffix(".md") for f in S.findings}

    for name, expect in (("inside_fence", True), ("outside_fence", False), ("fence_clean", False), ("division_only", False)):
        hit = name in got
        ok = hit == expect
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name:15s} -> detected={hit} expected={expect}")

    # the finding must name the line, or it cannot be acted on
    detail = next((f["detail"] for f in S.findings if "inside_fence" in f["path"]), "")
    ok = "line 2" in detail and "ç" in detail
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} finding names the line and the character -> {detail[:60]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
