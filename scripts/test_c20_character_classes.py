#!/usr/bin/env python3.12
"""C20-C26: one planted positive and one planted negative per character/encoding class.

Every class here was invisible to the scanner before this (nothing matched FEFF, U+2010/2011,
FB0x, E000-F8FF, U+00AD, Cyrillic, entities or dot leaders), so each one is planted twice: the
damage must fire and the legitimate lookalike must not. The two cases the plan names explicitly
are here: "Deductions тАв Gifts" (Cyrillic mojibake, must fire) and "Tāwhirimātea" (Māori macrons,
Latin, must NOT fire - a detector that fires on macrons would condemn the whole OECD corpus).

Run: /usr/bin/python3.12 scripts/test_c20_character_classes.py      (exit 1 on any failure)
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


SECTIONS = {
    # C20 - its own ticket: MTG chapter titles decoded as CP866
    "c20_pos": "# Ch 04 - Dividends тАв Imputation System\n",
    "c20_neg_macron": "# Deductions\n\nTāwhirimātea claims a deduction for a Māori authority.\n",
    "c20_neg_plain": "# General deductions\n\nA deduction is allowed under section 8-1.\n",
    # C21 is a derived-text class: entities in section markdown are still derived output
    "c21_pos": "# R&D\n\nThe R&amp;D offset is explained in &#8217; the guide.\n",
    "c21_neg": "# R&D\n\nThe R&D offset is explained in the guide.\n",
    # C22 - ligature (MTG 18,688, Keays 3,107)
    "c22_pos": "# Financial supply\n\nA \ufb01nancial supply is not a taxable supply.\n",
    "c22_neg": "# Financial supply\n\nA financial supply is not a taxable supply.\n",
    # C23 - the nz-it-2007 BOM sitting between subsection brackets
    "c23_pos": "# YB 4\n\nFor the purposes of subsection (1)\ufeff(a), the amount is calculated.\n",
    "c23_neg": "# YB 4\n\nFor the purposes of subsection (1)(a), the amount is calculated.\n",
    # C23 nonstandard_hyphen - corps U+2011, which FTS5 treats as a separator
    "c23h_pos": "# Sub-funds\n\nA sub\u2011fund of a managed investment scheme.\n",
    "c23h_neg": "# Sub-funds\n\nA sub-fund of a managed investment scheme.\n",
    # ... and the same character in a table row is not a finding (the plan's "outside tables")
    "c23h_neg_table": "| Item | Value |\n| --- | --- |\n| sub\u2011fund | 206 |\n",
    # C23 soft_hyphen - stands in for a real hyphen ("nonemployee")
    "c23s_pos": "# CR 2023/59\n\nThe non\u00ademployee payment is deductible.\n",
    "c23s_neg": "# CR 2023/59\n\nThe non-employee payment is deductible.\n",
    # C26 - a Symbol/Wingdings glyph passed through as private-use
    "c26_pos": "# Formula\n\nThe amount is \uf0b7 the bullet the PDF drew.\n",
    "c26_neg": "# Formula\n\nThe amount is \u2022 a real bullet character.\n",
}

EXPECTED = [
    ("c20_pos", "C20_non_latin_script", True),
    ("c20_neg_macron", "C20_non_latin_script", False),
    ("c20_neg_plain", "C20_non_latin_script", False),
    ("c21_pos", "C21_html_entity", True),
    ("c21_neg", "C21_html_entity", False),
    ("c22_pos", "C22_ligature", True),
    ("c22_neg", "C22_ligature", False),
    ("c23_pos", "C23_invisible_char", True),
    ("c23_neg", "C23_invisible_char", False),
    ("c23h_pos", "C23_nonstandard_hyphen", True),
    ("c23h_neg", "C23_nonstandard_hyphen", False),
    ("c23h_neg_table", "C23_nonstandard_hyphen", False),
    ("c23s_pos", "C23_soft_hyphen", True),
    ("c23s_neg", "C23_soft_hyphen", False),
    ("c26_pos", "C26_pua_glyph", True),
    ("c26_neg", "C26_pua_glyph", False),
]

with tempfile.TemporaryDirectory() as td:
    data = Path(td)
    sec = data / "test-act" / "sections"
    sec.mkdir(parents=True)
    for name, body in SECTIONS.items():
        (sec / f"{name}.md").write_text(body, encoding="utf-8")

    # the FTS source for case summaries: a derived .json with a raw numeric entity
    (data / "rulings" / "summaries").mkdir(parents=True)
    (data / "rulings" / "summaries" / "CR_2017_74.json").write_text(
        '{"title": "Private rulings", "summary": "R&amp;D &#8217;offset"}\n', encoding="utf-8")
    # and the raw AustLII HTML, where entities are correct and must never fire
    (data / "case_texts").mkdir(parents=True)
    (data / "case_texts" / "2009_FCAFC_29.html").write_text(
        "<p>R&amp;D &#8217;offset &#8212; 1,000 &lt;b&gt;</p>\n", encoding="utf-8")

    S.findings.clear()
    S.scan_character_classes(root=data)
    by_path: dict[str, dict[str, int]] = {}
    for f in S.findings:
        by_path.setdefault(Path(f["path"]).stem, {})[f["class"]] = f.get("count", 1)

    for name, cls, expect in EXPECTED:
        hit = cls in by_path.get(name, {})
        check(hit == expect,
              f"{name:16s} {cls:24s} detected={hit} expected={expect}")

    check("CR_2017_74" in by_path and "C21_html_entity" in by_path["CR_2017_74"],
          "a raw entity in the derived ruling summary fires (the FTS source)")
    check("2009_FCAFC_29" not in by_path,
          "entities in the raw AustLII .html are not scanned (correct there, by design)")

    counts = {c: n for c, n in by_path.get("c23_pos", {}).items() if c.startswith("C23")}
    check(counts.get("C23_invisible_char") == 1,
          f"the FEFF is counted once, not once per line (got {counts})")
    check(by_path.get("c20_pos", {}).get("C20_non_latin_script") == 3,
          f"'тАв' is 3 Cyrillic characters (got {by_path.get('c20_pos', {})})")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
