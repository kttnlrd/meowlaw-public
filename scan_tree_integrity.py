#!/usr/bin/env python3
"""Tree integrity scan for all primary acts — corrected for actual tree structure."""

import json
import os
import random
import sys

BASE = os.path.join(os.environ["LEGISLATION_EXPLORER_DIR"], "data")
ACTS = [
    "itaa-1997",
    "itaa-1936",
    "gst-1999",
    "taa-1953",
    "corporations-act-2001",
    "nz-it-2007",
    "aml-ctf-2006",
    "aml-ctf-rules-2007",
]

random.seed(42)


def collect_sections(node):
    """Recursively collect section nodes from any part of the tree.
    
    Structure:
      Root: {act, parts, compilation_no, compilation_date, [schedules]}
      Parts: {id, title, divisions, sections}
      Divisions: {id, title, subdivisions, sections}
      Subdivisions: {id, title, sections}
      Sections: {id, title, path}
      Schedules: {id, title, sections}  (like parts but at root level)
    """
    sections = []

    if isinstance(node, dict):
        # If this node has 'path' and 'id', it's a leaf section
        if 'path' in node and 'id' in node and 'title' in node:
            sections.append(node)
            return sections

        # Walk children containers
        for key in ('parts', 'divisions', 'subdivisions', 'sections', 'chapters', 'schedules'):
            if key in node:
                sections.extend(collect_sections(node[key]))

    elif isinstance(node, list):
        for item in node:
            sections.extend(collect_sections(item))

    return sections


