#!/usr/bin/env python3.12
"""C17 (CDN-0124) must fire when a corps section body starts at an item marker, and only there.

ingest_corps_act.py's stray-heading fallback deletes the first line of a section body unless it
starts with one of a few literal prefixes; a chapeau ending "...provided by others If:" (no
trailing space after 'If') is deleted with the other ~403 real opening words, and the file then
begins at '**(a)**'. The damage is corps-only, so the check is corps-only - the same body in
itaa-1997 is an act that does not use the (a)/(b) shape after the H1.

Run: /usr/bin/python3.12 scripts/test_c17_chapeau.py      (exit 1 on any failure)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

FM = '---\nact: "Corporations Act 2001"\nsection: "{s}"\n---\n\n# {s} {t}\n\n'

CHAPEAU_LOST = FM.format(s="189", t="Reliance on information or advice provided by others") + (
    "**(a)** a director relies on information, or professional or expert advice, given or "
    "prepared by:\n\n**(i)** an employee of the corporation whom the director believes on "
    "reasonable grounds to be reliable;\n")
# the whole section as it should read: the chapeau line is present
CHAPEAU_PRESENT = FM.format(s="181", t="Good faith--civil obligations") + (
    "A director or other officer of a corporation must exercise their powers and discharge their "
    "duties:\n\n**(a)** in good faith in the best interests of the corporation; and\n")

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    corp = data / "corporations-act-2001" / "sections" / "part-2D" / "division-1"
    corp.mkdir(parents=True)
    (corp / "189.md").write_text(CHAPEAU_LOST, encoding="utf-8")
    (corp / "181.md").write_text(CHAPEAU_PRESENT, encoding="utf-8")
    itaa = data / "itaa-1997" / "sections" / "part-3-1"
    itaa.mkdir(parents=True)
    (itaa / "8-1.md").write_text(
        '---\nact: "ITAA 1997"\nsection: "8-1"\n---\n\n# 8-1 General deductions\n\n'
        "**(a)** you can deduct from your assessable income any loss or outgoing;\n",
        encoding="utf-8")

    S.findings.clear()
    S.scan_missing_chapeau(root=data)
    got = {Path(f["path"]).stem for f in S.findings}

    check("189" in got, f"a corps body starting at '**(a)**' fires -> detected={sorted(got)}")
    check("181" not in got, f"the same section WITH its chapeau does not fire -> {sorted(got)}")
    check("8-1" not in got, f"the check is corps-only (itaa-1997 same shape is not a finding) -> {sorted(got)}")

    detail = next((f["detail"] for f in S.findings if f["path"].endswith("189.md")), "")
    check("**(a)**" in detail and "chapeau" in detail, f"the finding names the shape: {detail[:70]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
