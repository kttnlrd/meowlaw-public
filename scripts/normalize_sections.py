#!/usr/bin/env python3
"""
Normalize ITAA 1997 section markdown files - v3.
Final version - handles all edge cases found.
"""

import os
import re
import sys
from pathlib import Path

SECTIONS_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data/itaa-1997/sections")


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def normalize_file(path):
    content = read_file(path)
    parts = content.split("---")
    if len(parts) < 3:
        return False, "Not enough --- separators"

    yaml_block = parts[1].strip()
    body = parts[2]
    meta = {}
    for line in yaml_block.split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")

    snum = meta.get("section", "")
    stitle = meta.get("section_title", "")
    body_stripped = body.strip()

    # Get content lines (excluding the heading line if present)
    body_lines = [l for l in body_stripped.split("\n") if l.strip()]
    has_h = body_lines and body_lines[0].strip().startswith("# ")

    if has_h:
        # Check if the rest still needs formatting
        rest = body_stripped.split("\n", 1)
        rest_body = rest[1] if len(rest) > 1 else ""
        rest_lines = [l for l in rest_body.split("\n") if l.strip()]

        if not rest_lines:
            return False, "Already well-formatted"

        first_content = rest_lines[0]

        # If first content line doesn't look like a properly formatted paragraph
        # (starts with **(, > , ###, bullet), treat it as needing full normalization
        needs_reprocess = not re.match(r'^(\*\*\(|\s*>\s|###|- |\*\*)', first_content)

        if not needs_reprocess:
            # Just do inline fixes
            new_body = fix_inline_issues(body_stripped)
            if new_body != body_stripped:
                parts[2] = f"\n{new_body}\n"
                write_file(path, "---".join(parts))
                return True, "Inline fixes applied"
            return False, "Already well-formatted"

        # Re-process from scratch using just the raw text
        # We need to go back to the raw content, but we only have the normalized version
        # Best approach: get the first content line which should contain the heading text
        # and reconstruct from there
        parts[2] = f"\n{normalize_blob(first_content, snum, stitle)}\n"
        write_file(path, "---".join(parts))
        return True, "Re-normalized"

    new_body = normalize_blob(body_stripped, snum, stitle)
    parts[2] = f"\n{new_body}\n"
    write_file(path, "---".join(parts))
    return True, "Normalized"


def fix_inline_issues(body):
    """Fix minor issues in well-formatted files."""
    body = re.sub(r'\n---\s+Last updated.*?\n', '\n', body)
    lines = body.split("\n")
    new_lines = []
    for line in lines:
        line = re.sub(r'(\s*>)+$', '', line)
        # Split inline Note: in paragraphs
        if "Note:" in line and not line.strip().startswith(">"):
            line = re.sub(r'Note:\s*', '\n> ***Note:*** ', line, count=1)
        new_lines.append(line)
    return "\n".join(new_lines)


def get_depth(aid):
    parts = aid.lstrip("s").split("-")
    if len(parts) <= 2:
        return 0
    pp = parts[2:]
    if len(pp) <= 1:
        return 0
    elif len(pp) == 2:
        return 1
    else:
        return 2


