#!/usr/bin/env python3
"""
Scan all sections for *terms that are NOT in the 995-1 (or s6) definition index.
For each missing term, search all sections to find where it is defined
(by looking for "TERM has the meaning" or "TERM means" patterns).

Outputs a supplemental definitions mapping.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
DEFS_PATH = DATA_DIR / "definitions.json"

# Load existing definitions
existing_defs: dict[str, dict[str, dict]] = {}
if DEFS_PATH.exists():
    existing_defs = json.loads(DEFS_PATH.read_text(encoding="utf-8"))

# Collect all terms already indexed
itaa1997_terms = set(existing_defs.get("itaa-1997", {}).get("terms", {}).keys())
itaa1936_terms = set(existing_defs.get("itaa-1936", {}).get("terms", {}).keys())

# Normalize to lowercase for lookup
itaa1997_lower = {t.lower() for t in itaa1997_terms}
itaa1936_lower = {t.lower() for t in itaa1936_terms}

# Regex to find *terms in markdown
STAR_TERM_RE = re.compile(r"\*(?!\s)([\w%][\w\s%()-]*)")

# Regex to find definitions in text
DEF_PATTERN_RE = re.compile(
    r"([A-Za-z0-9\-'%][A-Za-z0-9\-'% ]{0,80}?)\s+"
    r"(has the meaning given by|means|has the same meaning as in|includes|has the meaning affected by)",
    re.IGNORECASE,
)

ANCHOR_RE = re.compile(r'<a id="([^"]+)">')


def find_star_terms(md_path: Path) -> set[str]:
    """Find all *terms in a markdown file."""
    content = md_path.read_text(encoding="utf-8")
    terms = set()
    for m in STAR_TERM_RE.finditer(content):
        candidate = m.group(1)
        # Greedy word extraction
        words = candidate.split()
        for i in range(len(words), 0, -1):
            prefix = " ".join(words[:i])
            terms.add(prefix.lower())
    return terms


def scan_all_star_terms(act: str) -> set[str]:
    """Scan all sections in an act for *terms."""
    sections_dir = DATA_DIR / act / "sections"
    all_terms: set[str] = set()
    for md_path in sections_dir.rglob("*.md"):
        all_terms.update(find_star_terms(md_path))
    return all_terms


def find_definition_locations(act: str, missing_terms: set[str]) -> dict[str, dict]:
    """Search all sections for where missing terms are defined."""
    sections_dir = DATA_DIR / act / "sections"
    found: dict[str, dict] = {}

    # Pre-index anchors per file
    for md_path in sections_dir.rglob("*.md"):
        content = md_path.read_text(encoding="utf-8")
        # Extract section ID from frontmatter or filename
        section_id = md_path.stem  # e.g., "40-25"

        # Find all anchors
        anchors = []
        for m in ANCHOR_RE.finditer(content):
            anchors.append((m.start(), m.group(1)))

        for m in DEF_PATTERN_RE.finditer(content):
            term = m.group(1).strip()
            key = term.lower()
            if key in missing_terms and key not in found:
                # Find nearest anchor before this definition
                anchor = None
                for pos, aid in anchors:
                    if pos < m.start():
                        anchor = aid
                    else:
                        break

                found[key] = {
                    "term": term,
                    "section": section_id,
                    "anchor": anchor,
                    "predicate": m.group(2),
                    "file": str(md_path.relative_to(DATA_DIR / act / "sections")),
                }
    return found


def main():
    print("Scanning ITAA 1997...")
    itaa1997_star_terms = scan_all_star_terms("itaa-1997")
    itaa1997_missing = itaa1997_star_terms - itaa1997_lower
    print(f"  Total *terms found: {len(itaa1997_star_terms)}")
    print(f"  Already in 995-1: {len(itaa1997_lower)}")
    print(f"  Missing from 995-1: {len(itaa1997_missing)}")

    print("\nSearching for definitions of missing ITAA 1997 terms...")
    itaa1997_found = find_definition_locations("itaa-1997", itaa1997_missing)
    print(f"  Found definitions for: {len(itaa1997_found)} terms")

    # Show some examples
    print("\n  Examples:")
    for i, (k, v) in enumerate(list(itaa1997_found.items())[:20]):
        print(f"    {v['term']} -> s{v['section']} #{v['anchor']} ({v['predicate']})")

    still_missing = itaa1997_missing - set(itaa1997_found.keys())
    print(f"\n  Still missing (no definition found): {len(still_missing)}")
    if still_missing:
        print("  Sample still-missing terms:")
        for t in list(still_missing)[:20]:
            print(f"    - {t}")

    print("\nScanning ITAA 1936...")
    itaa1936_star_terms = scan_all_star_terms("itaa-1936")
    itaa1936_missing = itaa1936_star_terms - itaa1936_lower
    print(f"  Total *terms found: {len(itaa1936_star_terms)}")
    print(f"  Already in s6: {len(itaa1936_lower)}")
    print(f"  Missing from s6: {len(itaa1936_missing)}")

    print("\nSearching for definitions of missing ITAA 1936 terms...")
    itaa1936_found = find_definition_locations("itaa-1936", itaa1936_missing)
    print(f"  Found definitions for: {len(itaa1936_found)} terms")

    # Save supplemental definitions
    supplemental = {
        "itaa-1997": {
            "section": "995-1",
            "terms": {v["term"]: {"anchor": v["anchor"], "section": v["section"]} for v in itaa1997_found.values()},
        },
        "itaa-1936": {
            "section": "6",
            "terms": {v["term"]: {"anchor": v["anchor"], "section": v["section"]} for v in itaa1936_found.values()},
        },
    }

    out_path = DATA_DIR / "definitions_supplemental.json"
    out_path.write_text(json.dumps(supplemental, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved supplemental definitions to {out_path}")
    print(f"  ITAA 1997 supplemental: {len(supplemental['itaa-1997']['terms'])}")
    print(f"  ITAA 1936 supplemental: {len(supplemental['itaa-1936']['terms'])}")

    # Also print full still-missing lists for review
    with open(DATA_DIR / "missing_terms_itaa1997.txt", "w") as f:
        for t in sorted(still_missing):
            f.write(t + "\n")
    print(f"\nWrote {len(still_missing)} still-missing ITAA 1997 terms to missing_terms_itaa1997.txt")


if __name__ == "__main__":
    main()
