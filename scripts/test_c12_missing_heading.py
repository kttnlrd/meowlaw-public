#!/usr/bin/env python3.12
"""C12_missing_heading: a section whose H1 is gone, decided by tree.json, not by convention.

Why this exists: C12 exists because the nightly investigator deleted an H1 from a copy of
30-315.md and no class tripped. It was then corrected twice - once because a whole-act loss
divided zero by everything and reported SKIP, and once to read each act's own tree.json as the
title authority. Neither correction had a self-test, so the next edit could reintroduce either
blindness with the audit still saying 14/14.

This pins all three behaviours:
  1. a file that lost its H1 while tree.json still titles it -> C12, by name;
  2. a file with its H1 -> nothing;
  3. a file with no H1 and no title in tree.json -> not claimed as a title loss (nothing to
     assert), so the check cannot become a false-positive factory on untitled material.

Run: /usr/bin/python3.12 scripts/test_c12_missing_heading.py      (exit 1 on any failure)
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


FM = "---\nact: \"Test Act 2001\"\nsection: \"{n}\"\n---\n\n"

with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    act = data / "test-act"
    sec = act / "sections"
    sec.mkdir(parents=True)

    # 1.md keeps its H1; 2.md lost it but tree.json still titles it; 3.md has neither
    (sec / "1.md").write_text(FM.format(n="1") + "# 1 Alpha provision\n\nBody text.\n", encoding="utf-8")
    (sec / "2.md").write_text(FM.format(n="2") + "Body text with no heading at all.\n", encoding="utf-8")
    (sec / "3.md").write_text(FM.format(n="3") + "Untitled material, no heading either.\n", encoding="utf-8")

    (act / "tree.json").write_text(json.dumps({"parts": [{"id": "1", "sections": [
        {"id": "1", "title": "Alpha provision"},
        {"id": "2", "title": "Beta provision"},
    ]}]}), encoding="utf-8")

    S.findings.clear()
    S.scan_missing_heading(check_root=str(data))

    c12 = [f for f in S.findings if f["class"] == "C12_missing_heading"]
    convention = [f for f in S.findings if f["class"] == "C12_missing_heading:CONVENTION"]
    paths = [str(f["path"]) for f in c12]

    check(any(p.endswith("2.md") for p in paths),
          f"the file whose H1 was lost while tree.json titles it is reported (got {paths})")
    check(not any(p.endswith("1.md") for p in paths),
          f"the file that kept its H1 is not reported (got {paths})")
    check(not any(p.endswith("3.md") for p in paths),
          f"an untitled file with no H1 is not claimed as a title loss (got {paths})")
    detail = next((f["detail"] for f in c12 if str(f["path"]).endswith("2.md")), "")
    check("Beta provision" in detail,
          f"the finding names the title tree.json holds: {detail[:90]!r}")
    check(not convention,
          f"a provable titled loss does not also report the convention skip (got {[f['detail'] for f in convention]})")

    # second case: an act that never adopts H1s at all must be reported as a skip, not as damage
    act2 = data / "no-h1-act"
    (act2 / "sections").mkdir(parents=True)
    for n in ("1", "2"):
        (act2 / "sections" / f"{n}.md").write_text(FM.format(n=n) + "Body only.\n", encoding="utf-8")
    (act2 / "tree.json").write_text(json.dumps({"parts": [{"id": "1", "sections": []}]}), encoding="utf-8")

    S.findings.clear()
    S.scan_missing_heading(check_root=str(data))
    skip = [f for f in S.findings if f["class"] == "C12_missing_heading:CONVENTION"]
    damage = [f for f in S.findings if f["class"] == "C12_missing_heading"
              and "no-h1-act" in str(f["path"])]
    check(len(skip) == 1 and "no-h1-act" in str(skip[0]["path"]),
          f"an act with no established convention is reported as a skip (got {[(f['class'], f['path']) for f in S.findings]})")
    check(not damage,
          f"that act's files are not reported as title losses (got {[f['path'] for f in damage]})")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
