#!/usr/bin/env python3
"""Fix missing article numbers in treaty cross-references.

Systematic extraction defect: cross-references like
"the principles of Article (Permanent Establishment)" lost their article
number. Restore the number from a conservative, OECD-stable title→number
map (articles 1–25 are identical across all four treaties). Ambiguous or
treaty-specific titles (26+, Limitation on Benefits, Miscellaneous) are
left untouched and reported.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREATIES = ROOT / "data" / "treaties"

# Conservative map: short title → article number, stable across all four
# treaties and the OECD Model Convention (articles 1–25 only).
TITLE_TO_NUM: dict[str, int] = {
    "personal scope": 1,
    "taxes covered": 2,
    "general definitions": 3,
    "resident": 4,
    "residence": 4,
    "permanent establishment": 5,
    "income from real property": 6,
    "business profits": 7,
    "shipping and air transport": 8,
    "associated enterprises": 9,
    "dividends": 10,
    "interest": 11,
    "royalties": 12,
    "capital gains": 13,
    "alienation of property": 13,
    "independent personal services": 14,
    "income from employment": 15,
    "dependent personal services": 15,
    "directors' fees": 16,
    "entertainers and sportspersons": 17,
    "entertainers": 17,
    "pensions": 18,
    "pensions, annuities, alimony and child support": 18,
    "government service": 19,
    "governmental remuneration": 19,
    "students": 20,
    "other income": 21,
    "elimination of double taxation": 22,
    "relief from double taxation": 22,
    "mutual agreement procedure": 23,
    "exchange of information": 24,
    "assistance in collection": 25,
}

# "Article (Title)" → "Article N (Title)" (Title may contain commas/ampersands).
_REF_RE = re.compile(r"Article\s*\(([^)]*)\)")


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().strip(".,;").lower()


def main() -> int:
    changed = 0
    unmapped: dict[str, int] = {}
    for art_file in sorted(TREATIES.glob("*/articles/*.md")):
        text = art_file.read_text(encoding="utf-8", errors="replace")

        def repl(m: re.Match[str]) -> str:
            nonlocal changed
            title = m.group(1)
            num = TITLE_TO_NUM.get(_norm(title))
            if num is None:
                unmapped[title] = unmapped.get(title, 0) + 1
                return m.group(0)
            changed += 1
            return f"Article {num} ({title})"

        new_text = _REF_RE.sub(repl, text)
        if new_text != text:
            art_file.write_text(new_text, encoding="utf-8")

    print(f"Restored {changed} article number(s).")
    if unmapped:
        print("Left unmapped (treaty-specific / ambiguous):")
        for title, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
            print(f"  {n:3d}  {title!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
