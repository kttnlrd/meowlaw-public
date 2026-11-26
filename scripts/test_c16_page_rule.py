#!/usr/bin/env python3.12
"""C16 (CDN-0203) must fire on a page rule left in prose and stay silent inside a formula fence.

A run of underscores inside an ```ingest-formula``` fence is a fraction bar - 62 of them across
52 files - so the check walks fences instead of grepping. The self-test asserts both directions
against a scratch corpus, and that the finding names the line so it can be worked by hand.

Run: /usr/bin/python3.12 scripts/test_c16_page_rule.py      (exit 1 on any failure)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

RUN = "_" * 37
CASES = {
    # the defect: a page rule splitting a sentence in prose
    "rule_in_prose": f"**(3)** For the effect of the GST, see Division 27. Note If you receive an amount {RUN}\n"
                     f"\namount may be included in your assessable income: see Subdivision 20-A.\n",
    # legitimate: the same run is a fraction bar inside a formula fence
    "rule_in_fence": f"```ingest-formula source=vol02.pdf page=175\n      $3,500\n   {RUN}\n          365\n```\n",
    # clean prose
    "clean_prose": "**(1)** A company must not acquire shares in itself.\n",
}

failures = 0
with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    (data / "test-act" / "sections").mkdir(parents=True)
    for name, body in CASES.items():
        (data / "test-act" / "sections" / f"{name}.md").write_text(body, encoding="utf-8")

    S.findings.clear()
    S.scan_page_rule_artifacts(root=data)
    got = {Path(f["path"]).stem for f in S.findings}

    for name, expect in (("rule_in_prose", True), ("rule_in_fence", False), ("clean_prose", False)):
        hit = name in got
        ok = hit == expect
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name:15s} -> detected={hit} expected={expect}")

    detail = next((f["detail"] for f in S.findings if "rule_in_prose" in f["path"]), "")
    ok = "line 1" in detail
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} finding names the line -> {detail[:70]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
