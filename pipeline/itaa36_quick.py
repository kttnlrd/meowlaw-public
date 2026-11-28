#!/usr/bin/env python3
"""Quick ITAA 1936 parser: strip noise, split on section headers."""
import re, sys
from pathlib import Path

raw_dir = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)

# Noise patterns (match anywhere in line)
noise = [
    "Compilation No. 191",
    "Compilation date:",
    "Authorised Version C2026C00165",
    "registered 22/04/2026",
    "Includes amendments:",
    "No. 27, 1936",
    "Prepared by the Office of Parliamentary Counsel",
    "This compilation is in 7 volumes",
    "Each volume has its own contents",
]

def is_noise(line):
    s = line.strip()
    if not s:
        return True
    for n in noise:
        if n in s:
            return True
    # Centered footer: lots of spaces + "Income Tax Assessment Act 1936" + page number
    if re.match(r'^\s{20,}Income Tax Assessment Act 1936\s+\d+\s*$', s):
        return True
    # Standalone page numbers
    if re.match(r'^\d+\s*$', s):
        return True
    # Standalone "Section X" page headers
    if re.match(r'^Section\s+\d+[A-Z]*\s*$', s):
        return True
    # Running headers (no em-dash, just text)
    if re.search(r'Part\s+[IVX]+\s*$', s) or re.search(r'Division\s+\d+[A-Z]*\s*$', s) or re.search(r'Section\s+\d+[A-Z]*\s*$', s):
        return True
    return False

# Structural patterns
re_part = re.compile(r'^Part\s+([IVX]+)')
re_division = re.compile(r'^Division\s+(\d+[A-Z]*)')
re_subdivision = re.compile(r'^Subdivision\s+([A-Z]+)')
# Section: starts at column 0, number + space + non-empty title
re_section = re.compile(r'^(\d+[A-Z]*)\s+(\S.*)$')

sections = []
part = division = subdivision = ""

for vol in sorted(raw_dir.glob("vol0[1-4].txt")):
    print(f"Reading {vol.name}...", file=sys.stderr)
    text = vol.read_text()
    # Split on form feeds to handle page breaks
    pages = text.split('\f')
    for page in pages:
        lines = page.split('\n')
        for line in lines:
            line = line.rstrip('\r')
            if is_noise(line):
                continue
            # Check structural markers (must be at start of line, no indent)
            if m := re_part.match(line):
                part = m.group(1)
                division = subdivision = ""
                continue
            if m := re_division.match(line):
                division = m.group(1)
                subdivision = ""
                continue
            if m := re_subdivision.match(line):
                subdivision = m.group(1)
                continue
            # Check section header
            if m := re_section.match(line):
                num = m.group(1)
                title = m.group(2).strip()
                # Skip TOC entries (indented or containing dots)
                if re.search(r'\.{3,}', title):
                    continue
                # Skip page footer artifacts
                if title == "Income Tax Assessment Act 1936":
                    continue
                if "Compilation No." in title or "Compilation date:" in title:
                    continue
                sections.append({
                    "num": num, "title": title,
                    "part": part, "division": division, "subdivision": subdivision,
                    "body": []
                })
                continue
            # Body line
            if sections:
                sections[-1]["body"].append(line)

# Write files
total = 0
for s in sections:
    part_dir = f"part-{s['part'].lower()}" if s['part'] else "part-unknown"
    div_dir = f"division-{s['division'].lower()}" if s['division'] else "division-unknown"
    target = out_dir / part_dir / div_dir / f"{s['num'].lower()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(s["body"]).strip()
    fm = f"""---
act: "ITAA 1936"
section: "{s['num']}"
section_title: "{s['title']}"
part: "{s['part']}"
division: "{s['division']}"
subdivision: "{s['subdivision']}"
---

# {s['num']}  {s['title']}

{body}
"""
    target.write_text(fm)
    total += 1

print(f"Wrote {total} sections", file=sys.stderr)
