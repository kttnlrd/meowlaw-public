#!/usr/bin/env python3
"""Comprehensive scan of all corps act section files for issues."""

import os, re, sys
from pathlib import Path
from collections import Counter

BASE = Path(f"{Path(__file__).resolve().parent.parent}/data/corporations-act-2001/sections")

total = 0
s_prefix_files = []
dup_heading_files = []
bad_encoding_files = []
empty_body_files = []
missing_fm_files = []
error_files = []

topline_mismatch = []  # heading doesn't match frontmatter section id

for root, dirs, files in os.walk(BASE):
    for f in files:
        if not f.endswith(".md"):
            continue
        total += 1
        path = Path(root) / f
        rel = path.relative_to(BASE)
        text = path.read_text(encoding="utf-8")

        # --- Frontmatter ---
        if not text.startswith("---"):
            missing_fm_files.append(str(rel))
            continue

        fm_end = text.find("---", 3)
        if fm_end < 0:
            error_files.append((str(rel), "frontmatter not closed"))
            continue

        fm_text = text[3:fm_end].strip()
        fm = {}
        for line in fm_text.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"')

        body_start = fm_end + 4
        body = text[body_start:]

        # --- Heading ---
        heading_match = re.search(r"^# (.+)", body, re.MULTILINE)
        if not heading_match:
            error_files.append((str(rel), "no heading"))
            continue

        heading = heading_match.group(1).strip()
        after_h = body[heading_match.end():].strip()

        # --- Check heading matches frontmatter section id ---
        expected_sec = fm.get("section", "")
        if expected_sec and not heading.startswith(expected_sec + " "):
            # Could be mismatch or alias - check
            if not heading.startswith(expected_sec):
                topline_mismatch.append((str(rel), expected_sec, heading))

        # --- s. prefix artifact at body start ---
        body_first_line = after_h.split("\n")[0].strip() if after_h else ""
        if body_first_line.startswith("s.") and re.match(r"^s\.\d+", body_first_line):
            s_prefix_files.append((str(rel), body_first_line[:80]))

        # --- Heading duplication in body ---
        title_part = heading.split(" ", 1)
        if len(title_part) > 1 and len(title_part[1]) > 10:
            check = after_h[:80]
            # Only flag if the body starts EXACTLY with the FULL heading title
            # (not a 30-char prefix — body text often naturally repeats the
            # first noun phrase of the title, e.g. "Foreign passport fund products...")
            if check.startswith(title_part[1]):
                # Additional check: the heading title shouldn't appear right at the start
                # unless it's a short definition section
                if not check.startswith("("):
                    dup_heading_files.append((str(rel), check[:80]))

        # --- Encoding artifacts ---
        bad = set()
        for c in after_h:
            if ord(c) > 127:
                # Known OK characters for legislation text
                if ord(c) in {0x2014, 0x2013, 0x2019, 0x2018, 0x201c, 0x201d,
                              0x2022, 0x2026, 0x2122, 0x00b0, 0x00a7, 0x2011,
                              0x00ae, 0x00b7, 0x00a9, 0x2020, 0x2030, 0x0153}:
                    continue
                bad.add(hex(ord(c)))
        if bad:
            bad_encoding_files.append((str(rel), bad))

        # --- Empty body ---
        if len(after_h.strip()) < 20:
            empty_body_files.append((str(rel), len(after_h.strip())))


# --- Report ---
print(f"Total files scanned: {total}")
print()

if missing_fm_files:
    print(f"=== MISSING FRONTMATTER: {len(missing_fm_files)} ===")
    for p in missing_fm_files[:10]:
        print(f"  {p}")

if error_files:
    print(f"\n=== OTHER ERRORS: {len(error_files)} ===")
    for p, e in error_files[:10]:
        print(f"  {p}: {e}")

if topline_mismatch:
    print(f"\n=== HEADING/SECTION MISMATCH: {len(topline_mismatch)} ===")
    for p, expected, got in topline_mismatch[:15]:
        print(f"  {p}: expected section={expected!r}, heading={got!r}")

if s_prefix_files:
    print(f"\n=== s. PREFIX AT BODY START: {len(s_prefix_files)} ===")
    for p, line in s_prefix_files[:15]:
        print(f"  {p}: {line!r}")

if dup_heading_files:
    print(f"\n=== HEADING DUPLICATED IN BODY: {len(dup_heading_files)} ===")
    for p, line in dup_heading_files[:15]:
        print(f"  {p}: body starts with {line!r}")

if bad_encoding_files:
    print(f"\n=== ENCODING ARTIFACTS: {len(bad_encoding_files)} ===")
    for p, chars in sorted(bad_encoding_files, key=lambda x: x[1])[:20]:
        print(f"  {p}: {chars}")

if empty_body_files:
    print(f"\n=== EMPTY/SHORT BODIES: {len(empty_body_files)} ===")
    for p, length in empty_body_files[:15]:
        print(f"  {p}: {length} chars")

print()
print("=== CLEAN STATS ===")
print(f"  Clean (no issues): {total - len(missing_fm_files) - len(error_files) - len(s_prefix_files) - len(dup_heading_files) - len(bad_encoding_files) - len(empty_body_files) - len(topline_mismatch)}")
print(f"  Total issues: {len(missing_fm_files) + len(error_files) + len(s_prefix_files) + len(dup_heading_files) + len(bad_encoding_files) + len(empty_body_files) + len(topline_mismatch)}")
