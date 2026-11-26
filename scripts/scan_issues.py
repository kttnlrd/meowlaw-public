#!/usr/bin/env python3
"""Scan for remaining formatting issues in section files."""
import os, re
from pathlib import Path

sections = f"{Path(__file__).resolve().parent.parent}/data/itaa-1997/sections"
results = {
    "no_heading": 0,
    "single_blob": 0,
    "inline_table_of_sections": 0,
    "inline_operative_provisions": 0,
    "inline_note_in_paragraph": 0,
    "inline_working_out": 0,
    "trailing_gt": 0,
    "missing_anchor_close": 0,
    "inline_method_statement": 0,
    "bold_note_block": 0,
    "total": 0
}
examples = {k: [] for k in results}

for root, dirs, files in os.walk(sections):
    for fname in sorted(files):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(root, fname)
        content = open(path, "r", encoding="utf-8").read()
        results["total"] += 1
        parts = content.split("---", 2)
        body = parts[2] if len(parts) > 2 else content
        lines = len(content.split("\n"))

        # Check heading
        has_h = any(l.strip().startswith("# ") for l in body.split("\n") if l.strip())
        if not has_h:
            results["no_heading"] += 1
        if lines <= 18:
            results["single_blob"] += 1
            if len(examples["single_blob"]) < 3:
                examples["single_blob"].append(fname)
        if "Table of sections" in body:
            results["inline_table_of_sections"] += 1
            if len(examples["inline_table_of_sections"]) < 3:
                examples["inline_table_of_sections"].append(fname)
        if "Operative provisions" in body:
            results["inline_operative_provisions"] += 1
            if len(examples["inline_operative_provisions"]) < 3:
                examples["inline_operative_provisions"].append(fname)
        if re.search(r'\.\s*Note:', body):
            results["inline_note_in_paragraph"] += 1
        if re.search(r'\([^)]*\)\. Working out', body):
            results["inline_working_out"] += 1
            if len(examples["inline_working_out"]) < 3:
                examples["inline_working_out"].append(fname)
        if re.search(r'\s+>\s*$', body, re.MULTILINE):
            results["trailing_gt"] += 1
        if "Method statement" in body:
            results["inline_method_statement"] += 1
        if "**Note:**" in body:
            results["bold_note_block"] += 1

print(f"=== Issues across {results['total']} files ===")
for k, v in sorted(results.items()):
    if k == "total": continue
    if v > 0:
        print(f"  {k}: {v}")
        if examples.get(k):
            for ex in examples[k]:
                print(f"    e.g. {ex}")