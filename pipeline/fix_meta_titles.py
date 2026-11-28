#!/usr/bin/env python3
"""Fix meta.json files for the newly exported rulings by extracting proper titles."""

import os
import json
import re
from pathlib import Path

RULING_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "rulings"))

# Generic titles that should be replaced with better descriptions
GENERIC_TITLES = [
    "Goods and Services Tax Ruling",
    "Taxpayer Alerts",
    "Miscellaneous Taxation Ruling",
    "Superannuation Guarantee Ruling",
    "Contents",
]

def extract_better_title(content: str) -> str | None:
    """Extract a descriptive title from the ATO ruling content."""
    lines = content.splitlines()
    
    # Method 1: Find the second citation mention (usually "GSTR 2000/13" alone)
    # and take the next meaningful line
    citation_matches = []
    for i, ln in enumerate(lines):
        ln_stripped = ln.strip()
        # Match "GSTR 2000/13" or "TA 2002/1" or "MT 2000/1" etc
        # accounting for possible trailing "W" for withdrawn
        m = re.match(r'^([A-Z]+)\s+(\d{4})/(\d+)([Ww])?\s*$', ln_stripped)
        if m:
            citation_matches.append(i)
    
    # Use the second-to-last citation match (the one in the main content area)
    # Actually, use the first citation match that doesn't have "| Legal database"
    citation_line = None
    for i, ln in enumerate(lines):
        ln_stripped = ln.strip()
        if re.match(r'^[A-Z]+\s+\d{4}/\d+[Ww]?\s*$', ln_stripped):
            citation_line = i
            break
    
    if citation_line is not None:
        for j in range(citation_line + 1, min(citation_line + 8, len(lines))):
            next_ln = lines[j].strip()
            if next_ln and not any(next_ln.startswith(s) for s in 
                ["Please", "PDF", "Legal database", "Contents", "Download", 
                 "Email", "Print", "Back to browse", "related documents",
                 "FOI", "Taxpayer Alerts"]):
                return next_ln
    
    return None


def fix_meta(filepath: Path):
    """Fix the meta.json for a single ruling file."""
    meta_path = filepath.with_suffix(filepath.suffix + ".meta.json")
    if not meta_path.exists():
        return False
    
    content = filepath.read_text(encoding="utf-8")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    
    current_title = meta.get("title", "")
    if current_title in GENERIC_TITLES:
        better_title = extract_better_title(content)
        if better_title:
            meta["title"] = better_title
            meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
            print(f"  FIXED: {filepath.name} -> '{better_title[:60]}'")
            return True
    
    return False


def main():
    print("Fixing meta.json titles...\n")
    
    types = ["GSTR", "SGR", "MT", "TA"]
    total_fixed = 0
    
    for rtype in types:
        count = 0
        for f in sorted(RULING_DIR.glob(f"{rtype}_*.txt")):
            if f.name.endswith(".meta.json"):
                continue
            if fix_meta(f):
                count += 1
                total_fixed += 1
        print(f"  {rtype}: {count} fixed")
    
    print(f"\nTotal fixed: {total_fixed}")

if __name__ == "__main__":
    main()
