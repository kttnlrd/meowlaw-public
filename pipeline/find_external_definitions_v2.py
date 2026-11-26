#!/usr/bin/env python3
"""
Scan all sections for *terms that have NO matching prefix in 995-1/s6.
For those, search all sections for local definitions.
Build a supplemental definitions index.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
DEFS_PATH = DATA_DIR / "definitions.json"

# Load existing 995-1 / s6 definitions
existing_defs: dict[str, set[str]] = {"itaa-1997": set(), "itaa-1936": set()}
if DEFS_PATH.exists():
    data = json.loads(DEFS_PATH.read_text(encoding="utf-8"))
    existing_defs["itaa-1997"] = {t.lower() for t in data.get("itaa-1997", {}).get("terms", {}).keys()}
    existing_defs["itaa-1936"] = {t.lower() for t in data.get("itaa-1936", {}).get("terms", {}).keys()}

STAR_RE = re.compile(r"\*(?!\s)([\w%][\w\s%()-]*)")

# Definition patterns
DEF_RE = re.compile(
    r"([A-Za-z0-9\-'%][A-Za-z0-9\-'% ]{0,80}?)\s+"
    r"(has the meaning given by|means|has the same meaning as in|includes|has the meaning affected by)",
    re.IGNORECASE,
)

ANCHOR_RE = re.compile(r'<a id="([^"]+)">')


def has_definition_prefix(candidate: str, defs: set[str]) -> bool:
    """Check if any prefix of candidate is in defs."""
    words = candidate.split()
    for i in range(len(words), 0, -1):
        prefix = " ".join(words[:i]).lower()
        if prefix in defs:
            return True
    return False


def extract_all_star_expressions(act: str) -> set[str]:
    """Extract all unique full expressions after * from all sections."""
    sections_dir = DATA_DIR / act / "sections"
    expressions: set[str] = set()
    for md_path in sections_dir.rglob("*.md"):
        content = md_path.read_text(encoding="utf-8")
        for m in STAR_RE.finditer(content):
            expr = m.group(1).strip()
            # Clean up trailing punctuation / noise
            expr = re.sub(r'[;:,\.]+$', '', expr)
            if len(expr) >= 2 and len(expr) <= 100:
                expressions.add(expr)
    return expressions


def find_missing_terms(act: str, expressions: set[str]) -> set[str]:
    """Find expressions with NO matching prefix in the main definitions."""
    defs = existing_defs[act]
    missing = set()
    for expr in expressions:
        if not has_definition_prefix(expr, defs):
            missing.add(expr.lower())
    return missing


def find_definitions_for_terms(act: str, terms: set[str]) -> dict[str, dict]:
    """Search all sections for definitions of the given terms."""
    sections_dir = DATA_DIR / act / "sections"
    found: dict[str, dict] = {}

    for md_path in sections_dir.rglob("*.md"):
        content = md_path.read_text(encoding="utf-8")
        section_id = md_path.stem

        anchors = []
        for m in ANCHOR_RE.finditer(content):
            anchors.append((m.start(), m.group(1)))

        for m in DEF_RE.finditer(content):
            term = m.group(1).strip()
            key = term.lower()
            if key in terms and key not in found:
                # Find nearest anchor before this definition
                anchor = None
                for pos, aid in anchors:
                    if pos < m.start():
                        anchor = aid
                    else:
                        break

                # Heuristic: skip if term is too generic or looks like a sentence fragment
                word_count = len(term.split())
                if word_count > 12:
                    continue
                if any(t.lower() in {"the", "this", "that", "these", "those", "there", "they", "it", "if", "for", "in", "on", "at", "by", "with", "from", "as", "is", "be", "been", "being", "have", "has", "had", "will", "would", "could", "should", "may", "might", "must", "shall", "can", "do", "does", "did", "a", "an", "and", "or", "of", "to"} for t in term.split()[:1]):
                    # Only skip if the FIRST word is a common word and the term is short
                    if word_count <= 2:
                        continue

                found[key] = {
                    "term": term,
                    "section": section_id,
                    "anchor": anchor,
                    "predicate": m.group(2),
                    "file": str(md_path.relative_to(DATA_DIR / act / "sections")),
                }
    return found


def main():
    for act in ["itaa-1997", "itaa-1936"]:
        print(f"\n=== {act.upper()} ===")

        expressions = extract_all_star_expressions(act)
        print(f"Total unique *expressions: {len(expressions)}")

        missing = find_missing_terms(act, expressions)
        print(f"Expressions with NO match in main definitions: {len(missing)}")

        # Show sample missing
        print("Sample missing expressions:")
        for t in sorted(list(missing))[:30]:
            print(f"  - {t}")

        found = find_definitions_for_terms(act, missing)
        print(f"\nFound local definitions for: {len(found)} terms")

        if found:
            print("Examples:")
            for i, (k, v) in enumerate(list(found.items())[:20]):
                print(f"  {v['term']} -> s{v['section']} #{v['anchor']} ({v['predicate']})")

        # Save per-act
        out_path = DATA_DIR / f"definitions_supplemental_{act}.json"
        supplemental = {
            "section": "995-1" if act == "itaa-1997" else "6",
            "terms": {v["term"]: {"anchor": v["anchor"], "section": v["section"]} for v in found.values()},
        }
        out_path.write_text(json.dumps(supplemental, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved {len(supplemental['terms'])} supplemental definitions to {out_path}")


if __name__ == "__main__":
    main()
