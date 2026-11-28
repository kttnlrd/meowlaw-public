#!/usr/bin/env python3
"""Scan all legislation datasets for formatting bugs. Outputs a catalogue."""
import os, re, json, glob, random
from pathlib import Path

BASE = Path(f"{Path(__file__).resolve().parent.parent}/data")
OUT = Path(__file__).resolve().parent.parent

datasets = {
    "itaa-1997": BASE / "itaa-1997",
    "itaa-1936": BASE / "itaa-1936",
    "corporations-act-2001": BASE / "corporations-act-2001",
    "taa-1953": BASE / "taa-1953",
    "nz-it-2007": BASE / "nz-it-2007",
    "master-tax-guide": BASE / "master-tax-guide",
    "master-gst-guide": BASE / "master-gst-guide",
    "master-tax-examples": BASE / "master-tax-examples",
    "aml-ctf-2006": BASE / "aml-ctf-2006",
    "insolvency-keays": BASE / "insolvency-keays",
}

checks = {
    "html_tag": re.compile(r'<(?!a id=")[a-z]+[^>]*>|&[a-z]+;'),
    "dup_title_line": re.compile(r'^\d+\.\d+\t', re.MULTILINE),
    "chap_part_leak": re.compile(r'^(CHAPTER \d+|Part \d+\.\d+)\t', re.MULTILINE),
    "footer_artifact": re.compile(r'Anti-Money Laundering and Counter-Terrorism Financing Rules Instrument'),
    "nbsp": re.compile(r'&nbsp;|&#160;'),
    "smart_quotes": re.compile(r'[\u201c\u201d\u2018\u2019]'),
}

results = {}

def scan_dataset(name, dpath):
    sections_dir = dpath / "sections"
    chapters_dir = dpath / "chapters"
    
    if sections_dir.is_dir():
        files = list(sections_dir.rglob("*.md"))
    elif chapters_dir.is_dir():
        files = list(chapters_dir.rglob("*.md"))
    else:
        files = list(dpath.rglob("*.md"))
    
    total = len(files)
    print(f"  {name}: {total} files")
    
    sample_size = min(100, max(20, int(total ** 0.5) + 20))
    random.seed(42)
    sample = random.sample(files, min(sample_size, total))
    
    bugs = {
        "duplicate_h1": {"count": 0, "examples": [], "severity": "medium"},
        "html_tags": {"count": 0, "examples": [], "severity": "medium"},
        "dup_plain_title": {"count": 0, "examples": [], "severity": "medium"},
        "chap_part_leak": {"count": 0, "examples": [], "severity": "medium"},
        "footer_artifact": {"count": 0, "examples": [], "severity": "low"},
        "nbsp_entity": {"count": 0, "examples": [], "severity": "low"},
        "smart_quotes": {"count": 0, "examples": [], "severity": "low"},
        "empty_body": {"count": 0, "examples": [], "severity": "low"},
        "broken_frontmatter": {"count": 0, "examples": [], "severity": "high"},
    }
    
    for fp in sample:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except:
            continue
        
        rel = str(fp.relative_to(dpath))
        
        parts = text.split("---", 2)
        fm_valid = len(parts) >= 3
        if not fm_valid:
            bugs["broken_frontmatter"]["count"] += 1
            if len(bugs["broken_frontmatter"]["examples"]) < 5:
                bugs["broken_frontmatter"]["examples"].append(rel)
        
        body = parts[2] if fm_valid else text
        
        h1s = re.findall(r'^#\s+', body, re.MULTILINE)
        if len(h1s) > 1:
            bugs["duplicate_h1"]["count"] += 1
            if len(bugs["duplicate_h1"]["examples"]) < 5:
                bugs["duplicate_h1"]["examples"].append(rel)
        
        if checks["dup_title_line"].search(body):
            bugs["dup_plain_title"]["count"] += 1
            if len(bugs["dup_plain_title"]["examples"]) < 5:
                bugs["dup_plain_title"]["examples"].append(rel)
        
        if checks["chap_part_leak"].search(body):
            bugs["chap_part_leak"]["count"] += 1
            if len(bugs["chap_part_leak"]["examples"]) < 5:
                bugs["chap_part_leak"]["examples"].append(rel)
        
        if checks["footer_artifact"].search(body):
            bugs["footer_artifact"]["count"] += 1
            if len(bugs["footer_artifact"]["examples"]) < 5:
                bugs["footer_artifact"]["examples"].append(rel)
        
        html_matches = checks["html_tag"].findall(body)
        if html_matches:
            bugs["html_tags"]["count"] += 1
            if len(bugs["html_tags"]["examples"]) < 5:
                unique_tags = set(html_matches[:5])
                bugs["html_tags"]["examples"].append(f"{rel} -> {unique_tags}")
        
        if checks["nbsp"].search(body):
            bugs["nbsp_entity"]["count"] += 1
            if len(bugs["nbsp_entity"]["examples"]) < 5:
                bugs["nbsp_entity"]["examples"].append(rel)
        
        if checks["smart_quotes"].search(body):
            bugs["smart_quotes"]["count"] += 1
            if len(bugs["smart_quotes"]["examples"]) < 5:
                bugs["smart_quotes"]["examples"].append(rel)
        
        if fm_valid and not body.strip():
            bugs["empty_body"]["count"] += 1
            if len(bugs["empty_body"]["examples"]) < 5:
                bugs["empty_body"]["examples"].append(rel)
    
    return {"total": total, "sampled": len(sample), "bugs": bugs}

