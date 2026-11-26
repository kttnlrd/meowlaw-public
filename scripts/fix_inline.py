#!/usr/bin/env python3
"""Lightweight inline fix for ITAA 1997 section markdown files."""

import os
import re
from pathlib import Path

SECTIONS_DIR = f"{Path(__file__).resolve().parent.parent}/data/itaa-1997/sections"

fixes = 0
for root, dirs, files in os.walk(SECTIONS_DIR):
    for fname in sorted(files):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(root, fname)
        content = open(path, "r", encoding="utf-8").read()
        orig = content

        # Remove straggling "--- Last updated" lines from body
        content = re.sub(r'\n---\s+Last updated.*?\n', '\n', content)

        # Clean up: if an anchor line has trailing orphan >, fix it
        content = re.sub(r'(</a>)\s*>\s*', r'\1', content)
        content = re.sub(r'>\s*$', '', content, flags=re.MULTILINE)

        # Remove blank lines between anchor tags and their content
        # Pattern: <a id="x"></a>\n\n**(N)** → <a id="x"></a>\n**(N)**
        content = re.sub(r'(<a\s+id="[^"]+"\s*/?>\s*)\n\n(\*\*\()', r'\1\n\2', content)

        # Fix: inline subsection titles like "Working out if this section applies"
        # at end of subsection text
        # Pattern: end of paragraph text followed by uppercase start on same line
        # e.g., "...subsection (3)). Working out if this section applies\n\n<a id="s40-102-2">"  
        content = re.sub(r'(\.\)\)[\s]*)([A-Z][a-z]+ [A-Z][a-z]+ .*?)(?=\n\n<a)', r'\1\n\2', content)

        if content != orig:
            open(path, "w", encoding="utf-8").write(content)
            fixes += 1
            print(f"  ✓ {os.path.relpath(path, SECTIONS_DIR)}")

print(f"\nFixed: {fixes} files")