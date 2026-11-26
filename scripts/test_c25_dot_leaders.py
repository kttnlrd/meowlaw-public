#!/usr/bin/env python3.12
"""C25 (E-f) must fire on leaked endnote/TOC dot leaders and clear the legitimate ones.

Two populations: the artefact (an "Endnote 4 - Amendment history" block flattened into the last
section, mostly on one >2,000-character line; the OECD table of contents leaked as a spaced
". . . ." leader) and the legitimate (itAA-1997 checklists, MTG rate layouts, the GST glossary,
NZ/OECD fill-in blanks). The plan plants `child care subsidy ...... 52-150` as the must-not-fire
case, and this test plants it verbatim.

Run: /usr/bin/python3.12 scripts/test_c25_dot_leaders.py      (exit 1 on any failure)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

LONG_LINE = "Endnote 4 - Amendment history " + ("amended by item 12 ......... 4 " * 90)
assert len(LONG_LINE) > 2000

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    itaa = data / "itaa-1997" / "sections"
    itaa.mkdir(parents=True)
    gst = data / "gst-1999" / "sections"
    gst.mkdir(parents=True)
    oecd = data / "oecd-mtc-2017" / "sections"
    oecd.mkdir(parents=True)

    # positive 1: the flattened endnote block on one very long line (gst 195-1:2483's shape)
    (gst / "195-1.md").write_text("# 195-1 Dictionary\n\nSection text.\n\n" + LONG_LINE + "\n",
                                  encoding="utf-8")
    # positive 2: dot leaders under an Endnote heading in a normal-length line (sis 381 / fbt 167)
    (data / "sis-1993" / "sections").mkdir(parents=True)
    (data / "sis-1993" / "sections" / "381.md").write_text(
        "# 381 Administration\n\nSection text.\n\n## Endnotes\n\nAmendment history ......... 4\n",
        encoding="utf-8")
    # positive 3: the OECD table of contents leak
    (oecd / "c31-32.md").write_text(
        "# Chapter 31\n\nChapter 31 - Allocation of profits . . . . . 1736\n", encoding="utf-8")
    # negative: the itAA-1997 checklist the plan names, on a short line with no Endnote above it
    (itaa / "11-15.md").write_text(
        "# 11-15 Checklist\n\n| Item | Provision |\n| --- | --- |\n"
        "| child care subsidy ...... 52-150 | Subdivision 52-D |\n", encoding="utf-8")
    # negative: a long flattened index row with dot leaders (itAA-1997 12-5's shape). Length
    # alone is what the plan's first draft used, and it reported 15 legitimate rows like this.
    (itaa / "12-5.md").write_text(
        "# 12-5 List of provisions about deductions\n\n"
        "companies " + "." * 71 + " Subdivision 40-B " + "." * 90 + " 40-880\n",
        encoding="utf-8")
    # negative: ordinary prose full of periods and spaces
    (itaa / "8-1.md").write_text(
        "# 8-1 General deductions\n\nSee Division 27. Note If you receive an amount. It applies.\n",
        encoding="utf-8")

    S.findings.clear()
    S.scan_dot_leaders(root=data)
    got = {}
    for f in S.findings:
        got.setdefault(f["class"], set()).add(Path(f["path"]).stem)

    lead = got.get("C25_dot_leader_endnote", set())
    toc = got.get("C25_toc_leak", set())
    check(lead == {"195-1", "381"}, f"endnote dot leaders fire on 195-1 and 381 -> {sorted(lead)}")
    check("11-15" not in lead, "the itAA-1997 checklist dot leader does NOT fire (legitimate)")
    check("12-5" not in lead, "a long flattened index row does NOT fire (length alone is not the test)")
    check("8-1" not in lead and "8-1" not in toc, "ordinary prose does not fire")
    check(toc == {"c31-32"}, f"the spaced TOC leader fires on the OECD leak -> {sorted(toc)}")

    detail = next((f["detail"] for f in S.findings
                   if f["class"] == "C25_dot_leader_endnote" and "195-1" in f["path"]), "")
    check("character line" in detail,
          f"the long-line finding says why it fired: {detail[:80]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