for name in sorted(datasets.keys()):
    dpath = datasets[name]
    if dpath.is_dir():
        print(f"Scanning {name}...")
        results[name] = scan_dataset(name, dpath)

# Tree.json check
print("\n=== Tree JSON Check ===")
tree_issues = {}
for name in sorted(datasets.keys()):
    dpath = datasets[name]
    tj = dpath / "tree.json"
    if tj.exists():
        try:
            data = json.loads(tj.read_text())
            parts = data.get("parts", [])
            placeholder_count = 0
            examples = []
            for p in parts:
                title = p.get("title", "")
                if re.match(r'^(Ch \d+|Part \d+)$', title):
                    placeholder_count += 1
                    if len(examples) < 5:
                        examples.append(f"{p.get('id','?')}: '{title}'")
            if placeholder_count > 0:
                tree_issues[name] = {"placeholder_titles": placeholder_count, "examples": examples}
        except Exception as e:
            print(f"  Error reading {tj}: {e}")

# Write catalogue
catalogue_lines = ["# Formatting Bug Catalogue \u2014 CDN-0097 Scan", "", f"Generated: scan of {len(results)} datasets", ""]

for name in sorted(results.keys()):
    r = results[name]
    bugs = r["bugs"]
    total_bug_count = sum(v["count"] for v in bugs.values())
    
    catalogue_lines.append(f"## {name} ({r['total']} files, sampled {r['sampled']})")
    catalogue_lines.append(f"**{total_bug_count} total issues in sample**")
    catalogue_lines.append("")
    
    for bug_type, info in bugs.items():
        if info["count"] > 0:
            pct = round(100 * info["count"] / r["sampled"])
            catalogue_lines.append(f"### {bug_type} [{info['severity']}] \u2014 {info['count']}/{r['sampled']} ({pct}%)")
            for ex in info["examples"]:
                catalogue_lines.append(f"- {ex}")
            catalogue_lines.append("")
    
    if all(v["count"] == 0 for v in bugs.values()):
        catalogue_lines.append("No issues found in sample.")
        catalogue_lines.append("")

if tree_issues:
    catalogue_lines.append("## Tree.json Issues")
    for name, info in sorted(tree_issues.items()):
        catalogue_lines.append(f"### {name}")
        catalogue_lines.append(f"{info['placeholder_titles']} parts have placeholder titles")
        for ex in info["examples"]:
            catalogue_lines.append(f"- {ex}")
        catalogue_lines.append("")

final = "\n".join(catalogue_lines)
(OUT / "BUG_CATALOGUE.md").write_text(final)
print(f"\nWritten to BUG_CATALOGUE.md ({len(final)} chars)")
print(final[:3000])
print("...")
