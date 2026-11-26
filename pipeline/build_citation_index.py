#!/usr/bin/env python3
"""
Build a citation index mapping legislation sections to cases and rulings that cite them.

Sources (pre-parsed project data — the old dirs this script scanned were deleted):
- rulings: data/ruling_section_index.json   { ruling_id: [ {act, section}, ... ] }  (10,410)
- cases:   data/case_section_refs.json      { case_id:   [ {act, section}, ... ] }  (1,853)

Outputs:
- data/citation_index.json  { act: { section: [ { type, citation, title, year, snippet } ] } }

The rich fields (snippet/title/year) are not in the pre-parsed sources, so entries
carry citation == title and year 0. The primary retrieval path is the embeddings
similarity index; this file is the deterministic fallback for
get_cases_for_section / get_rulings_for_section.
"""

import json
import os
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data")
OUT_PATH = DATA_DIR / "citation_index.json"

# Load known sections per act to disambiguate
KNOWN_SECTIONS: dict[str, set[str]] = {}
for act_dir in DATA_DIR.iterdir():
    if not act_dir.is_dir():
        continue
    sections_dir = act_dir / "sections"
    if not sections_dir.exists():
        continue
    section_ids = set()
    for f in sections_dir.rglob("*.md"):
        section_ids.add(f.stem)
    if section_ids:
        KNOWN_SECTIONS[act_dir.name] = section_ids

print("Known sections per act:")
for act, secs in KNOWN_SECTIONS.items():
    print(f"  {act}: {len(secs)} sections")


def load_index(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        print(f"  !! missing source: {path}")
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def section_key(section: str) -> str:
    """Normalise to section level: '27A(1)' -> '27A', '8-1(2)(a)' -> '8-1'."""
    i = section.find("(")
    return section[:i] if i > 0 else section


def add_entry(index: dict[str, dict[str, list[dict]]], act: str, section: str, entry: dict) -> None:
    """Add an entry, filtering to sections we actually hold (disambiguation)."""
    key = section_key(section)
    known = KNOWN_SECTIONS.get(act)
    if known is not None and key not in known:
        return
    index[act].setdefault(key, []).append(entry)


def main() -> None:
    rulings = load_index(DATA_DIR / "ruling_section_index.json")
    cases = load_index(DATA_DIR / "case_section_refs.json")

    index: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    dropped = {"ruling": 0, "case": 0}
    total = {"ruling": 0, "case": 0}

    for ruling_id, refs in rulings.items():
        for ref in refs:
            act, section = ref.get("act", ""), ref.get("section", "")
            if not act or not section:
                continue
            total["ruling"] += 1
            entry = {
                "type": "ruling",
                "citation": ruling_id,
                "title": ruling_id,
                "year": 0,
                "snippet": "",
            }
            add_entry(index, act, section, entry)
            # count drops precisely: known sections exist but section not in set
            known = KNOWN_SECTIONS.get(act)
            if known is not None and section_key(section) not in known:
                dropped["ruling"] += 1

    for case_id, refs in cases.items():
        for ref in refs:
            act, section = ref.get("act", ""), ref.get("section", "")
            if not act or not section:
                continue
            total["case"] += 1
            entry = {
                "type": "case",
                "citation": case_id,
                "title": case_id,
                "year": 0,
                "snippet": "",
            }
            add_entry(index, act, section, entry)
            known = KNOWN_SECTIONS.get(act)
            if known is not None and section_key(section) not in known:
                dropped["case"] += 1

    # defaultdict -> plain dict for JSON
    out = {act: dict(secs) for act, secs in index.items()}

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    print(f"\nWrote {OUT_PATH}")
    print(f"  rulings: {total['ruling']} refs ({dropped['ruling']} dropped — section not in known set)")
    print(f"  cases:   {total['case']} refs ({dropped['case']} dropped)")
    acts = sum(len(secs) for secs in out.values())
    print(f"  acts: {len(out)}, sections with citations: {acts}")


if __name__ == "__main__":
    main()
