#!/usr/bin/env python3
"""One text normalisation, imported by the producers - never forked (E-c).

E-c: typographic ligatures U+FB00-U+FB06 were never NFKC-normalised, so "ﬁnancial" is a
different token from "financial" for every path that reads the raw text (MCP `get_section`,
`scripts/embed_legislation.py:270`, the insolvency FTS).  Flattening the whole corpus with
full NFKC is NOT acceptable: it would also change superscripts and fractions inside the
formulas (`pipeline/parse_itaa36.py`, the `2ⁿ` and `x₂` shapes), which the corpus serves as
text.  The mapping below therefore touches U+FB00-U+FB06 and nothing else.
"""
from __future__ import annotations

import unicodedata

LIGATURES = "\uFB00\uFB01\uFB02\uFB03\uFB04\uFB05\uFB06"


def nfkc_ligatures(text: str) -> str:
    """NFKC-normalise the ligature block only: U+FB00-06 -> their ASCII expansion.

    ff ﬁ fi ﬂ fl ﬃ ffi ﬄ ffl ﬅ/ﬆ st.  Every other character is copied byte for byte, so
    superscripts (U+2070-U+2079), fractions (U+00BD) and multiplication signs in formulas
    are untouched.
    """
    if not any(c in text for c in LIGATURES):
        return text
    return "".join(
        unicodedata.normalize("NFKC", c) if "\uFB00" <= c <= "\uFB06" else c
        for c in text
    )
