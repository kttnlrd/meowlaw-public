"""Build ruling_section_index.json by scanning all ruling files for section references."""
from __future__ import annotations

import json
import re
from pathlib import Path

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from backend.services.data_loader import load_rulings, DATA_DIR

# Match section references like:
#   s 6-5, ss 6-5, 6-10, section 31G, sections 31G and 31H
#   ITAA 1997 s 8-1, ITAA 1936 s 160ZZV
#   (with optional subsections: s 6-5(1)(a))
SECTION_RE = re.compile(
    r'(?:s\.?|ss\.?|section|sections)\s+(\d+[A-Za-z]*(?:-\d+)?(?:\(\d+[A-Za-z]*\))*(?:\([a-z]\))*)',
    re.IGNORECASE
)

DIVISION_RE = re.compile(r'Division\s+(\d+[A-Z]?)', re.IGNORECASE)

ACT_MAP = {
    'itaa 1997': 'itaa-1997',
    'income tax assessment act 1997': 'itaa-1997',
    'itaa 1936': 'itaa-1936',
    'income tax assessment act 1936': 'itaa-1936',
    'gst act 1999': 'gst-1999',
    'a new tax system (goods and services tax) act 1999': 'gst-1999',
    'tax administration act 1953': 'taa-1953',
    'fringe benefits tax assessment act 1986': 'fbt-1986',
    'fbtaa 1986': 'fbt-1986',
    'superannuation industry (supervision) act 1993': 'sis-1993',
    'sis act 1993': 'sis-1993',
}


def resolve_act(line: str) -> str | None:
    """Determine which act a line refers to, or None if unknown."""
    lower = line.lower()
    for key, act in ACT_MAP.items():
        if key in lower:
            return act
    return None


def extract_sections(content: str) -> list[dict]:
    """Extract all section references from ruling text."""
    refs: set[tuple[str, str]] = set()
    lines = content.split('\n')

    # Lookback: try to find an act declaration in first 50 lines
    default_act = 'itaa-1997'
    for line in lines[:50]:
        act = resolve_act(line)
        if act:
            default_act = act
            break
    explicit_act_found = default_act != 'itaa-1997'

    for line in lines:
        act = resolve_act(line) or default_act

        for m in SECTION_RE.finditer(line):
            raw = m.group(1).strip()
            # Split on commas for "ss 6-5, 6-10"
            for part in raw.split(','):
                sec = part.strip()
                # Strip only unmatched trailing close parens
                while sec.endswith(')') and sec.count(')') > sec.count('('):
                    sec = sec[:-1]
                # Skip pure numeric values < 100 — paragraph numbers, not real
                # sections — UNLESS an act was explicitly named in this ruling,
                # because FBTAA (s 5, s 6, s 7...) and SIS (s 10, s 62...) have
                # many genuine sections below 100.
                if not explicit_act_found and re.fullmatch(r'\d{1,2}', sec):
                    continue
                if sec:
                    refs.add((act, sec))

        for m in DIVISION_RE.finditer(line):
            div = m.group(1).strip()
            if div:
                refs.add((act, f'Division {div}'))

    return [{"act": a, "section": s} for a, s in sorted(refs)]


def main():
    print("Loading rulings from data_loader...")
    rulings = load_rulings()
    print(f"Loaded {len(rulings)} rulings")

    index: dict[str, list[dict]] = {}
    for r in rulings:
        citation = r["citation"]
        path = Path(r["source"])
        if not path.exists():
            print(f"  SKIP: source missing for {citation}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
            sections = extract_sections(content)
            if sections:
                index[citation] = sections
                print(f"  {citation}: {len(sections)} refs")
            else:
                print(f"  {citation}: 0 refs")
        except Exception as e:
            print(f"  ERROR {citation}: {e}")

    output = DATA_DIR / "ruling_section_index.json"
    output.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"\nWrote {len(index)} ruling entries to {output}")
    total_refs = sum(len(v) for v in index.values())
    print(f"Total section references: {total_refs}")


if __name__ == "__main__":
    main()
