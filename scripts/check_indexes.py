#!/usr/bin/env python3
"""Check citation and case indexes for section 52-10."""
import json
from pathlib import Path

# Citation index
idx = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/citation_index.json"))
print("Citation index top keys:", list(idx.keys())[:10])
print("Has itaa-1997:", "itaa-1997" in idx)
if "itaa-1997" in idx:
    sections = list(idx["itaa-1997"].keys())[:10]
    print("Sample sections:", sections)
    if "52-10" in idx["itaa-1997"]:
        entries = idx["itaa-1997"]["52-10"]
        print(f"52-10 entries: {len(entries)}")
        for e in entries[:5]:
            print(f"  {e}")
    else:
        print("52-10 NOT in citation_index")

# Section case index
try:
    sci = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/section_case_index.json"))
    print("\nSection case index top keys:", list(sci.keys())[:5])
except FileNotFoundError:
    print("\nNo section_case_index.json")

# Ruling section index
try:
    rsi = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/ruling_section_index.json"))
    print("\nRuling section index top keys:", list(rsi.keys())[:5])
except FileNotFoundError:
    print("\nNo ruling_section_index.json")

# Check smartlink index
try:
    sli = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/smartlink_index.json"))
    print("\nSmartlink index top keys:", list(sli.keys())[:5])
    if "sections" in sli and "itaa-1997" in sli.get("sections", {}):
        secs = list(sli["sections"]["itaa-1997"].keys())[:10]
        print("Sample smartlink sections:", secs)
except FileNotFoundError:
    print("\nNo smartlink_index.json")