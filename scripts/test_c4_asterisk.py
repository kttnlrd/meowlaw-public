#!/usr/bin/env python3.12
r"""C4 must fire on a bare line of asterisks and stay silent on the Act's own emphasis.

CDN-0199: the retired third alternative (`\*+ *[A-Za-z]+\s*\*+`, an emphasis-wrapped word)
produced 18 findings over the live corpus and all 18 were legitimate content, so the class
was reporting the Act's own emphasis subheadings and defined-term markers inside fences as
damage.  A detector that cannot fail is decoration, so this points the scanner at a scratch
corpus and asserts BOTH directions: the false positive is gone, the positive control (a line
of nothing but asterisks) still fires, and the reported line number is the asterisk line's own
(the old `^\s*` anchor let a match begin on the preceding blank line and reported one too low).
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

# The detector walks <root>/itaa-1997/sections, so the scratch act dir carries that name.
CASES = {
    # the Act's own emphasis subheading, rendered as whole-line markdown emphasis - this is
    # the exact shape all 18 live findings had (51-5.md `**Defence**`, 104-95.md `*Exception*`)
    "whole_line_emphasis": ["Preamble text.", "", "**Defence**", "", "Body continues."],
    # positive control: a line of nothing but asterisks is genuine noise in every case
    "bare_asterisks": ["Text before.", "****", "Text after."],
    # defined-term asterisk markers inside a fence are literal text, never markdown emphasis
    "fenced_defined_terms": [
        "```ingest-unclassified source=vol08.pdf page=12",
        "      *distribution *franked with    under    on which the",
        "```",
    ],
    # a single bare `*` is a stray bullet, NOT asterisk noise: C8_stray_bullet
    # (`^[ \t]*[-*][ \t]*$` in ARTIFACT_PATS) covers that shape, and C4 must not double-report it
    "lone_star": ["Some text.", "   *", "More text."],
    # regression guard for the off-by-one: the asterisk line is line 3, and the blank line
    # above it must not become the match's start
    "blank_then_stars": ["first line", "", "  ***", "tail"],
}

failures = 0
with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    (data / "itaa-1997" / "sections").mkdir(parents=True)
    for name, lines in CASES.items():
        (data / "itaa-1997" / "sections" / f"{name}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    S.findings.clear()
    S.scan_asterisk_noise(root=data)

    flagged: dict[str, list[int]] = {}
    for f in S.findings:
        assert f["class"] == "C4_asterisk_noise", f
        name = f["path"].rsplit("/", 1)[-1].removesuffix(".md")
        flagged.setdefault(name, []).append(int(f["detail"].split()[1].rstrip(":")))

    def check(ok: bool, label: str):
        global failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    emph_line = CASES["whole_line_emphasis"].index("**Defence**") + 1
    check("whole_line_emphasis" not in flagged,
          f"whole-line '**Defence**' (line {emph_line}) -> NOT C4 "
          f"(C4 lines={flagged.get('whole_line_emphasis', [])}, expected none)")

    star_line = CASES["bare_asterisks"].index("****") + 1
    check(flagged.get("bare_asterisks") == [star_line],
          f"bare line of '****' (line {star_line}) -> FLAGGED "
          f"(C4 lines={flagged.get('bare_asterisks', [])}, expected [{star_line}])")

    check("fenced_defined_terms" not in flagged,
          f"fenced defined-term markers -> NOT C4 (C4 lines={flagged.get('fenced_defined_terms', [])})")

    check("lone_star" not in flagged,
          f"lone '*' line (line {CASES['lone_star'].index('   *') + 1}) -> NOT C4 "
          f"(C4 lines={flagged.get('lone_star', [])}) - C8_stray_bullet covers that shape")

    stars_line = CASES["blank_then_stars"].index("  ***") + 1
    check(flagged.get("blank_then_stars") == [stars_line],
          f"asterisk line after a blank line reported as line {stars_line}, not {stars_line - 1} "
          f"(C4 lines={flagged.get('blank_then_stars', [])}, expected [{stars_line}])")

    # scratch-root findings must be relative to that root, never absolute
    check(bool(S.findings) and all(
        not f["path"].startswith("/") and f["path"].startswith("itaa-1997/sections/")
        for f in S.findings),
        f"paths are relative to the scratch root ({[f['path'] for f in S.findings]})")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
