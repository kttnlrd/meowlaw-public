#!/usr/bin/env python3
"""Replace curly quotes with straight quotes in a given dataset directory."""
import sys, re, glob
from pathlib import Path

target = Path(sys.argv[1])
dry_run = '--dry-run' in sys.argv

def fix_quotes(text: str) -> str:
    text = text.replace('\u201c', '"')
    text = text.replace('\u201d', '"')
    text = text.replace('\u2018', "'")
    text = text.replace('\u2019', "'")
    return text

sections_dir = target / 'sections'
chapters_dir = target / 'chapters'

files = []
if sections_dir.is_dir():
    files = [p for p in sections_dir.rglob('*.md') if '.bak' not in str(p)]
elif chapters_dir.is_dir():
    files = [p for p in chapters_dir.rglob('*.md') if '.bak' not in str(p)]
else:
    files = [p for p in target.rglob('*.md') if '.bak' not in str(p)]

modified = 0
total = len(files)

for fp in sorted(files):
    try:
        original = fp.read_text(encoding='utf-8', errors='replace')
    except:
        continue
    fixed = fix_quotes(original)
    if fixed != original:
        modified += 1
        if not dry_run:
            fp.write_text(fixed, encoding='utf-8')

print(f'{target.name}: {total} files, {modified} modified' + (' (dry run)' if dry_run else ''))
