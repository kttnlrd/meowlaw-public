#!/usr/bin/env python3
"""Check citation format matching."""
import json

rulings = json.load(open(f"{Path(__file__).resolve().parent.parent}/data/ruling_manifest.json"))

# Check what format citations have
sample = rulings[:5]
for r in sample:
    print(f"citation={r['citation']:20s} citation_display={r.get('citation_display','N/A')}")

# Check for specific citations
targets = ['TD_2023_2', 'TD_2022_2', 'TD_2020_1', 'TD 2023/2', 'TD 2022/2', 'TD 2020/1']
for t in targets:
    found = [r for r in rulings if r.get('citation','') == t]
    if found:
        print(f"  FOUND: {t} → {found[0].get('citation_display','')}")
    else:
        print(f"  NOT FOUND: {t}")

# Also check the load_rulings function
from backend.services.data_loader import load_rulings
from pathlib import Path
print("\nVia load_rulings():")
lr = load_rulings()
for t in targets:
    found = [r for r in lr if r.get('citation','') == t]
    if found:
        print(f"  FOUND: {t} → {found[0].get('citation_display','')}")