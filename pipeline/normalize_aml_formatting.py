#!/usr/bin/env python3
"""Normalize AML/CTF legislation markdown: convert tab-separated
subsection/paragraph/subparagraph structure to GFM blockquote + bold format
matching the tax-act display spec.

Patterns (from data/aml-ctf-2006 and data/aml-ctf-rules-2007):
  (N)\ttext            -> subsection      ** (N) **  text
  (letter)\ttext       -> paragraph       > ** (letter) **  text
  (roman)\ttext        -> subparagraph    > > ** (roman) **  text
  Note:\ttext           -> note            > ** Note: **  text
  \\ttext  (bare tab)   -> continuation    dedent at current level
  plain text            -> continuation    keep at current level
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SUBSECTION = re.compile(r"^\((\d+[A-Za-z]*)\)\t(.*)$")
SUBSUBPARA = re.compile(r"^\((i|ii|iii|iv|v|vi|vii|viii|ix|x|x{1,2})\)\t(.*)$")
PARA = re.compile(r"^\(([a-z]+)\)\t(.*)$")
NOTE = re.compile(r"^Note(\s+\d+)?:\t(.*)$")
BULLET = re.compile(r"^•\t(.*)$")
BARE_TAB = re.compile(r"^\t(.*)$")

# Pre-split: some files have multiple items crammed onto one line
# (a docx→markdown extraction artifact). Split on boundary before each item.
MULTI_ITEM = re.compile(
    r"(?=\((?:i|ii|iii|iv|v|vi|vii|viii|ix|x|x{1,2})\)\t"
    r"|\(\d+[A-Za-z]*\)\t"
    r"|\([a-z]+\)\t"
    r"|Note\s*\d*:\t"
    r"|•\t)"
)

PREFIX = {0: "", 1: "> ", 2: "> > "}


def normalize_body(lines: list[str]) -> list[str]:
    out: list[str] = []
    level = 0  # 0=subsection, 1=paragraph, 2=subparagraph
    for raw in lines:
        line = raw.rstrip("\n")

        # Pre-split: docx extraction sometimes crams multiple items
        # onto one line. Split before each item boundary.
        parts = MULTI_ITEM.split(line) if "\t" in line else [line]
        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Process the part as a regular item line
            if m := SUBSECTION.match(part):
                level = 0
                out.append("")
                out.append(f"**({m.group(1)})**  {m.group(2)}")
            elif m := SUBSUBPARA.match(part):
                level = 2
                out.append("")
                out.append(f"> > **({m.group(1)})**  {m.group(2)}")
            elif m := PARA.match(part):
                level = 1
                out.append("")
                out.append(f"> **({m.group(1)})**  {m.group(2)}")
            elif m := NOTE.match(part):
                level = 0
                out.append("")
                out.append(f"> **Note:** {m.group(2)}")
            elif m := BULLET.match(part):
                level = 0
                out.append("")
                out.append(f"- {m.group(1)}")
            elif m := BARE_TAB.match(part):
                text = m.group(1).strip()
                if not text:
                    continue
                if out and out[-1].startswith(PREFIX[level]):
                    out[-1] = out[-1] + " " + text
                else:
                    out.append(PREFIX[level] + text)
            else:
                # plain prose continuation
                if out and out[-1].startswith(PREFIX[level]) and not out[-1].startswith("**("):
                    out[-1] = out[-1] + " " + part
                else:
                    out.append(part)

    # strip trailing blank lines
    while out and out[-1] == "":
        out.pop()
    return out


def process_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "\t" not in text:
        return False

    lines = text.split("\n")
    # locate frontmatter (first --- ... ---) and footer (last --- line)
    # body = lines between first closing --- and the footer '---' separator
    front_end = None
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                front_end = i
                break
    if front_end is None:
        return False

    head = lines[: front_end + 1]  # includes frontmatter

    # footer: find the last '---' line that starts a footer block
    # (the body's # heading is between frontmatter and footer)
    # body starts after frontmatter; footer starts at a '---' line that
    # is followed by '*Act name*'
    body_and_footer = lines[front_end + 1 :]
    footer_start = None
    for i in range(len(body_and_footer)):
        ln = body_and_footer[i].strip()
        if ln == "---" and i + 1 < len(body_and_footer):
            nxt = body_and_footer[i + 1].strip()
            if nxt.startswith("*") and nxt.endswith("*"):
                footer_start = i
                break
    if footer_start is None:
        # No footer block — body extends to end of file.
        body = body_and_footer
        footer = []
    else:
        body = body_and_footer[:footer_start]
        footer = body_and_footer[footer_start:]

    # keep the '# section title' H1 and following blank lines at body start
    # normalize only the content AFTER the title line
    title_end = 0
    for i, ln in enumerate(body):
        if ln.strip().startswith("# "):
            title_end = i + 1
            break
    # skip blank lines after title
    while title_end < len(body) and body[title_end].strip() == "":
        title_end += 1

    title_part = body[:title_end]
    content = body[title_end:]

    normalized = normalize_body(content)

    new_text = "\n".join(head + title_part + normalized + [""] + footer)
    # Any remaining tabs (frontmatter titles, heading, mid-line artifacts)
    # become single spaces — structural tabs are already converted above.
    new_text = new_text.replace("\t", " ")
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    roots = [
        Path("data/aml-ctf-2006/sections"),
        Path("data/aml-ctf-rules-2007/sections"),
    ]
    changed = 0
    total = 0
    for root in roots:
        if not root.exists():
            print(f"skip (missing): {root}", file=sys.stderr)
            continue
        for f in sorted(root.rglob("*.md")):
            total += 1
            try:
                if process_file(f):
                    changed += 1
            except Exception as e:
                print(f"ERROR {f}: {e}", file=sys.stderr)
    print(f"processed {total} files, changed {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
