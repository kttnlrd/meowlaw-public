#!/usr/bin/env python3.12
"""C18 (CDN-0173) must fire on a non-letter nz-it-2007 part and on amendment-history titles.

A consolidated Act's parts are letters A-Z. parse_nz_it.py matched the parts nested inside the
endnote copies of the amending Acts too, so parts '1', '2', '3' and '3B' (445 sections) were
written into tree.json beside the consolidated Parts A-Z and were served as law. The check also
catches an amendment-history title in a lettered part, which is the shape a future re-parse would
produce if the ancestor filter were dropped again.

Run: /usr/bin/python3.12 scripts/test_c18_nz_parts.py      (exit 1 on any failure)
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

TREE = {
    "act": "Income Tax Act 2007",
    "compilation_no": 1,
    "parts": [
        {"id": "3B", "title": "Part 3B Tax Administration Act 1994", "divisions": [], "sections": [
            {"id": "RA 1", "title": "What this Part does", "path": "part-3B/RA-1.md"}]},
        {"id": "C", "title": "Part C Income", "divisions": [], "sections": [
            {"id": "CD 1", "title": "Income", "path": "part-C/CD-1.md"},
            # a lettered part carrying an amendment-history heading: still a finding
            {"id": "CZ 25", "title": "Section 130 replaced", "path": "part-C/CZ-25.md"}]},
    ],
}

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    d = data / "nz-it-2007"
    d.mkdir(parents=True)
    (d / "tree.json").write_text(json.dumps(TREE), encoding="utf-8")

    S.findings.clear()
    S.scan_nz_amendment_parts(root=data)
    details = [f["detail"] for f in S.findings]
    paths = {f["path"] for f in S.findings}

    check(any("part '3B'" in d and "RA 1" in d for d in details),
          f"non-letter part 3B fires for each of its sections -> {[d[:60] for d in details]}")
    check(any("CZ 25" in d for d in details),
          f"an amendment-history title inside a lettered part fires -> {[d[:60] for d in details]}")
    check(not any("'CD 1'" in d for d in details),
          "the consolidated section CD 1 does not fire")
    check(len(S.findings) == 2, f"exactly the two planted defects (got {len(S.findings)})")
    check(paths == {"data/nz-it-2007/tree.json"} or all(p.endswith("nz-it-2007/tree.json") for p in paths),
          f"findings are located in the tree ({paths})")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
