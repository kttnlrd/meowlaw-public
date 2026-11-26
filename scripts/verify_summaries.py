#!/usr/bin/env python3
"""Verify formatting of generated summary JSON files.
Checks: valid JSON, required fields present, non-empty values, proper types.
Outputs a summary report."""
import json, os, sys, glob
from pathlib import Path

OUT = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries"

def verify_file(path):
    fname = os.path.basename(path)
    issues = []
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        return fname, 0, [f"Parse error: {e}"]

    # Check required fields
    required = ["facts", "held", "reasoning", "outcome"]
    for field in required:
        if field not in data:
            issues.append(f"Missing field: {field}")
        elif not isinstance(data[field], str) or not data[field].strip():
            issues.append(f"Empty field: {field}")

    # issues should be a list
    if "issues" not in data:
        issues.append("Missing field: issues")
    elif not isinstance(data["issues"], list):
        issues.append("issues is not a list")
    elif len(data["issues"]) == 0:
        issues.append("issues list is empty")

    # cases_cited should be a list
    if "cases_cited" in data and not isinstance(data["cases_cited"], list):
        issues.append("cases_cited is not a list")

    # legislation_cited should be a list
    if "legislation_cited" in data and not isinstance(data["legislation_cited"], list):
        issues.append("legislation_cited is not a list")

    # Check _meta
    if "_meta" not in data:
        issues.append("Missing _meta")
    elif not isinstance(data["_meta"], dict):
        issues.append("_meta is not an object")

    score = max(0, 10 - len(issues))
    return fname, score, issues

if __name__ == "__main__":
    files = sorted(glob.glob(os.path.join(OUT, "*.json")))
    if not files:
        print("No summary files found.")
        sys.exit(0)

    results = []
    for f in files:
        results.append(verify_file(f))

    total = len(results)
    good = sum(1 for _, s, _ in results if s >= 10)
    ok = sum(1 for _, s, _ in results if 5 <= s < 10)
    bad = sum(1 for _, s, _ in results if s < 5)

    print(f"📊 VERIFY: {total} files checked — {good} perfect, {ok} minor issues, {bad} broken")

    if bad > 0:
        print("\n❌ BROKEN:")
        for fname, score, issues in results:
            if score < 5:
                print(f"  {fname}: score={score}")
                for issue in issues[:3]:
                    print(f"    • {issue}")

    if ok > 0:
        print(f"\n⚠️  MINOR ISSUES ({ok} files):")
        for fname, score, issues in results:
            if 5 <= score < 10:
                print(f"  {fname}: {', '.join(issues[:2])}")

    # Sample a few new files for content quality
    new_files = [f for f in files if os.path.getmtime(f) > (os.path.getmtime(__file__) if os.path.exists(__file__) else 0)]
    if not new_files:
        new_files = files[-5:]  # last 5 if none new

    print(f"\n📝 SPOT CHECK (last {min(5, len(new_files))} files):")
    for f in new_files[-5:]:
        fname = os.path.basename(f)
        try:
            with open(f) as fh:
                data = json.load(fh)
            citation = data.get("citation", "?")
            facts_len = len(data.get("facts", ""))
            reasoning_len = len(data.get("reasoning", ""))
            cases_n = len(data.get("cases_cited", []))
            leg_n = len(data.get("legislation_cited", []))
            print(f"  {citation}: facts={facts_len}c, reasoning={reasoning_len}c, cases={cases_n}, leg={leg_n}")
        except:
            print(f"  {fname}: FAILED TO READ")
