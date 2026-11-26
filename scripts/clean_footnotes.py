"""Clean up *For definition footnote markers from TAA 1953 markdown files.

These footnote lines were placed inline by the PDF converter but should
be removed from the body text. In some cases, the continuation text
following the footnote marker on the next page was also lost.
"""
import re
import shutil
from pathlib import Path

data_dir = Path(__file__).parent.parent / "data" / "taa-1953" / "sections"

# Known manual fixes for specific sections where text was lost across page breaks
# These were identified by examining pdftotext -layout output
KNOWN_FIXES = {
    "part-unknown/division-269/269-15.md": (
        r"\(\*2A\*\)  To avoid doubt, if the obligation of the company is an obligation to pay the amount of an estimate of an underlying liability under \n\*For definition, see section 995-1 of the Income Tax Assessment Act 1997.",
        "**(2A)**  To avoid doubt, if the obligation of the company is an obligation to pay the amount of an estimate of an underlying liability under Division 268, a director is subject to his or her obligation under subsection (1):",
    ),
}

# Pattern matching a line that is solely a footnote definition marker
# Covers: *For definition..., *For definitions..., Note: *For definition...
FOOTNOTE_LINE = re.compile(r"^\*For definition|^\s*\*For definition")

fixes_applied = 0
footnote_lines_removed = 0

for md_path in sorted(data_dir.rglob("*.md")):
    rel = md_path.relative_to(data_dir)
    content = md_path.read_text(encoding="utf-8")
    original = content

    # 1. Apply known structural fixes first
    if str(rel) in KNOWN_FIXES:
        pattern, replacement = KNOWN_FIXES[str(rel)]
        content = re.sub(pattern, replacement, content)

    # 2. Remove standalone footnote lines (lines consisting only of *For definition or *For definitions)
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if FOOTNOTE_LINE.match(stripped):
            footnote_lines_removed += 1
            continue
        new_lines.append(line)
    content = "\n".join(new_lines)

    if content != original:
        md_path.write_text(content, encoding="utf-8")
        fixes_applied += 1
        print(f"Fixed: {rel}")

print(f"\nTotal files fixed: {fixes_applied}")
print(f"Total footnote lines removed: {footnote_lines_removed}")