def normalize_blob(blob, snum, stitle):
    """Transform one-line blob into structured markdown."""
    blob = re.sub(r'</a>', '', blob)
    blob = re.sub(r'\bCapped life\b', '', blob)
    blob = re.sub(r'Chapter \d+[^.]*?(?:Part|Division)\s+\S+[^.]*?(?:Division|Section)\s+\S+\w*', '', blob)
    blob = re.sub(r'  +', ' ', blob).strip()
    if not blob:
        return f"# {snum} {stitle}\n\n"

    has_anchors = bool(re.search(r'<a\s+id=', blob))

    if not has_anchors:
        return handle_no_anchor(blob, snum, stitle)

    hm = re.match(r'^([\dA-Za-z-]+)\s+(.*?)(?:\s+<a\s+id=)', blob)
    if hm:
        heading = f"# {hm.group(1)} {hm.group(2)}"
        blob = re.sub(r'^[\dA-Za-z-]+\s+.*?(?=\s*<a\s+id=)', '', blob, count=1).strip()
    else:
        heading = f"# {snum} {stitle}"
    if not blob:
        return heading + "\n\n"

    tokens = re.split(r'(<a\s+id="[^"]+"\s*>)', blob)
    out = [heading, ""]

    for idx, tok in enumerate(tokens):
        tok = tok.strip()
        if not tok:
            continue

        am = re.match(r'^<a\s+id="([^"]+)"\s*>', tok)
        if not am:
            text = tok.strip()
            text = re.sub(r'^>\s*', '', text)
            text = re.sub(r'\s*>\s*', ' ', text).strip()
            text = re.sub(r'\s{2,}', ' ', text).strip()
            if not text:
                continue
            if "•" in text:
                for item in re.split(r'\s*•\s*', text):
                    item = item.strip()
                    if item:
                        out.append("- " + item.rstrip(";"))
                continue
            em = re.match(r'^(Note|Example|Warning)\s*:\s*(.*)', text, re.IGNORECASE)
            if em:
                out.append(f"> ***{em.group(1)}:*** {em.group(2)}")
                continue
            if re.search(r'Table of sections|Operative provisions', text, re.IGNORECASE):
                out.extend(["", text, ""])
                continue
            if "Method statement" in text and "Step" in text:
                for sp in re.split(r'(Step\s+\d+\s*\.)', text):
                    if sp.strip():
                        if sp.startswith("Step"):
                            out.extend(["", sp])
                        else:
                            out.append(sp)
                continue
            if out and out[-1] != "":
                out.append("")
            out.append(text)
            continue

        aid = am.group(1)
        depth = get_depth(aid)
        prefix = "> " * depth
        label = ""
        text = ""

        if idx + 1 < len(tokens):
            nxt = tokens[idx + 1].strip()
            if not nxt.startswith("<a"):
                nxt = re.sub(r'\s*>\s*', ' ', nxt).strip()
                nxt = re.sub(r'\s{2,}', ' ', nxt).strip()
                lm = re.match(r'^(\d+|[a-z]+)\b', nxt)
                if lm:
                    label = lm.group(1)
                    text = nxt[len(label):].strip()
                    text = re.sub(r'^[,;]?\s*(?:or|and)?\s*', '', text)
                    text = re.sub(r'[,;]\s*(?:or|and)?\s*$', '', text).strip()
                else:
                    text = nxt
                tokens[idx + 1] = ""

        em = re.match(r'^(Note|Example|Warning)\s*:\s*(.*)', text, re.IGNORECASE)
        if em:
            line = f"{prefix}***{em.group(1)}:*** {em.group(2)}"
        elif label:
            line = f"{prefix}**({label})** {text}"
        else:
            line = f"{prefix}{text}" if text else ""

        line = re.sub(r'(\s*>)+$', '', line)
        if depth == 0 and out and out[-1] != "":
            out.append("")
        if line.strip():
            out.append(line)

    cleaned = []
    prev_blank = False
    for line in out:
        line = re.sub(r'(\s*>)+$', '', line).rstrip()
        b = line == ""
        if b and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = b

    return "\n".join(cleaned)


def handle_no_anchor(blob, snum, stitle):
    """Handle pure text sections with no anchor tags."""
    out = [f"# {snum} {stitle}", ""]

    # Extract heading from blob, remove its text
    hm = re.match(r'^([\dA-Za-z-]+)\s+(.*?)(?:\.|$)', blob)
    body_text = blob[len(hm.group(0)):].strip() if hm else blob

    # Split on table of sections and operative provisions
    body_text = re.sub(r'\s*Table of sections\s+', ' ###Table_of_sections ', body_text)
    body_text = re.sub(r'\s*Operative provisions\s+', ' ###Operative_provisions ', body_text)
    # Normalize note
    body_text = re.sub(r'Note:', '\n> ***Note:*** ', body_text)

    # Now split into paragraphs/sections
    sections = re.split(r'(###Table_of_sections|###Operative_provisions)', body_text)
    current_header = ""
    for seg in sections:
        seg = seg.strip()
        if not seg:
            continue
        if seg == "###Table_of_sections":
            out.append("")
            out.append("### Table of sections")
            out.append("")
            continue
        if seg == "###Operative_provisions":
            out.append("")
            out.append("### Operative provisions")
            out.append("")
            continue
        # Check if this section starts with list of section references
        if "•" in seg:
            items = re.split(r'\s*•\s*', seg)
            for item in items:
                item = item.strip()
                if item:
                    out.append("- " + item.rstrip(";"))
        elif re.search(r'\d+-\d+', seg):
            # Section reference list — split on section numbers
            refs = re.split(r',?\s+(?=\d+-\d+)', seg)
            for ref in refs:
                ref = ref.strip()
                if ref:
                    out.append(f"- {ref}")
        else:
            if out and out[-1] != "":
                out.append("")
            out.append(seg)

    # Clean blanks
    cleaned = []
    prev_blank = False
    for line in out:
        b = line.strip() == ""
        if b and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = b

    return "\n".join(cleaned)


def process_all():
    stats = {"n": 0, "r": 0, "f": 0, "e": 0}
    for root, dirs, files in os.walk(SECTIONS_DIR):
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            path = os.path.join(root, fname)
            try:
                changed, msg = normalize_file(path)
                if changed:
                    print(f"  ✓ {os.path.relpath(path, SECTIONS_DIR)}: {msg}")
            except Exception as e:
                stats["e"] += 1
                print(f"  ✗ {fname}: {e}", file=sys.stderr)
    return stats


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--all":
        path = sys.argv[1]
        changed, msg = normalize_file(path)
        print(f"{path}: {msg}")
        if changed:
            print(read_file(path))
    else:
        stats = process_all()
        print(f"\n{'='*50}")
        print(f"Normalized: {stats['n']}")
        print(f"Re-normalized: {stats['r']}")
        print(f"Inline fixed: {stats['f']}")
        print(f"Errors: {stats['e']}")