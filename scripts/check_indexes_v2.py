#!/usr/bin/env python3
"""Check what indexes have data for section 52-10."""
import json
from collections import defaultdict
from pathlib import Path

# Check citation index
idx = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/citation_index.json"))
itaa = idx.get('itaa-1997', {})
print(f"Citation index: {len(itaa)} sections")
# Check if 52-10 exists
if '52-10' in itaa:
    print(f"  52-10 entries: {len(itaa['52-10'])}")
    for e in itaa['52-10'][:5]:
        print(f"    {e}")
else:
    print("  52-10 NOT found")

# Check smartlink index
sli = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/smartlink_index.json"))
sections = sli.get('sections', {}).get('itaa-1997', {})
print(f"\nSmartlink index: {len(sections)} sections")
print(f"  Sample: {list(sections.keys())[:10]}")
if '52-10' in sections:
    print(f"  Has 52-10: {len(sections['52-10'])} links")
    for l in sections['52-10'][:5]:
        print(f"    {l}")
else:
    print("  52-10 NOT in smartlink sections")

# Check if ruling section index can be reversed
rsi = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/ruling_section_index.json"))
print(f"\nRuling section index: {len(rsi)} rulings")
# Build reverse map: section -> rulings
sec_to_ruling = defaultdict(list)
for citation, refs in rsi.items():
    for ref in refs:
        sec_to_ruling[f"{ref['act']}:{ref['section']}"].append(citation)
print(f"  Reversed: {len(sec_to_ruling)} sections with ruling links")
if 'itaa-1997:52-10' in sec_to_ruling:
    rulings = sec_to_ruling['itaa-1997:52-10']
    print(f"  Rulings for 52-10: {len(rulings)}")
    for r in rulings[:5]:
        print(f"    {r}")
else:
    print("  52-10 NOT in reversed ruling index")

# Check section_case_index
sci = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/section_case_index.json"))
print(f"\nSection case index: {len(sci)} entries")
# Keys are like "itaa-1936:109D" — check if itaa-1997 sections exist
itaa_cases = [k for k in sci if k.startswith('itaa-1997:')]
print(f"  itaa-1997 case links: {len(itaa_cases)}")
itaa_cases_sections = [k.split(':')[1] for k in itaa_cases]
print(f"  Sample sections: {sorted(itaa_cases_sections)[:10]}")
if 'itaa-1997:52-10' in sci:
    print(f"  Cases for 52-10: {len(sci['itaa-1997:52-10'])}")
    for c in sci['itaa-1997:52-10'][:5]:
        print(f"    {c}")
else:
    print("  itaa-1997:52-10 NOT in section_case_index")