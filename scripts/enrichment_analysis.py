#!/usr/bin/env python3
"""Analyze enrichment opportunities in the summaries."""
import json, os, re
from collections import Counter

SUMMARY_DIR = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries"

# Check a sample of cases_cited to see how many are bare citations
bare_cited = 0
full_cited = 0
total_cited = 0

# Check legislation_cited for bare references
bare_leg = 0
full_leg = 0
total_leg = 0

sample_cases = []
sample_leg = []

files = [f for f in os.listdir(SUMMARY_DIR) if f.endswith(".json")]
for fname in files[:2000]:  # Sample 2000 files
    fpath = os.path.join(SUMMARY_DIR, fname)
    try:
        with open(fpath) as f:
            data = json.load(f)
    except:
        continue
    if data.get("error"):
        continue
    
    for c in data.get("cases_cited", []):
        total_cited += 1
        # Simple heuristic: if citation contains a full stop or comma, it has a case name
        if re.search(r'[a-z]{2,}', c) and not re.match(r'^\[', c):
            full_cited += 1
        elif re.match(r'^\[\d{4}\]', c):
            bare_cited += 1
        else:
            full_cited += 1
    
    for l in data.get("legislation_cited", []):
        total_leg += 1
        if re.match(r'^(s|section|div|part|sch|reg)\b', l, re.IGNORECASE):
            bare_leg += 1
        elif re.search(r'\(Cth\)|\(NSW\)|\(Vic\)|\(Qld\)|\(WA\)|\(SA\)|\(Tas\)|\(ACT\)|\(NT\)', l):
            full_leg += 1
        else:
            full_leg += 1

print(f"=== Cases Cited (sample: {total_cited}) ===")
print(f"Bare citations: {bare_cited} ({bare_cited/max(total_cited,1)*100:.1f}%)")
print(f"Full citations: {full_cited} ({full_cited/max(total_cited,1)*100:.1f}%)")

print(f"\n=== Legislation (sample: {total_leg}) ===")
print(f"Bare refs: {bare_leg} ({bare_leg/max(total_leg,1)*100:.1f}%)")
print(f"Full refs: {full_leg} ({full_leg/max(total_leg,1)*100:.1f}%)")

# Show some examples of each
print("\n=== Bare Citation Examples ===")
files2 = [f for f in files if f.endswith(".json")]
bc = 0
for fname in files2:
    fpath = os.path.join(SUMMARY_DIR, fname)
    try:
        with open(fpath) as f:
            data = json.load(f)
    except:
        continue
    if data.get("error"):
        continue
    for c in data.get("cases_cited", []):
        if re.match(r'^\[\d{4}\]', c) and bc < 10:
            print(f"  {c}")
            bc += 1
    if bc >= 10:
        break

# Count how many citations are in our local DB
local_db_citations = set()
import subprocess
from pathlib import Path
r = subprocess.run([
    "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
    "-d", "cadena_knowledge", "-tA",
    "-c", "SELECT citation FROM cases;"
], capture_output=True, text=True, timeout=30)
for line in r.stdout.strip().split('\n'):
    if line.strip():
        local_db_citations.add(line.strip())

print(f"\n=== Local DB Stats ===")
print(f"Total citations in DB: {len(local_db_citations)}")

# Count unique bare citations across all summaries
unique_bare = set()
for fname in files:
    fpath = os.path.join(SUMMARY_DIR, fname)
    try:
        with open(fpath) as f:
            data = json.load(f)
    except:
        continue
    if data.get("error"):
        continue
    for c in data.get("cases_cited", []):
        if re.match(r'^\[\d{4}\]', c):
            unique_bare.add(c)

in_db = unique_bare & local_db_citations
not_in_db = unique_bare - local_db_citations
print(f"Unique bare citations across ALL summaries: {len(unique_bare)}")
print(f"  Can enrich from local DB: {len(in_db)}")
print(f"  Not in local DB: {len(not_in_db)}")