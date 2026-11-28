#!/usr/bin/env python3
"""Check smartlinks for section 52-10 in detail."""
import json
from pathlib import Path

sli = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/smartlink_index.json"))
sec = sli.get('sections', {}).get('itaa-1997', {}).get('52-10', [])

print(f"Smartlinks for 52-10: {len(sec)} total")
for s in sec:
    print(f"  {s['type']:12s} score={s.get('score','?'):5s} reason={s.get('reason','')}  id={s.get('id','')}")

# Check what cases look like in the full citation index  
idx = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/citation_index.json"))
# Find any section that has cases
for section, entries in idx.get('itaa-1997', {}).items():
    cases = [e for e in entries if e.get('type') == 'case']
    if cases:
        print(f"\nExample: section {section} has {len(cases)} cases")
        for c in cases[:2]:
            print(f"  {c}")
        break