#!/usr/bin/env python3
"""Clean duplicate titles, footers, and leaked Part/Chapter lines from AML/CTF Rules section files."""
import re
import sys
from pathlib import Path

SECTIONS_DIR = Path(__file__).resolve().parent.parent / "data/aml-ctf-rules-2007/sections"

FOOTER_RE = re.compile(
    r"\n*---\n\*Anti-Money Laundering and Counter-Terrorism Financing Rules Instrument 2007 \(No\. 1\)\*\n?$"
)
PART_LEAK_RE = re.compile(r"^Part \d+\.\d+\t.*\n?", re.MULTILINE)
CHAPTER_LEAK_RE = re.compile(r"^CHAPTER \d+\t?.*\n?", re.MULTILINE)
SECTION_RE = re.compile(r'^section:\s*"?([^"\n]+)"?\s*$', re.MULTILINE)


def clean_file(text):
    stripped = []
    if not text.startswith("---\n"):
        return text, stripped
    end = text.find("\n---\n", 4)
    if end == -1:
        return text, stripped
    frontmatter, body = text[: end + 5], text[end + 5 :]

    m = SECTION_RE.search(frontmatter)
    if m:
        section = m.group(1)
        dup_re = re.compile(rf"^{re.escape(section)}[ \t]*\t.*\n?", re.MULTILINE)
        new_body, n = dup_re.subn("", body, count=1)
        if n:
            stripped.append("duplicate title line")
            body = new_body

    new_body, n = FOOTER_RE.subn("", body)
    if n:
        stripped.append("footer")
        body = new_body

    new_body, n = PART_LEAK_RE.subn("", body)
    if n:
        stripped.append(f"Part leak line x{n}")
        body = new_body

    new_body, n = CHAPTER_LEAK_RE.subn("", body)
    if n:
        stripped.append(f"CHAPTER leak line x{n}")
        body = new_body

    body = body.rstrip("\n") + "\n"
    return frontmatter + body, stripped


def main():
    dry_run = "--dry-run" in sys.argv
    files = sorted(SECTIONS_DIR.rglob("*.md"))
    modified = 0
    for path in files:
        original = path.read_text(encoding="utf-8")
        cleaned, stripped = clean_file(original)
        if cleaned != original:
            modified += 1
            rel = path.relative_to(SECTIONS_DIR.parent.parent.parent)
            print(f"{rel}: {', '.join(stripped)}")
            if not dry_run:
                path.write_text(cleaned, encoding="utf-8")
    print(f"\n{'Would modify' if dry_run else 'Modified'} {modified}/{len(files)} files")


if __name__ == "__main__":
    main()
