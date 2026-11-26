#!/usr/bin/env python3.12
r"""C14 must not report a formula that kept its printed operator, and must still report one that lost it.

CDN-0202.  C14_formula_structure means "every term present, nothing relating them - the formula a
reader cannot reconstruct".  Its operator set was + − × ÷ =, which omits the en dash these formulas
actually print for subtraction, so 15 of its 80 findings were fences whose en dash is right there in
the text (PDF-verified on the source pages the fences name: 40-75 vol02 p49 "365 – 25", 104-95 vol03
p228 "$5,000 – $4,500").  A finding that reports a present operator as an absent one is noise in a
worklist that is worked by hand, page by page.

The correction cannot blind the check: '*' and '-' are NOT promoted to operators, because in ITAA
formulas '*' is the printed defined-term marker (*All Groups, *exempt income) and '-' is an
intra-word hyphen ("Write-off days in income year") - 40 and 23 of the 80 findings respectively.
Both directions are asserted here, and scripts/final_data_audit.py phase 4 runs this file every
night, so a "fix" that merely mutes the class fails the audit instead of passing quietly.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_corpus_error_classes as S  # noqa: E402

# Real fence shapes, verbatim from the corpus (the 40-75 / 54-40 / 104-95 / 40-545 findings).
CASES = {
    # verbatim 40-75 line 34: subtraction printed as an en dash -> structure IS present
    "en_dash_operator": [
        "```ingest-formula source=vol02.pdf page=49",
        "365 – 25             100%",
        "      $3,500                                                   $978",
        "```",
    ],
    # verbatim 104-95 line 79
    "en_dash_money": [
        "```ingest-formula source=vol03.pdf page=228",
        "$5,000  –  $4,500      $500",
        "```",
    ],
    # verbatim 54-40 line 54: drawn x survived as the multiplication sign
    "multiplication_sign": [
        "```ingest-formula source=vol02.pdf p384",
        "      *Franking credit    ×",
        "      on the distribution     Amount of the distribution",
        "```",
    ],
    # verbatim 40-75 line 22: the real damage - terms only, structure drawn and gone
    "structure_lost": [
        "```ingest-formula source=vol02.pdf page=49",
        "Days held                100%",
        "      Asset's cost",
        "                              365            Asset's effective life",
        "```",
    ],
    # verbatim 40-545 line 23: hyphen inside a word must not count as a minus
    "hyphen_in_word": [
        "```ingest-formula source=vol02.pdf page=136",
        "Establishment          Write-off days in income year",
        "                                                             Write-off rate",
        "   expenditure                       365",
        "```",
    ],
    # verbatim 54-40 line 76: defined-term asterisks are not multiplication
    "asterisk_defined_term": [
        "```ingest-formula source=vol02.pdf page=384",
        "Indexation factor      Minimum monthly level of support",
        "                       for the pre-*quarter",
        "```",
    ],
    # a fraction rule IS the structure: reported as C14_fraction_rule, never as the main class
    "fraction_rule": [
        "```ingest-formula source=vol02.pdf page=175",
        "      $3,500",
        "   ______________",
        "          365",
        "```",
    ],
}

failures = 0
with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    (data / "itaa-1997" / "sections").mkdir(parents=True)
    for name, lines in CASES.items():
        (data / "itaa-1997" / "sections" / f"{name}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    S.findings.clear()
    S.scan_lost_formula_structure(root=data)

    by_class: dict[str, set[str]] = {}
    for f in S.findings:
        by_class.setdefault(f["class"], set()).add(
            f["path"].rsplit("/", 1)[-1].removesuffix(".md"))

    def check(ok: bool, label: str):
        global failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    struct = by_class.get("C14_formula_structure", set())
    rules = by_class.get("C14_fraction_rule", set())

    for name in ("en_dash_operator", "en_dash_money", "multiplication_sign"):
        check(name not in struct,
              f"{name} -> NOT C14_formula_structure (operator present in the text)")

    for name in ("structure_lost", "hyphen_in_word", "asterisk_defined_term"):
        check(name in struct,
              f"{name} -> FLAGGED (no operator: hyphen-in-word and defined-term asterisk "
              f"are not arithmetic)")

    check(rules == {"fraction_rule"} and "fraction_rule" not in struct,
          f"fraction rule -> C14_fraction_rule only (rules={sorted(rules)}, main={sorted(struct & {'fraction_rule'})})")

    check(bool(S.findings) and all(
        not f["path"].startswith("/") and f["path"].startswith("itaa-1997/sections/")
        for f in S.findings),
        "paths are relative to the scratch root")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
