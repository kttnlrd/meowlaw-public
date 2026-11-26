#!/usr/bin/env python3.12
"""E-a producer rule: a chapter title is never derived from a file name.

The defect (plan E-a): the archive ingest took each MTG chapter title from the PDF's FILE
NAME, and 22 zip entry names carried the UTF-8 flag the unzip step ignored and decoded as
CP866 - so "Dividends • Imputation System" arrived as "Dividends тАв Imputation System" and
was served as a chapter title, a tree part title and a section_index chapter_title.

The planted positives here are the two file-name shapes the ingest produced; the planted
negatives are the legitimate titles the rule must keep.  Run:

    /usr/bin/python3.12 scripts/test_e_a_chapter_title_rule.py      (exit 1 on any failure)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_cch_explorer",
                                             ROOT / "pipeline" / "build_cch_explorer.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


# (chapter record, expected title, why)
CASES = [
    # the mojibake the archive served: the title is the file name's tail with the CP866
    # signature - must NOT be served as a title
    ({"number": "04", "title": "Dividends тАв Imputation System",
      "source_file": "Ch 04 - Dividends тАв Imputation System.pdf"},
     "Chapter 04", "CP866 signature in the title is refused, not served"),
    # the whole file name used as the title
    ({"number": "06", "title": "Ch 06 - Trustees • Beneficiaries • Deceased Estates.pdf",
      "source_file": "Ch 06 - Trustees • Beneficiaries • Deceased Estates.pdf"},
     "Chapter 06", "a .pdf file name is refused"),
    # the file-name shape without the extension
    ({"number": "09", "title": "Ch 09 - Tax Accounting • Trading Stock",
      "source_file": "Ch 09 - Tax Accounting • Trading Stock.pdf"},
     "Chapter 09", "'Ch NN - X' is the file-name shape, refused"),
    # the repaired, legitimate title - kept
    ({"number": "04", "title": "Dividends • Imputation System",
      "source_file": "Ch 04 - Dividends • Imputation System.pdf"},
     "Dividends • Imputation System", "a clean upstream title is kept"),
    # a macron (Latin, OECD/Māori) is NOT a decoding signature
    ({"number": "07", "title": "Tāwhirimātea and the Māori authority",
      "source_file": "Ch 07 - Tawhiri.pdf"},
     "Tāwhirimātea and the Māori authority", "Latin macrons are legitimate"),
    # the PDF's own first-page heading wins over the upstream title
    ({"number": "11", "title": "Ch 11 - CGT.pdf", "source_file": "Ch 11 - CGT.pdf",
      "first_page_heading": "Capital Gains Tax • General Topics"},
     "Capital Gains Tax • General Topics", "first_page_heading is preferred"),
    # ligatures are normalised on the way in (E-c shares this funnel)
    ({"number": "12", "title": "Super’s ﬁnancial beneﬁts",
      "source_file": "Ch 12 - Super.pdf"},
     "Super's financial benefits", "curly quote + ligatures normalised in the title"),
]

for ch, want, why in CASES:
    got = B.chapter_title(ch)
    check(got == want, f"{why}: {got!r} (want {want!r})")

# a refused title must never come back through the file-name path: the fallback is the
# chapter label, and it carries no file name, no .pdf and no non-Latin script
bad = B.chapter_title({"number": "16", "title": "Ch 16 - Deductions тАв Gifts",
                       "source_file": "Ch 16 - Deductions тАв Gifts.pdf"})
check(bad == "Chapter 16" and "тАв" not in bad and ".pdf" not in bad,
      f"the refused case yields a label, not the defect: {bad!r}")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
