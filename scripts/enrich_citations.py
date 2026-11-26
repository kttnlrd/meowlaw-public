#!/usr/bin/env python3
"""Enrich bare case citations in summaries with full case names from local DB."""
import json, os, re, subprocess
from pathlib import Path

SUMMARY_DIR = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries"

# Load all case_name lookups from DB
print("Loading case names from DB...")
r = subprocess.run([
    "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
    "-d", "cadena_knowledge", "-tA",
    "-c", "SELECT citation, case_name FROM cases WHERE case_name IS NOT NULL AND case_name != '';"
], capture_output=True, text=True, timeout=60)

db_lookup = {}
for line in r.stdout.strip().split('\n'):
    if '|' in line:
        cit, name = line.split('|', 1)
        db_lookup[cit.strip()] = name.strip()

# Also handle parallel citations like [2024] FCAFC 50; (2024) 300 FCR 1
# Build a dict mapping citation -> case_name for all variants
db_lookup_expanded = dict(db_lookup)
for cit, name in list(db_lookup.items()):
    # If citation has semicolons, add each part as separate key
    if ';' in cit:
        for part in cit.split(';'):
            db_lookup_expanded[part.strip()] = name
    # Also add with/without year format
    bare = re.sub(r'^\[\d{4}\] ', '', cit)  # FCA 123 from [2024] FCA 123
    if bare != cit:
        pass  # Keep main citation as primary key

print(f"Loaded {len(db_lookup)} case names from DB ({len(db_lookup_expanded)} expanded)")

# Process all summary files
files = sorted(os.listdir(SUMMARY_DIR))
total = 0
enriched = 0
already_full = 0

for fname in files:
    if not fname.endswith(".json"):
        continue
    fpath = os.path.join(SUMMARY_DIR, fname)
    with open(fpath) as f:
        try:
            data = json.load(f)
        except:
            continue
    
    if data.get("error"):
        continue
    
    cited = data.get("cases_cited", [])
    if not cited:
        continue
    
    total += 1
    changed = False
    new_cited = []
    
    for c in cited:
        if isinstance(c, dict):
            new_cited.append(c)
            continue
        
        c = c.strip()
        
        # Check if it's already enriched (has description after citation)
        # Pattern: just a bare citation like [2024] FCA 123
        is_bare = bool(re.match(r'^\[\d{4}\]', c)) and '—' not in c
        
        if is_bare:
            # Try to find in DB
            if c in db_lookup_expanded:
                full_name = db_lookup_expanded[c]
                enriched_c = f"{c} — {full_name}"
                new_cited.append(enriched_c)
                enriched += 1
                changed = True
                continue
            
            # Try matching on court+number part — MUST also match year
            # (CDN-0120: year-blind matching stamped 2026 case names on old citations)
            m = re.match(r'^(\[\d{4}\]) (.+)$', c)
            if m:
                year = m.group(1)
                court_num = m.group(2)
                # Search for matches where court_num AND year match
                matches = []
                for db_cit, name in db_lookup.items():
                    db_year = re.match(r'^\[\d{4}\]', db_cit)
                    if db_year and db_year.group(0) != year:
                        continue  # different year — not the same case
                    db_court_num = re.sub(r'^\[\d{4}\] ', '', db_cit)
                    if db_court_num == court_num:
                        matches.append((db_cit, name))
                if matches:
                    enriched_c = f"{c} — {matches[0][1]}"
                    new_cited.append(enriched_c)
                    enriched += 1
                    changed = True
                    continue
        
        new_cited.append(c)
    
    if changed:
        data["cases_cited"] = new_cited
        with open(fpath, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

print(f"\nProcessed {total} summaries")
print(f"Enriched citations: {enriched}")
print(f"Already full: {already_full}")

# Re-run analysis to verify
print("\n=== Post-enrichment stats ===")
bare = 0
full = 0
total_cited = 0
for fname in files:
    if not fname.endswith(".json"):
        continue
    with open(os.path.join(SUMMARY_DIR, fname)) as f:
        try:
            data = json.load(f)
        except:
            continue
    if data.get("error"):
        continue
    for c in data.get("cases_cited", []):
        if isinstance(c, dict):
            continue
        total_cited += 1
        if '—' in c:
            full += 1
        elif re.match(r'^\[\d{4}\]', c):
            bare += 1
        else:
            full += 1

print(f"Total citations: {total_cited}")
print(f"Full (with case name): {full} ({full/max(total_cited,1)*100:.1f}%)")
print(f"Still bare: {bare} ({bare/max(total_cited,1)*100:.1f}%)")