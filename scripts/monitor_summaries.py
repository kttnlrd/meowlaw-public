#!/usr/bin/env python3
"""Monitor FCA/FCAFC summary generation — silent unless there's a problem."""
import json, os, re
from pathlib import Path

SUMMARIES_DIR = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries"

# Expected totals (from DB queries at start)
expected = {"FCA": 2901, "FCAFC": 522}

issues = []

for court, total in expected.items():
    # Match _FCA_ but not _FCAFC_ when looking for FCA
    pattern = re.compile(r"_" + re.escape(court) + r"_")
    court_files = [f for f in os.listdir(SUMMARIES_DIR) if f.endswith(".json") and pattern.search(f)]
    done = len(court_files)
    pct = round(done / total * 100, 1) if total > 0 else 0
    remaining = total - done
    if remaining > 0:
        issues.append(f"{court}: {done}/{total} ({pct}%) - {remaining} remaining")
    else:
        issues.append(f"{court}: {done}/{total} ✅")

# Check for error summaries (quick scan of recently modified files)
errors = 0
for fname in sorted(os.listdir(SUMMARIES_DIR), key=lambda f: os.path.getmtime(os.path.join(SUMMARIES_DIR, f)), reverse=True)[:20]:
    if not fname.endswith(".json"):
        continue
    try:
        with open(os.path.join(SUMMARIES_DIR, fname)) as f:
            data = json.load(f)
        if data.get("error"):
            errors += 1
    except:
        pass

if errors:
    issues.append(f"⚠️ {errors} error files in recent 20 checked")

if issues:
    print(" | ".join(issues))
else:
    # Silent
    pass