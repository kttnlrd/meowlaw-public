#!/usr/bin/env python3.12
"""Reattach defined-term asterisks that the extractor stranded as their own line.

CDN-0200.  Six lines, three ITAA 1997 sections.  Cause: a printed defined-term
asterisk sits 0.75pt above the baseline of the words it belongs to, and
extract_itaa_tables_pdf.page_lines clusters on y1 with Y_JITTER = 0.5, so the
asterisk became a line of its own and the corpus rendered markdown bullets.
(Evidence per instance: PyMuPDF word boxes on the source page named by the fence
marker — the asterisk box abuts the word immediately to its right, same visual
line; plus pdftotext -layout of the same page.)

This repairs the corpus files.  It does NOT change the extractor: Y_JITTER is a
corpus-wide clustering parameter that has to be re-validated by a full re-ingest
and the gates, so that change is Harry's call, not a nightly edit.

Evidence, per reattached marker:
  54-40   vol02.pdf p384   published *All Groups / for a *quarter / same *quarter
  208-165 vol05.pdf p198   not *exempt income / on the *distribution
  727-810 vol09.pdf p146   The *disaggregated

Each reattached term is a defined term the corpus already prints with its asterisk
attached elsewhere (grep: *All Groups Consumer Price Index number in 54-30/54-60,
*quarter in 995-1, *exempt income in 995-1/207-90, *disaggregated attributable
increase in 727-800), so the placement is corroborated, not invented.

Invariant enforced: deleting every '*' and every whitespace character leaves the
file byte-identical.  Only asterisks move.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

EDITS = [
    (
        "itaa-1997/sections/part-2-15/division-54/54-40.md",
        "```ingest-formula source=vol02.pdf page=384\n"
        "                     *\n"
        "       Most recently published All Groups\n"
        "           Consumer Price Index number\n"
        "                    *\n"
        "               for a quarter\n"
        "\n"
        "     *All Groups Consumer Price Index number\n"
        "                  *\n"
        "        for the same quarter in the base year\n"
        "```",
        "```ingest-formula source=vol02.pdf page=384\n"
        "       Most recently published *All Groups\n"
        "           Consumer Price Index number\n"
        "               for a *quarter\n"
        "\n"
        "     *All Groups Consumer Price Index number\n"
        "        for the same *quarter in the base year\n"
        "```",
    ),
    (
        "itaa-1997/sections/part-3-6/division-208/208-165.md",
        "```ingest-formula source=vol05.pdf page=198\n"
        "                                Amount of the distribution\n"
        "                             *\n"
        "                                that is not exempt income\n"
        "                                     of the recipient\n"
        "      *Franking credit    ×\n"
        "                             *\n"
        "      on the distribution     Amount of the distribution\n"
        "```",
        "```ingest-formula source=vol05.pdf page=198\n"
        "                                Amount of the distribution\n"
        "                                that is not *exempt income\n"
        "                                     of the recipient\n"
        "      *Franking credit    ×\n"
        "      on the *distribution     Amount of the distribution\n"
        "```",
    ),
    (
        "itaa-1997/sections/part-3-95/division-727/727-810.md",
        "```ingest-formula source=vol09.pdf page=146\n"
        "                                        Total reductions\n"
        "    The disaggregated                 for affected interests\n"
        "                                  *\n"
        "    attributable increase             Total disaggregated\n"
        "                                      attributable decreases\n"
        "```",
        "```ingest-formula source=vol09.pdf page=146\n"
        "                                        Total reductions\n"
        "    The *disaggregated                 for affected interests\n"
        "    attributable increase             Total disaggregated\n"
        "                                      attributable decreases\n"
        "```",
    ),
]


def skeleton(text: str) -> str:
    """The file with every asterisk and whitespace character removed."""
    return "".join(c for c in text if c != "*" and not c.isspace())


LONE = re.compile(r"^[ \t]*\*[ \t]*$", re.M)


def main() -> int:
    failed = 0
    for rel, old, new in EDITS:
        p = DATA / rel
        text = p.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"FAIL {rel}: expected fence block matched {text.count(old)} times, want exactly 1")
            failed += 1
            continue
        updated = text.replace(old, new)
        if skeleton(updated) != skeleton(text):
            print(f"FAIL {rel}: content changed beyond the moved asterisks")
            failed += 1
            continue
        if updated.count("*") != text.count("*"):
            print(f"FAIL {rel}: asterisk count changed")
            failed += 1
            continue
        p.write_text(updated, encoding="utf-8")
        print(f"ok   {rel}: {old.count('*')} marker(s) reattached, "
              f"{old.count(chr(10)) - new.count(chr(10))} stranded line(s) removed")

    left = [str(md.relative_to(DATA)) for md in DATA.rglob("*.md")
            if LONE.search(md.read_text(errors="replace"))]
    print(f"lone-asterisk lines remaining corpus-wide: {len(left)} {left}")
    failed += len(left)
    print("PASS" if not failed else f"{failed} FAILURE(S)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
