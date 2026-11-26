#!/usr/bin/env python3.12
"""C1_compilation_no: the compilation claim on an act's tree.json must be typed and dated.

Why this exists: C1 is one of the four emitted classes that had no self-test, so a change to
scan_compilation_no() (or to the tree.json shape it reads) could have silently stopped it firing
with nothing in the nightly audit noticing. The class matters because the source-accuracy check
holds these claims against raw volumes - a claim stored as "97" instead of 97, or with no date,
is unverifiable before it ever reaches that comparison.

Both directions are asserted: two planted defects must be caught, and a well-formed tree must
stay silent. A check that reports everything catches nothing.

Run: /usr/bin/python3.12 scripts/test_c1_compilation_no.py      (exit 1 on any failure)
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


with tempfile.TemporaryDirectory() as td:
    data = Path(td)

    # planted: compilation_no stored as a string, and no compilation_date at all
    (data / "bad-act").mkdir()
    (data / "bad-act" / "tree.json").write_text(
        json.dumps({"compilation_no": "97", "parts": [{"id": "1", "sections": [{"id": "1"}]}]}),
        encoding="utf-8")

    # planted: unreadable tree.json
    (data / "broken-act").mkdir()
    (data / "broken-act" / "tree.json").write_text("{not json", encoding="utf-8")

    # negative: int claim, dated, with content
    (data / "good-act").mkdir()
    (data / "good-act" / "tree.json").write_text(json.dumps({
        "compilation_no": 97, "compilation_date": "2026-07-01",
        "parts": [{"id": "1", "sections": [{"id": "1", "title": "Alpha"}]}],
    }), encoding="utf-8")

    S.findings.clear()
    S.scan_compilation_no(root=str(data))

    by_path: dict[str, list[dict]] = {}
    for f in S.findings:
        by_path.setdefault(str(f["path"]), []).append(f)

    bad = [f for f in S.findings if "bad-act" in str(f["path"])]
    details = " | ".join(f["detail"] for f in bad)
    check(len(bad) >= 2, f"a string compilation_no and a missing date are both reported (got {len(bad)})")
    check("is str" in details, f"the type defect names the offending value: {details[:70]!r}")
    check("missing compilation_date" in details, f"the missing date is named: {details[:70]!r}")
    check(all(f["class"] == "C1_compilation_no" for f in S.findings),
          f"every finding is the C1 class (got {sorted({f['class'] for f in S.findings})})")

    broken = [f for f in S.findings if "broken-act" in str(f["path"])]
    check(len(broken) == 1 and "unreadable" in broken[0]["detail"],
          f"an unreadable tree.json is reported once as unreadable (got {[f['detail'] for f in broken]})")

    check(not by_path.get(str(data / "good-act" / "tree.json")),
          f"a well-formed tree.json reports nothing (got {by_path.get(str(data / 'good-act' / 'tree.json'))})")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
