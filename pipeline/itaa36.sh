#!/bin/bash
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW="$ROOT/data/itaa-1936/raw"
OUT="$ROOT/data/itaa-1936/sections"
TMP=/tmp/itaa36_all.txt

rm -rf "$OUT"/*
mkdir -p "$OUT"

# Concatenate vols 1-4 (the acts), strip obvious footer/header noise
cat "$RAW"/vol01.txt "$RAW"/vol02.txt "$RAW"/vol03.txt "$RAW"/vol04.txt | \
  grep -vE '^\s*(Income Tax Assessment Act 1936\s+[0-9]+|Compilation No\. 191|Compilation date:|Authorised Version C2026C00165 registered 22/04/2026|Prepared by the Office of Parliamentary Counsel|This compilation is in 7 volumes|Each volume has its own contents|Includes amendments:|No\. 27, 1936|About this compilation|Uncommenced amendments|Application, saving and transitional provisions|Editorial changes|\s*[0-9]+\s*)$' | \
  grep -vE '^\s*(Section [0-9]+[A-Z]*|Part [IVX]+|Division [0-9]+[A-Z]*|Subdivision [A-Z]+)\s*$' > "$TMP"

python3 - "$TMP" "$OUT" << 'PY'
import re, sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2])

# Split on section headers: number + title at start of line
sections = list(re.finditer(r'\n([0-9]+[A-Z]*) ([^\n]+)\n', raw))

total = 0
for i, m in enumerate(sections):
    num = m.group(1)
    title = m.group(2).strip()
    start = m.end()
    end = sections[i+1].start() if i+1 < len(sections) else len(raw)
    body = raw[start:end].strip()

    # Determine part/division/subdivision by scanning backward from this section
    prefix = raw[:m.start()]
    part = re.findall(r'\nPart ([IVX]+)', prefix)
    div = re.findall(r'\nDivision ([0-9]+[A-Z]*)', prefix)
    subdiv = re.findall(r'\nSubdivision ([A-Z]+)', prefix)

    part_str = part[-1] if part else ""
    div_str = div[-1] if div else ""
    subdiv_str = subdiv[-1] if subdiv else ""

    part_dir = f"part-{part_str.lower()}" if part_str else "part-unknown"
    div_dir = f"division-{div_str.lower()}" if div_str else "division-unknown"
    target = out / part_dir / div_dir / f"{num.lower()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    fm = f"""---
act: "ITAA 1936"
section: "{num}"
section_title: "{title}"
part: "{part_str}"
division: "{div_str}"
subdivision: "{subdiv_str}"
---

# {num}  {title}

{body}
"""
    target.write_text(fm)
    total += 1

print(f"Wrote {total} sections", file=sys.stderr)
PY
