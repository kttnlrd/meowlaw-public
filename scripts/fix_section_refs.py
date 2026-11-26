#!/usr/bin/env python3
"""Fix section reference lists in guide sections and remove duplicate headers."""

import os
import re
from pathlib import Path

SECTIONS = f"{Path(__file__).resolve().parent.parent}/data/itaa-1997/sections"
fixed = 0

for root, dirs, files in os.walk(SECTIONS):
    for fname in sorted(files):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(root, fname)
        content = open(path, "r", encoding="utf-8").read()
        orig = content
        parts = content.split("---", 2)
        if len(parts) < 3:
            continue
        body = parts[2]

        new_body = body

        # Remove duplicate "### Operative provisions" 
        # (first legit one, second at end from original text)
        new_body = re.sub(
            r'### Operative provisions\n\n### Operative provisions',
            '### Operative provisions',
            new_body
        )

        # Convert inline section references to bullet list
        # After "### Operative provisions" heading, convert "275-10 Title 275-15 Title"
        # Split on section number patterns
        lines = new_body.split("\n")
        new_lines = []
        in_section_list = False

        for line in lines:
            stripped = line.strip()
            # Check if this line is a paragraph of section references
            # Pattern: lines after ### headings that contain section-number sequences
            if stripped and not stripped.startswith("#") and not stripped.startswith(">"):
                refs = re.findall(r'\b\d+-\d+\b', stripped)
                if refs and len(refs) > 1 and len(stripped) > 100:
                    # This is a section reference line — convert to bullet list
                    # Split on each section reference
                    items = re.split(r'(?=\b\d+-\d+\b)', stripped)
                    for item in items:
                        item = item.strip()
                        if item:
                            new_lines.append(f"- {item}")
                    continue

            new_lines.append(line)

        new_body = "\n".join(new_lines)

        if new_body != body:
            parts[2] = new_body
            new_content = "---".join(parts)
            open(path, "w", encoding="utf-8").write(new_content)
            fixed += 1
            print(f"  ✓ {os.path.relpath(path, SECTIONS)}")

print(f"\nFixed: {fixed} files")