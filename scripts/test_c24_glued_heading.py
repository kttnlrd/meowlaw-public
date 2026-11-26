#!/usr/bin/env python3.12
"""C24 (E-d) must fire on an aml-ctf-2006 heading glued to a paragraph, and only on that shape.

The ingester joined the bold heading to the end of the preceding paragraph, so the heading reads
as body text ("...effect to this Act. **Penalties**"). A heading on its own line, a bold phrase
followed by more words, and a lower-case bold phrase are all legitimate and must not fire - the
brief's "1,148 stranded-bold in nz / 54 in aml" could not be reproduced, and this class is the
measurable substitute (176 in 89 files).

Run: /usr/bin/python3.12 scripts/test_c24_glued_heading.py      (exit 1 on any failure)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

CASES = {
    # positive: verbatim shape - a paragraph ending in a bold heading, no newline between them
    "glued": "# 252 Penalties\n\nThe offence applies despite any other provision that gives "
              "effect to this Act. **Penalties**\n\nThe penalty for an offence is 100 units.\n",
    # negative: the heading on its own line
    "own_line": "# 252 Penalties\n\nThe offence applies to a body corporate.\n\n**Penalties**\n\n"
                "The penalty is 100 units.\n",
    # negative: bold inside the sentence (more words follow it)
    "mid_sentence": "# 252 Penalties\n\nThe offence applies to a body corporate. **Penalties apply** "
                    "to the following persons.\n",
    # negative: a bold phrase that ends the line but is not a heading shape
    "lower_case": "# 252 Penalties\n\nThe offence applies to a body corporate. **penalties**\n",
}

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    sec = data / "aml-ctf-2006" / "sections" / "part-15"
    sec.mkdir(parents=True)
    for name, body in CASES.items():
        (sec / f"{name}.md").write_text(body, encoding="utf-8")
    # the same shape in another act must not fire: the check is scoped to aml
    other = data / "itaa-1997" / "sections"
    other.mkdir(parents=True)
    (other / "glued.md").write_text(CASES["glued"], encoding="utf-8")

    S.findings.clear()
    S.scan_glued_headings(root=data)
    got = {Path(f["path"]).stem for f in S.findings}

    check("glued" in got, f"a heading glued to the paragraph fires -> {sorted(got)}")
    check("own_line" not in got, "a heading on its own line does not fire")
    check("mid_sentence" not in got, "bold followed by more words does not fire")
    check("lower_case" not in got, "a lower-case bold phrase does not fire")
    check(len(S.findings) == 1, f"exactly one finding (got {len(S.findings)}) - the scope is aml-ctf-2006")

    detail = next((f["detail"] for f in S.findings), "")
    check("line 3" in detail and "Penalties" in detail,
          f"the finding names the line and the heading: {detail[:80]!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
