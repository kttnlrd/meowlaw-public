#!/usr/bin/env python3.12
"""S5: C11_table_glyph / C11_table_truncated / C11_table_midword are separate defects on one row.

The question S5 had to settle was whether "table_midword" and "table_truncated" are one class
reported twice - the measured answer is no (of 31 files with either, 11 carry both, 0 shared a
LINE, 20 carry only midword and 20 only truncated). What made them look like one is that the
per-line check stopped at the first match, so a row carrying several signatures was reported once.

This test plants the row that the first-match-wins order used to mask: it carries a glyph
('ç'), a trailing connector ('... r; and |') and a mid-word split ('p | rimary pr' = 'primary')
on the SAME line. All three classes must come out of ONE row, and the same class must not be
reported twice for it. Both directions are asserted - a legitimate row must stay silent - because
a check that reports everything catches nothing.

Run: /usr/bin/python3.12 scripts/test_c11_table_coherence.py      (exit 1 on any failure)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

# Verbatim shapes from the corpus: the glyph row is itaa-1936 94.md:146's pair (a formula glyph
# in a row) crossed with 121AS.md's mid-word split, and the connector end is the CDN-0193 shape.
ALL_THREE = ("| ç | p | rimary pr | oduction income for that yea | r; and |\n")
GLYPH_ONLY = "| 2 | ç In working out attributable income | Section 102AAY |\n"
CLEAN = ("| Item | For this situation: | See: |\n"
         "| --- | --- | --- |\n"
         "| 1 | In working out attributable income of a non-resident trust estate | Section 102AAY |\n"
         "| 2 | In working out the attributable income of a controlled foreign corporation | Section 397 |\n")
MIDWORD_ONLY = "| p | rimary pr | oduction income for that yea |\n"

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    (data / "test-act" / "sections").mkdir(parents=True)
    for name, body in (("all_three", ALL_THREE), ("glyph_only", GLYPH_ONLY),
                       ("clean", CLEAN), ("midword_only", MIDWORD_ONLY)):
        (data / "test-act" / "sections" / f"{name}.md").write_text(body, encoding="utf-8")

    S.findings.clear()
    for p in sorted((data / "test-act" / "sections").rglob("*.md")):
        S.findings.extend(S.table_coherence_findings(
            p.read_text(encoding="utf-8").splitlines(), str(p)))

    by_file: dict[str, list[dict]] = {}
    for f in S.findings:
        by_file.setdefault(Path(f["path"]).stem, []).append(f)
    classes = {k: {f["class"] for f in v} for k, v in by_file.items()}

    got = classes.get("all_three", set())
    for cls in ("C11_table_glyph", "C11_table_truncated", "C11_table_midword"):
        check(cls in got, f"one planted row emits {cls} -> got {sorted(got)}")
    check(len(by_file.get("all_three", [])) == 3,
          f"the row is reported exactly 3 times, once per class (got {len(by_file.get('all_three', []))})")
    check(len(by_file.get("all_three", [])) == len(got),
          "no class is reported twice for the same (file, line, class)")

    check(classes.get("glyph_only") == {"C11_table_glyph"},
          f"a glyph-only row still reports one class (got {sorted(classes.get('glyph_only', []))})")
    check(classes.get("midword_only") == {"C11_table_midword"},
          f"a mid-word-only row still reports one class (got {sorted(classes.get('midword_only', []))})")
    check(not classes.get("clean"),
          f"a legitimate table reports nothing (got {sorted(classes.get('clean', []))})")

    detail = next((f["detail"] for f in by_file.get("all_three", [])
                   if f["class"] == "C11_table_glyph"), "")
    check("line 1" in detail and "ç" in detail, f"the glyph finding names line and character: {detail[:60]!r}")
    detail = next((f["detail"] for f in by_file.get("all_three", [])
                   if f["class"] == "C11_table_midword"), "")
    check("'p|rimary'" in detail, f"the mid-word finding names the split: {detail[:70]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
