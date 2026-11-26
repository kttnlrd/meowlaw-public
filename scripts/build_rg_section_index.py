#!/usr/bin/env python3
"""Build rg_section_index.json: mapping ASIC RG numbers → Corps Act sections.

Parses each RG summary's legislation_referenced field for references to
the Corporations Act 2001 (Cth) and extracts section numbers.

Output: data/rg_section_index.json
  { "RG_1": [{"act": "corporations-act-2001", "section": "912A"}, ...], ... }
"""
import json
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SUMMARIES_DIR = DATA_DIR / "regulatory-guides" / "summaries"

# Match section references in "Corporations Act 2001 (Cth)" strings
# Examples: "s 912A", "s 633(2)", "s 601GA(4)", "s340", "s 912A, 912B, 913A"
# Also: "Ch 6", "Pt 5C.6", "Div 4" — but we'll focus on section refs for now
SECTION_PAT = re.compile(
    r"\b(?:s|sec|section|ss|sections)\.?\s+"  # "s" or "section" prefix
    r"(\d{1,4}[A-Za-z]*(?:\([^)]*\))*)"  # section number like 912A or 912A(2)(b)
)

def extract_sections(text: str) -> list[str]:
    """Extract bare section numbers from a legislation reference string."""
    sections = []
    # Handle comma-separated section lists: "s 340, 341, 342"
    # and individual refs: "s 912A", "s 633(2)"
    for m in SECTION_PAT.finditer(text):
        sec = m.group(1)
        # Strip trailing parenthetical subsections for the base section number
        base = re.sub(r'\([^)]*\)', '', sec).strip()
        if base and base not in sections:
            sections.append(base)
    return sections


def main():
    index: dict[str, list[dict]] = {}
    # For reverse lookup: (act, section) → RG numbers
    reverse_index: dict[str, list[str]] = {}

    skipped_no_corps = 0
    skipped_no_refs = 0

    for f in sorted(SUMMARIES_DIR.glob("RG_*.json")):
        rg_num = f.stem  # e.g. "RG_1"
        d = json.loads(f.read_text(encoding="utf-8"))
        refs = d.get("legislation_referenced", [])

        if not refs:
            skipped_no_refs += 1
            continue

        corps_sections = []
        for ref in refs:
            if "Corporations Act 2001" in ref:
                corps_sections.extend(extract_sections(ref))

        if not corps_sections:
            skipped_no_corps += 1
            continue

        # Deduplicate while preserving order
        seen = set()
        unique_sections = []
        for s in corps_sections:
            if s not in seen:
                seen.add(s)
                unique_sections.append(s)

        entries = [
            {"act": "corporations-act-2001", "section": s}
            for s in unique_sections
        ]
        index[rg_num] = entries

        # Build reverse index
        for entry in entries:
            key = f"{entry['act']}#{entry['section']}"
            if key not in reverse_index:
                reverse_index[key] = []
            if rg_num not in reverse_index[key]:
                reverse_index[key].append(rg_num)

    # Write forward index
    out_path = DATA_DIR / "rg_section_index.json"
    out_path.write_text(
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )

    # Write reverse index  
    rev_path = DATA_DIR / "section_rg_index.json"
    rev_path.write_text(
        json.dumps(reverse_index, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Forward index: {len(index)} RGs with Corps Act section refs")
    total_sections = sum(len(v) for v in index.values())
    print(f"Total section refs: {total_sections}")
    print(f"Reverse index: {len(reverse_index)} unique (act#section) keys")

    # Show some stats
    rg_counts = [(k, len(v)) for k, v in index.items()]
    rg_counts.sort(key=lambda x: -x[1])
    print(f"\nTop 5 RGs by section refs:")
    for rg, count in rg_counts[:5]:
        print(f"  {rg}: {count} sections")

    print(f"\nRGs with no Corps refs (skipped): {skipped_no_corps}")
    print(f"RGs with no legislation refs at all: {skipped_no_refs}")


if __name__ == "__main__":
    main()
