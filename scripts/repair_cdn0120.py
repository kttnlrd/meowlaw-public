#!/usr/bin/env python3
"""Repair CDN-0120: wrong-year enrichments in summary cases_cited.

enrich_citations.py matched court+number ignoring year, so old citations
like [1961] HCA 18 got stamped with the name of [2026] HCA 18 (Bendel).
This rewrites every corrupted entry:
  - canonical name available  -> {citation} — {correct name}
  - no canonical name         -> bare citation (honest, no phantom)
"""
import json, os, re, subprocess
from pathlib import Path

SUMMARY_DIR = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries"

# 1. Build canonical lookup from summary files themselves + postgres DB
canon = {}
for f in sorted(os.listdir(SUMMARY_DIR)):
    if not f.endswith(".json"):
        continue
    try:
        data = json.load(open(os.path.join(SUMMARY_DIR, f)))
    except Exception:
        continue
    cit = data.get("citation")
    title = data.get("title") or data.get("case_name")
    if cit and title:
        canon[cit] = title

r = subprocess.run([
    "docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
    "-d", "cadena_knowledge", "-tA",
    "-c", "SELECT citation, case_name FROM cases WHERE case_name IS NOT NULL AND case_name != '';"
], capture_output=True, text=True, timeout=60)
for line in r.stdout.strip().split('\n'):
    if '|' in line:
        cit, name = line.split('|', 1)
        canon[cit.strip()] = name.strip()

expanded = dict(canon)
for cit, name in list(canon.items()):
    for part in str(cit).split(';'):
        part = part.strip()
        if part:
            expanded[part] = name

# 2. Scan + repair
corrupted = 0
repaired = 0
bared = 0
for f in sorted(os.listdir(SUMMARY_DIR)):
    if not f.endswith(".json"):
        continue
    fpath = os.path.join(SUMMARY_DIR, f)
    try:
        data = json.load(open(fpath))
    except Exception:
        continue
    cited = data.get("cases_cited", [])
    if not cited:
        continue
    changed = False
    new_cited = []
    for c in cited:
        if not isinstance(c, str) or "—" not in c:
            new_cited.append(c)
            continue
        cit_part, name_part = c.split("—", 1)
        m = re.search(r"\[(\d{4})\] ([A-Z]+ \d+)", cit_part)
        m2 = re.search(r"\[(\d{4})\] ([A-Z]+ \d+)", name_part)
        # Corrupted = same court+number but different year in the name
        if m and m2 and m.group(2) == m2.group(2) and m.group(1) != m2.group(1):
            corrupted += 1
            bare = m.group(0)
            if bare in expanded:
                new_cited.append(f"{bare} — {expanded[bare]}")
                repaired += 1
            else:
                new_cited.append(bare)
                bared += 1
            changed = True
        else:
            new_cited.append(c)
    if changed:
        data["cases_cited"] = new_cited
        with open(fpath, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

print(f"corrupted entries: {corrupted}")
print(f"repaired with canonical name: {repaired}")
print(f"stripped to bare citation: {bared}")
