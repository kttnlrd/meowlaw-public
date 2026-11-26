#!/usr/bin/env python3
"""Fix remaining formatting issues in section files:
1. Inline Table of sections / Operative provisions → proper headings + lists
2. Inline Method statement → break onto new line
3. Inline "Working out" subsection titles → break onto new line
"""
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

        # Split into parts
        parts = content.split("---", 2)
        if len(parts) < 3:
            continue
        yaml = parts[1]
        body = parts[2]

        new_body = body

        # 1. Fix inline Method statement
        # Pattern: "Method statement Step 1. Text Step 2. More text"
        new_body = re.sub(
            r'Method statement\s+(Step\s+\d+\.)',
            r'Method statement\n\1',
            new_body
        )

        # 2. Fix inline "Working out" subsection titles
        # Pattern: "(subsection (3)). Working out if this section applies\n\n<a id" 
        new_body = re.sub(
            r'(\.\s*)\[Ww\]orking\s+out\s+(if|whether)\s+(this|the)\s+section\s+applies',
            r'. Working out if this section applies\n',
            new_body,
            count=0
        )

        # 3. Fix inline Table of sections / Operative provisions
        # First: detect if this is a guide section with Table of sections
        # Pattern: "text. Table of sections Operative provisions Section-N Section-NN..."
        new_body = re.sub(
            r'Table of sections\s+Operative provisions\s+',
            '\n\n### Table of sections\n\n### Operative provisions\n\n',
            new_body
        )
        # Some have just "Table of sections" without Operative provisions
        new_body = re.sub(
            r'(?<!\n### )Table of sections\s+',
            '\n\n### Table of sections\n\n',
            new_body
        )
        # Some have just "Operative provisions" at the end
        new_body = re.sub(
            r'(?<!\n### )Operative provisions\s*$',
            '\n\n### Operative provisions\n\n',
            new_body,
            flags=re.MULTILINE
        )

        if new_body != body:
            parts[2] = new_body
            new_content = "---".join(parts)
            open(path, "w", encoding="utf-8").write(new_content)
            fixed += 1
            print(f"  ✓ {os.path.relpath(path, SECTIONS)}")

print(f"\nFixed: {fixed} files")