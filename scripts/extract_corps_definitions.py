#!/usr/bin/env python3
"""Extract definitions from Corporations Act 2001 dictionary sections into definitions_all.json.

Corps act dictionary sections (s.9, s.761A, etc.) use this format:
  term name has the meaning given by section X.
  ANOTHER_TERM means:
  short_term (short for "Full Term Name") has the meaning...
  yet_another_term includes:

This script parses line-by-line to capture each definition.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DATA_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data")
CORPS_DIR = DATA_DIR / "corporations-act-2001"
SECTIONS_DIR = CORPS_DIR / "sections"
DEFS_FILE = DATA_DIR / "definitions_all.json"


def _normalize_term_key(key: str) -> str:
    return key.lower().replace("\u2018", "'").replace("\u2019", "'")


def extract_definitions_from_section(section_id: str, body: str) -> list[dict]:
    """Extract definition entries from corps act section body text.
    
    Uses a simple line-by-line approach: find lines that start a definition
    (beginning with the term at col-0) and capture their text until the next
    definition line or structure marker.
    """
    terms = []
    seen = set()
    
    lines = body.split("\n")
    
    # Clean Unicode artifacts
    body_clean = body.replace("\u2018", "'").replace("\u2019", "'")
    body_clean = body_clean.replace("\u201c", '"').replace("\u201d", '"')
    body_clean = body_clean.replace("*", "")
    # Normalize em-dashes, non-breaking hyphens, en-dashes to regular dashes
    body_clean = body_clean.replace("\u2014", "---").replace("\u2013", "--").replace("\u2011", "-")
    
    clean_lines = body_clean.split("\n")
    
    # First pass: find all lines that look like definition starts
    # Pattern: line starts col-0, is not > (blockquote), ( (sub-para), empty, or structural
    def_start_pattern = re.compile(
        r'^([A-Za-z0-9][A-Za-z0-9\s,/\-–—\'"()&.]*?)\s+(?:'
        r'has\s+(?:the\s+)?(?:same\s+)?meaning|'
        r'means|'
        r'includes|'
        r'\(short\s+for|'
        r'see\s'
        r')\s',
        re.IGNORECASE
    )
    
    def_start_indices = []
    for i, line in enumerate(clean_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(">") or stripped.startswith("(") or stripped.startswith("["):
            continue
        if stripped.startswith("Note:") or stripped.startswith("Example:") or stripped.startswith("In this Act:"):
            continue
        if re.match(r'^#', stripped):
            continue
        m = def_start_pattern.match(stripped)
        if m:
            term = m.group(1).strip()
            key = _normalize_term_key(term)
            if key not in seen and len(term) <= 120:
                seen.add(key)
                def_start_indices.append(i)
    
    # Second pass: extract definition text between consecutive definition starts
    for idx, start_idx in enumerate(def_start_indices):
        line = clean_lines[start_idx].strip()
        m = def_start_pattern.match(line)
        if not m:
            continue
        term = m.group(1).strip()
        key = _normalize_term_key(term)
        
        # Find end: next definition start or end of body
        if idx + 1 < len(def_start_indices):
            end_idx = def_start_indices[idx + 1]
        else:
            end_idx = len(clean_lines)
        
        # Extract definition text from start to end
        def_text_lines = clean_lines[start_idx:end_idx]
        def_text = " ".join(l.strip() for l in def_text_lines if l.strip())
        def_text = re.sub(r'<a id="[^"]*"></a>\s*', '', def_text)
        def_text = re.sub(r'>\s+', '', def_text)
        # Remove double-spaces etc.
        def_text = re.sub(r'\s+', ' ', def_text).strip()[:500]
        
        if def_text:
            terms.append({"term": term, "section": section_id, "definition": def_text})
    
    return terms


def main():
    print(f"Scanning {SECTIONS_DIR} for dictionary sections...")
    
    dict_sections = []
    all_terms = {}
    
    for md_file in sorted(SECTIONS_DIR.rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        
        fm_end = re.search(r"\n---\s*\n", text)
        if not fm_end:
            continue
        fm_text = text[3:fm_end.start()].strip()
        fm = {}
        for line in fm_text.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"')
        
        section_title = fm.get("section_title", "")
        section_id = fm.get("section", "")
        
        is_dict = any(kw in section_title.lower() for kw in ["definition", "interpretation", "dictionary", "meaning of"])
        if not is_dict:
            continue
        
        body = text[fm_end.end():]
        body = re.sub(r'^# .+\n', '', body)
        
        terms = extract_definitions_from_section(section_id, body)
        if terms:
            dict_sections.append((section_id, section_title, len(terms)))
            for t in terms:
                key = _normalize_term_key(t["term"])
                if key not in all_terms:
                    all_terms[key] = {
                        "term": t["term"],
                        "section": t["section"],
                        "definition": t["definition"],
                    }
    
    print(f"\nFound {len(dict_sections)} dictionary sections:")
    for sid, stitle, n in dict_sections:
        print(f"  s.{sid:6s} — {stitle[:50]}: {n} terms")
    print(f"Total unique terms: {len(all_terms)}")
    
    # Merge into definitions_all.json
    defs_data = json.loads(DEFS_FILE.read_text(encoding="utf-8"))
    existing = len(defs_data.get("corporations-act-2001", {}).get("terms", {}))
    
    defs_data["corporations-act-2001"] = {
        "section": "9",
        "terms": {**defs_data.get("corporations-act-2001", {}).get("terms", {}), **all_terms},
    }
    
    new_total = len(defs_data["corporations-act-2001"]["terms"])
    added = new_total - existing
    
    DEFS_FILE.write_text(json.dumps(defs_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDefinitions: {existing} → {new_total} ({added} new)")
    print(f"Saved to {DEFS_FILE}")


if __name__ == "__main__":
    main()