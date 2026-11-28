#!/usr/bin/env python3.12
"""C5_fragment_inline / C5_fragment_tail: text torn by the ingest, inside a body or at its end.

Why this exists: C5 was emitted nightly with no self-test, and both of its arms are narrow
regexes - an inline repeat marker ("the the", "of the of", "and the and") and a body ending on a
bare lowercase word. Either arm can stop matching after an edit to the pattern or to the line
handling, and the audit would report a clean corpus instead of an untested detector.

Both directions are asserted for both arms: a planted inline fragment and a planted stray tail
must be caught, and bodies that legitimately end with a full stop, a colon, a bracket or a
heading must stay silent - the tail arm is the one that would otherwise fire on every section.

Run: /usr/bin/python3.12 scripts/test_c5_section_fragments.py      (exit 1 on any failure)
"""
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


PLANTS = {
    "inline.md": "The amount is worked out under the the statutory formula.\n",
    "tail.md": "The amount is worked out under the statutory formula.\nwhich\nthe\nstatutory\n",
    "clean_full_stop.md": "# 1 Alpha\n\nThe amount is worked out under the statutory formula.\n",
    "clean_colon.md": "# 2 Beta\n\nFor this situation, see:\n",
    "clean_heading.md": "# 3 Gamma\n\nThe amount is worked out as follows.\n\n#### Note\n",
}

with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    sec = data / "test-act" / "sections"
    sec.mkdir(parents=True)
    for name, body in PLANTS.items():
        (sec / name).write_text(body, encoding="utf-8")

    S.findings.clear()
    S.scan_section_fragments(root=str(data))

    by_file: dict[str, list[dict]] = {}
    for f in S.findings:
        by_file.setdefault(Path(str(f["path"])).name, []).append(f)

    inline = by_file.get("inline.md", [])
    check(len(inline) == 1 and inline[0]["class"] == "C5_fragment_inline",
          f"a doubled phrase is reported inline once (got {[(f['class'], f['detail']) for f in inline]})")
    check(bool(inline) and "the the" in inline[0]["detail"],
          f"the inline finding quotes the fragment: {inline[0]['detail'] if inline else None!r}")

    tail = by_file.get("tail.md", [])
    check(any(f["class"] == "C5_fragment_tail" for f in tail),
          f"a body ending on a bare word is reported as a tail (got {[(f['class'], f['detail']) for f in tail]})")

    for clean in ("clean_full_stop.md", "clean_colon.md", "clean_heading.md"):
        got = by_file.get(clean, [])
        check(not got, f"{clean} reports nothing (got {[(f['class'], f['detail']) for f in got]})")

    check(all(f["class"].startswith("C5_") for f in S.findings),
          f"every finding is a C5 class (got {sorted({f['class'] for f in S.findings})})")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
