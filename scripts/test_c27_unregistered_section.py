#!/usr/bin/env python3.12
"""C27 (S4) must report a section file no tree/index references, and only that.

The 23 master-tax-examples twins are two slug generations written in one regeneration, with only
one registered. The check is deliberately general (every act with a tree/index), because that is
what catches the next generation; the finding says "triage", because an orphan can also be a real
section missing from the tree - which is why nothing is deleted by the detector.

Run: /usr/bin/python3.12 scripts/test_c27_unregistered_section.py      (exit 1 on any failure)
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

TREE = {"act": "Master tax examples", "compilation_no": 1, "parts": [
    {"id": "1", "title": "Part 1", "divisions": [], "sections": [
        {"id": "1-290-twin", "title": "Personal services business",
         "path": "1-290-personal-services-business.md"}]}]}
INDEX = [{"id": "1-290-twin", "title": "1-290 - Personal services business",
          "chapter": "1", "chapter_title": "Assessable Income"}]

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    act = data / "master-tax-examples" / "sections"
    act.mkdir(parents=True)
    # registered in the tree by path and in the index by id
    (act / "1-290-personal-services-business.md").write_text("# 1-290\n\nBody.\n", encoding="utf-8")
    # the twin: same body, filename cut at 81 characters, in neither tree nor index
    (act / "1-290-personal-services-income-unrelated-clients-test-personal-services-busine.md"
     ).write_text("# 1-290\n\nBody.\n", encoding="utf-8")
    (data / "master-tax-examples" / "tree.json").write_text(json.dumps(TREE), encoding="utf-8")
    (data / "master-tax-examples" / "section_index.json").write_text(json.dumps(INDEX), encoding="utf-8")
    # an act with no tree/index at all must report a skip, not a pass and not 13 findings
    other = data / "spec" / "sections"
    other.mkdir(parents=True)
    (other / "x.md").write_text("# x\n", encoding="utf-8")

    S.findings.clear()
    S.scan_unregistered_sections(root=data)
    real = [f for f in S.findings if f["class"] == "C27_unregistered_section"]
    skipped = [f for f in S.findings if f["class"].endswith(":CONVENTION")]

    names = {Path(f["path"]).name for f in real}
    check(names == {"1-290-personal-services-income-unrelated-clients-test-personal-services-busine.md"},
          f"exactly the unregistered twin fires -> {sorted(names)}")
    check("1-290-personal-services-business.md" not in names,
          "the registered file does not fire (registered by tree path)")
    check(len(skipped) == 1 and "spec" in skipped[0]["path"],
          f"an act with no tree/index reports a skip, not a silent pass -> {[f['path'] for f in skipped]}")
    check("triage" in real[0]["detail"],
          f"the finding says triage rather than delete: {real[0]['detail'][:80]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
