#!/usr/bin/env python3
"""Clean up PDF header noise and duplicate headings from section files."""
import os, re
from pathlib import Path

SECTIONS = f"{Path(__file__).resolve().parent.parent}/data"
FIXED = 0

# PDF header noise patterns that appear inline in section text
HEADER_NOISE = re.compile(
    r'Chapter \d+.*?(?:Part|Division|Section).*?(?=\d+-|$)',
    re.IGNORECASE
)
# Trailing PDF header fragments at end of a line
TRAILING_NOISE = re.compile(
    r'\s+Chapter \d+.*$',
    re.IGNORECASE
)
# Bullet line that is ONLY PDF header noise
BULLET_NOISE = re.compile(
    r'^- .*?(?:Chapter \d+|Part \d+-\d+|Division \d+|Section \d+)',
    re.IGNORECASE
)

for root, dirs, files in os.walk(SECTIONS):
    for fname in sorted(files):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(root, fname)
        content = open(path, "r", encoding="utf-8").read()
        orig = content

        # Split frontmatter from body
        parts = content.split("---", 2)
        if len(parts) < 3:
            continue
        body = parts[2]
        new_lines = []
        prev_blank = False

        for line in body.split("\n"):
            stripped = line.strip()

            # Remove lines that are ONLY PDF header noise in bullet format
            if re.match(r'^- (Chapter \d+|Part \d+[\d-]*|Division \d+|Section \d+)', stripped, re.IGNORECASE):
                continue  # skip pure noise bullets

            # Remove duplicate "### Operative provisions" 
            if stripped == "### Operative provisions" and "### Operative provisions" in "\n".join(new_lines):
                continue

            # Clean trailing PDF header fragments from legitimate content lines
            if stripped.startswith("- ") and not stripped.startswith("- #"):
                cleaned = re.sub(TRAILING_NOISE, "", stripped)
                if cleaned != stripped:
                    stripped = cleaned
                    line = cleaned

            # Collapse multiple blank lines
            if stripped == "":
                if prev_blank:
                    continue
                prev_blank = True
            else:
                prev_blank = False

            new_lines.append(line)

        new_body = "\n".join(new_lines)
        if new_body != body:
            parts[2] = new_body
            new_content = "---".join(parts)
            open(path, "w", encoding="utf-8").write(new_content)
            FIXED += 1
            print(f"  ✓ {os.path.relpath(path, SECTIONS)}")

print(f"\nFixed: {FIXED} files")