def check_yaml_frontmatter(file_path):
    """Read first 5 lines of a file and check YAML frontmatter."""
    try:
        with open(file_path, "r") as f:
            lines = [f.readline() for _ in range(5)]

        has_frontmatter = lines[0].strip() == "---"
        has_title = any("title:" in l for l in lines)
        has_id = any("id:" in l for l in lines)

        return {
            "exists": True,
            "has_frontmatter": has_frontmatter,
            "has_title": has_title,
            "has_id": has_id,
            "lines": lines,
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


results = {}

for act in ACTS:
    act_dir = os.path.join(BASE, act)
    tree_path = os.path.join(act_dir, "tree.json")
    sections_dir = os.path.join(act_dir, "sections")

    print(f"\n{'='*60}")
    print(f"ACT: {act}")
    print(f"{'='*60}")

    if not os.path.exists(tree_path):
        print(f"  ERROR: tree.json not found")
        results[act] = {"error": "tree.json not found"}
        continue

    # 1. Load tree.json
    with open(tree_path, "r") as f:
        tree = json.load(f)

    # 2-3. Collect all section nodes and build paths
    section_nodes = collect_sections(tree)
    print(f"  Sections in tree: {len(section_nodes)}")

    # Build expected file paths
    # Paths in tree.json are relative to the sections/ directory
    # EXCEPT schedule sections in some acts (e.g., itaa-1936) which already
    # include the 'sections/' prefix in their path
    section_paths = set()
    section_ids = []
    for sn in section_nodes:
        rel_path = sn.get('path', '')
        if rel_path:
            if rel_path.startswith('sections/'):
                # Path already includes sections/ prefix (e.g., schedule sections in itaa-1936)
                full_path = os.path.join(act_dir, rel_path)
            else:
                full_path = os.path.join(act_dir, 'sections', rel_path)
            section_paths.add(full_path)
            section_ids.append(sn['id'])

    print(f"  Unique file paths from tree: {len(section_paths)}")

    # 4. Verify which files exist on disk
    files_found = 0
    files_missing = []
    for p in section_paths:
        if os.path.exists(p):
            files_found += 1
        else:
            files_missing.append(p)

    print(f"  Files found on disk: {files_found}")
    print(f"  Files missing: {len(files_missing)}")
    if files_missing:
        print(f"  Missing files (up to 15):")
        for m in files_missing[:15]:
            rel = m.replace(act_dir, "")
            print(f"    - {rel}")
        if len(files_missing) > 15:
            print(f"    ... and {len(files_missing) - 15} more")

    # 5. Check for extra files on disk not in tree
    extra_files = set()
    if os.path.exists(sections_dir):
        disk_files = set()
        for root, dirs, fnames in os.walk(sections_dir):
            for fn in fnames:
                if fn.endswith(".md"):
                    disk_files.add(os.path.join(root, fn))
        extra_files = disk_files - section_paths
        print(f"  Extra files on disk (not in tree): {len(extra_files)}")
        if extra_files:
            extra_list = sorted(extra_files)[:10]
            for e in extra_list:
                rel = e.replace(act_dir, "")
                print(f"    - {rel}")
            if len(extra_files) > 10:
                print(f"    ... and {len(extra_files) - 10} more")
    else:
        print(f"  sections/ directory not found")

    # 6. Sample 5 random section files for YAML frontmatter check
    existing_paths = [p for p in section_paths if os.path.exists(p)]
    sample = random.sample(existing_paths, min(5, len(existing_paths)))

    print(f"\n  YAML Frontmatter Samples (checked first 5 lines):")
    frontmatter_ok = 0
    frontmatter_fail = 0
    for sp in sample:
        rel = sp.replace(act_dir, "")
        check = check_yaml_frontmatter(sp)
        status = "OK" if (check.get("has_frontmatter") and check.get("has_title")) else "FAIL"
        if status == "OK":
            frontmatter_ok += 1
        else:
            frontmatter_fail += 1
        print(f"    [{status}] {rel}")
        if status == "FAIL":
            reasons = []
            if not check.get("has_frontmatter"):
                reasons.append("no --- delimiter")
            if not check.get("has_title"):
                reasons.append("no title:")
            if not check.get("has_id"):
                reasons.append("no id:")
            print(f"      Issues: {', '.join(reasons)}")
            if check.get("lines"):
                print(f"      Lines: {''.join(check['lines'][:3]).rstrip()}")

    results[act] = {
        "sections_in_tree": len(section_nodes),
        "files_found": files_found,
        "files_missing": len(files_missing),
        "extra_files": len(extra_files),
        "sample_ok": frontmatter_ok,
        "sample_fail": frontmatter_fail,
        "sample_size": len(sample),
    }


# Summary table
print(f"\n\n{'='*90}")
print("TREE INTEGRITY SCAN — SUMMARY")
print(f"{'='*90}")
print(f"{'Act':<30} {'Tree Sects':>10} {'Found':>8} {'Missing':>8} {'Extra':>7}  {'Fmt OK':>7} {'Fmt Fail':>9}")
print(f"{'-'*30} {'-'*10} {'-'*8} {'-'*8} {'-'*7}  {'-'*7} {'-'*9}")

total_tree = 0
total_found = 0
total_missing = 0
total_extra = 0

for act in ACTS:
    r = results.get(act, {})
    if "error" in r:
        print(f"{act:<30} ERROR: {r['error']}")
    else:
        tree_s = r['sections_in_tree']
        found = r['files_found']
        miss = r['files_missing']
        extra = r['extra_files']
        ok = r['sample_ok']
        fail = r['sample_fail']
        total_tree += tree_s
        total_found += found
        total_missing += miss
        total_extra += extra
        print(f"{act:<30} {tree_s:>10} {found:>8} {miss:>8} {extra:>7}  {ok:>7} {fail:>9}")

print(f"{'-'*30} {'-'*10} {'-'*8} {'-'*8} {'-'*7}  {'-'*7} {'-'*9}")
print(f"{'TOTAL':<30} {total_tree:>10} {total_found:>8} {total_missing:>8} {total_extra:>7}  {'':>7} {'':>9}")
print(f"{'='*90}")

# Gap details for acts with issues
print(f"\n\nGAP ANALYSIS:")
print(f"{'='*90}")
for act in ACTS:
    r = results.get(act, {})
    if "error" in r:
        continue
    gaps = []
    if r['files_missing'] > 0:
        gaps.append(f"{r['files_missing']} section files missing on disk")
    if r['extra_files'] > 0:
        gaps.append(f"{r['extra_files']} orphan files on disk not in tree")
    if r['sample_fail'] > 0:
        gaps.append(f"{r['sample_fail']}/{r['sample_size']} sample YAML frontmatter checks failed")
    if gaps:
        print(f"\n{act}:")
        for g in gaps:
            print(f"  - {g}")
    else:
        print(f"\n{act}: ✅ No issues")