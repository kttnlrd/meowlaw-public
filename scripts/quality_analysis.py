#!/usr/bin/env python3
"""Comprehensive quality analysis of all summary files."""
import json, os, sys
from collections import Counter
from pathlib import Path

SUMMARY_DIR = f"{Path(__file__).resolve().parent.parent}/scripts/cleaned/summaries"
files = [f for f in os.listdir(SUMMARY_DIR) if f.endswith(".json")]
print(f"Total files: {len(files)}")

stats = {
    "total": 0, "errors": 0, "empty": 0, "no_meta": 0,
    "short_facts": 0, "short_reasoning": 0,
    "zero_cases": 0, "zero_leg": 0,
    "missing_case_name": 0, "missing_court": 0,
}
error_types = Counter()
case_name_types = Counter()
spot_checks = []

for fname in sorted(files):
    fpath = os.path.join(SUMMARY_DIR, fname)
    try:
        with open(fpath) as f:
            data = json.load(f)
    except:
        stats["errors"] += 1
        error_types["parse_error"] += 1
        continue

    stats["total"] += 1

    if not data.get("_meta"):
        stats["no_meta"] += 1

    if data.get("error"):
        stats["errors"] += 1
        error_types[data["error"]] += 1
        continue

    if not data.get("case_name"):
        stats["missing_case_name"] += 1
    if not data.get("court"):
        stats["missing_court"] += 1

    facts = data.get("facts", "")
    reasoning = data.get("reasoning", "")

    if len(facts) < 200:
        stats["short_facts"] += 1
    if len(reasoning) < 400:
        stats["short_reasoning"] += 1

    cases_cited = data.get("cases_cited", [])
    leg_cited = data.get("legislation_cited", [])

    if len(cases_cited) == 0:
        stats["zero_cases"] += 1
    if len(leg_cited) == 0:
        stats["zero_leg"] += 1

    # Spot check every 100th case
    if stats["total"] % 100 == 0:
        has_name = bool(data.get("case_name"))
        court = data.get("court", "?")
        spot_checks.append({
            "citation": data.get("citation", "?"),
            "case_name": data.get("case_name", "?")[:80],
            "court": court,
            "facts_len": len(facts),
            "reasoning_len": len(reasoning),
            "cases_n": len(cases_cited),
            "leg_n": len(leg_cited),
            "issues": []
        })
        if len(facts) < 200:
            spot_checks[-1]["issues"].append(f"short_facts({len(facts)})")
        if len(reasoning) < 400:
            spot_checks[-1]["issues"].append(f"short_reasoning({len(reasoning)})")
        if not has_name:
            spot_checks[-1]["issues"].append("missing_case_name")

print(f"\n=== Quality Metrics ===")
print(f"Total good:       {stats['total'] - stats['errors']} ({((stats['total']-stats['errors'])/stats['total']*100):.1f}%)")
print(f"Errors:           {stats['errors']}")
print(f"Missing case_name: {stats['missing_case_name']}")
print(f"Missing court:    {stats['missing_court']}")
print(f"Short facts (<200): {stats['short_facts']}")
print(f"Short reasoning:  {stats['short_reasoning']}")
print(f"Zero cases cited: {stats['zero_cases']}")
print(f"Zero legislation: {stats['zero_leg']}")

if error_types:
    print(f"\n=== Error Breakdown ===")
    for etype, count in error_types.most_common():
        print(f"  {etype}: {count}")

print(f"\n=== Every 100th Case Spot Check ===")
for sc in spot_checks:
    status = "OK" if not sc["issues"] else "|".join(sc["issues"])
    print(f"  {sc['citation']}: facts={sc['facts_len']}c reasoning={sc['reasoning_len']}c cases={sc['cases_n']} leg={sc['leg_n']} [{status}]")
    if sc["case_name"] and sc["case_name"] != "?":
        print(f"    -> {sc['case_name']}")

print(f"\n=== Sample Errors ===")
err_count = 0
for fname in sorted(files):
    fpath = os.path.join(SUMMARY_DIR, fname)
    try:
        with open(fpath) as f:
            data = json.load(f)
    except:
        continue
    if data.get("error") and err_count < 10:
        print(f"  {data.get('citation','?')}: {data['error']}")
        err_count += 1
        if data.get("raw"):
            print(f"    raw: {data['raw'][:100]}...")
