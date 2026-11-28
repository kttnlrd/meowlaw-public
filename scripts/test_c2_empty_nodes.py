#!/usr/bin/env python3.12
"""C2_empty_node: a tree.json node with no sections (and no children) is damage.

Why this exists: C2 was emitted by the nightly scan with no self-test pinning it, so nothing
would have failed if the walk stopped descending (the subdivision loop is three levels deep and
the easiest of the three to lose in a refactor). An empty part, division or subdivision means
the browse tree advertises a container that opens on nothing.

Both directions are asserted: three planted empties at three depths, and a populated tree that
must stay silent - including a part whose only content is a division, which is legitimate.

Run: /usr/bin/python3.12 scripts/test_c2_empty_nodes.py      (exit 1 on any failure)
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

    # planted: an empty part, an empty division inside a populated part, and an empty
    # subdivision inside a populated division - one at each depth the walk covers
    (data / "bad-act").mkdir()
    (data / "bad-act" / "tree.json").write_text(json.dumps({"parts": [
        {"id": "1", "sections": [], "divisions": []},
        {"id": "2", "divisions": [
            {"id": "2.1", "sections": [], "subdivisions": []},
            {"id": "2.2", "sections": [{"id": "9", "title": "Real section"}],
             "subdivisions": [{"id": "2.2.1", "sections": []}]},
        ]},
    ]}), encoding="utf-8")

    # negative: a part populated only through a division, and a division populated with a section
    (data / "good-act").mkdir()
    (data / "good-act" / "tree.json").write_text(json.dumps({"parts": [
        {"id": "1", "divisions": [
            {"id": "1.1", "sections": [{"id": "1", "title": "Alpha"}]},
        ]},
        {"id": "2", "sections": [{"id": "2", "title": "Beta"}]},
    ]}), encoding="utf-8")

    S.findings.clear()
    S.scan_empty_tree_nodes(root=str(data))

    bad = [f for f in S.findings if "bad-act" in str(f["path"])]
    details = " | ".join(f["detail"] for f in bad)
    check(len(bad) == 3, f"one empty part, one empty division, one empty subdivision (got {len(bad)}: {details})")
    check("empty part" in details, f"the empty part is named: {details[:80]!r}")
    check("empty division" in details, f"the empty division is named: {details[:80]!r}")
    check("empty subdivision" in details, f"the empty subdivision is named: {details[:80]!r}")
    check(all(f["class"] == "C2_empty_node" for f in bad),
          f"every finding is the C2 class (got {sorted({f['class'] for f in bad})})")

    good = [f for f in S.findings if "good-act" in str(f["path"])]
    check(not good, f"a tree populated through divisions and sections reports nothing (got {[f['detail'] for f in good]})")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
