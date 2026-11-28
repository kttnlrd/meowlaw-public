#!/usr/bin/env python3
"""Minimal ITAA 1936 parser. Read pdftotext output, split by section headers,
strip footer noise, write markdown."""
import argparse, re, sys
from pathlib import Path

NOISE = [
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

# Structural regexes
RE_PART = re.compile(r"^Part\s+([IVX]+)(?:\s*[\u2014\u2013\-])?\s*(.*)$")
RE_DIVISION = re.compile(r"^Division\s+(\d+[A-Z]*)(?:\s*[\u2014\u2013\-])?\s*(.*)$")
RE_SUBDIVISION = re.compile(r"^Subdivision\s+([A-Z]+)(?:\s*[\u2014\u2013\-])?\s*(.*)$")
RE_SECTION = re.compile(r"^(\d+[A-Z]*)\s+(\S.*)$")

def is_noise(line):
    s = line.strip()
    if not s:
        return True
    for n in NOISE:
        if n in s:
            return True
    # Standalone page numbers or "Section X" page headers
    if re.match(r"^\d+\s*$", s):
        return True
    if re.match(r"^Section\s+\d+[A-Z]*$", s):
        return True
    # Footer: "Income Tax Assessment Act 1936" with trailing page number and lots of leading space
    if re.match(r"^\s{20,}Income Tax Assessment Act 1936\s+\d+\s*$", s):
        return True
    # Running headers without em-dash (e.g. "Part III Liability to taxation")
    if re.search(r"Part\s+[IVX]+$", s) or re.search(r"Division\s+\d+[A-Z]*$", s) or re.search(r"Section\s+\d+[A-Z]*$", s):
        return True
    return False

def parse_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")

    # First pass: identify all structural markers and section boundaries
    markers = []  # (line_idx, type, id, title)
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r").replace("\f", "")
        if is_noise(line):
            continue
        if m := RE_PART.match(line):
            markers.append((i, "part", m.group(1), m.group(2).strip()))
        elif m := RE_DIVISION.match(line):
            markers.append((i, "division", m.group(1), m.group(2).strip()))
        elif m := RE_SUBDIVISION.match(line):
            markers.append((i, "subdivision", m.group(1), m.group(2).strip()))
        elif m := RE_SECTION.match(line):
            markers.append((i, "section", m.group(1), m.group(2).strip()))

    # Build sections with context
    sections = []
    part = division = subdivision = None
    part_title = division_title = subdivision_title = ""

    for idx, (line_idx, kind, sid, title) in enumerate(markers):
        if kind == "part":
            part, part_title = sid, title
            division = subdivision = None
            division_title = subdivision_title = ""
        elif kind == "division":
            division, division_title = sid, title
            subdivision = None
            subdivision_title = ""
        elif kind == "subdivision":
            subdivision, subdivision_title = sid, title
        elif kind == "section":
            # Gather body until next marker
            start = line_idx + 1
            end = markers[idx + 1][0] if idx + 1 < len(markers) else len(lines)
            body_lines = []
            for j in range(start, end):
                bl = lines[j].rstrip("\r").replace("\f", "")
                if is_noise(bl):
                    continue
                body_lines.append(bl)
            # Strip leading blank lines
            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            # Strip trailing blank lines
            while body_lines and not body_lines[-1].strip():
                body_lines.pop()

            # Multi-line title: next non-blank, non-structural, non-noise lines
            # that are indented or short continuations
            extra_title = ""
            ti = line_idx + 1
            while ti < end:
                tl = lines[ti].rstrip("\r").replace("\f", "")
                if not tl.strip() or is_noise(tl):
                    ti += 1
                    continue
                # If it looks like a structural marker, stop
                if any(p.match(tl) for p in [RE_PART, RE_DIVISION, RE_SUBDIVISION, RE_SECTION]):
                    break
                # If deeply indented, it's body text
                if len(tl) - len(tl.lstrip()) >= 10:
                    break
                extra_title += " " + tl.strip()
                ti += 1

            sections.append({
                "number": sid,
                "title": (title + extra_title).strip(),
                "part": part or "",
                "part_title": part_title or "",
                "division": division or "",
                "division_title": division_title or "",
                "subdivision": subdivision or "",
                "subdivision_title": subdivision_title or "",
                "body": body_lines,
            })

    return sections

def write_markdown(section, out_dir):
    s = section
    part_dir = f"part-{s['part'].lower()}" if s["part"] else "part-unknown"
    div_dir = f"division-{s['division'].lower()}" if s["division"] else "division-unknown"
    target = out_dir / part_dir / div_dir / f"{s['number'].lower()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    fm = f"""---
act: "ITAA 1936"
part: "{s['part']}"
part_title: "{s['part_title']}"
division: "{s['division']}"
division_title: "{s['division_title']}"
subdivision: "{s['subdivision']}"
subdivision_title: "{s['subdivision_title']}"
section: "{s['number']}"
section_title: "{s['title']}"
compilation_no: 191
compilation_date: "2026-04-01"
---

# {s['number']}  {s['title']}

"""
    body = "\n".join(s["body"])
    target.write_text(fm + body + "\n", encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for vol in sorted(Path(args.raw_dir).glob("vol0[1-4].txt")):
        print(f"Parsing {vol.name}...", file=sys.stderr)
        sections = parse_file(vol)
        for s in sections:
            write_markdown(s, out_dir)
        print(f"  -> {len(sections)} sections", file=sys.stderr)
        total += len(sections)

    print(f"Done. {total} sections total.", file=sys.stderr)

if __name__ == "__main__":
    main()
