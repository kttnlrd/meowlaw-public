#!/usr/bin/env python3
"""Ingest AML/CTF Rules 2007 from DOCX into legislation-explorer format.

Structure: Chapter → Part → Section
Chapters use HP style, Parts use HD style, Sections are Normal/Paragraph style.

Produces:
  - data/aml-ctf-rules-2007/tree.json
  - data/aml-ctf-rules-2007/sections/{chapter-slug}/{part-slug}/{section}.md

Usage:
    python3 scripts/ingest_aml_rules.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from docx import Document

DOCX_PATH = Path(__file__).resolve().parent.parent / "data" / "aml-ctf-2006" / "raw" / "aml-ctf-rules-2007.docx"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "aml-ctf-rules-2007"
SECTION_DIR = OUT_DIR / "sections"

ACT_TITLE = "Anti-Money Laundering and Counter-Terrorism Financing Rules Instrument 2007 (No. 1)"
COMPILATION_NO = 76
COMPILATION_DATE = "22 November 2024"


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.strip().lower())
    return s.strip("-") or "unnamed"


def parse():
    doc = Document(str(DOCX_PATH))
    pars = doc.paragraphs
    n = len(pars)
    print(f"Total paragraphs: {n}")

    # Locate body start: first paragraph with HR style
    body_start = None
    for i, p in enumerate(pars):
        s = p.style.name if p.style else ""
        if s == "HR":
            body_start = i
            break
    if body_start is None:
        print("ERROR: Could not find body start (HR style)")
        return
    print(f"Body starts at paragraph {body_start}")

    # Find endnotes boundary — search from end for "Endnote" or "Note Heading" style
    body_end = n
    for i in range(n - 1, body_start, -1):
        p = pars[i]
        s = p.style.name if p.style else ""
        if s == "Note Heading" or ("Endnote" in p.text.strip() and s in ("Normal", "Note Heading")):
            body_end = i
            break
    print(f"Body ends at paragraph {body_end}")

    # Walk body paragraphs to build structure: chapter → part → section
    chapters = []  # [{num, title, parts: [{num, title, sections: [{num, title, para_num}]}]}]
    current_chapter = None
    current_part = None

    for i in range(body_start, body_end):
        p = pars[i]
        s = p.style.name if p.style else ""
        text = p.text.strip()
        if not text:
            continue

        # Chapter heading
        if s == "HP" and text.startswith("CHAPTER"):
            m = re.match(r"CHAPTER\s+(\d+)(?:\s+(.*))?", text)
            if m:
                ch_num = int(m.group(1))
                ch_title = (m.group(2) or "").strip()
                current_chapter = {
                    "num": ch_num,
                    "title": f"Ch {ch_num} — {ch_title}" if ch_title else f"Ch {ch_num}",
                    "title_raw": ch_title,
                    "parts": [],
                }
                chapters.append(current_chapter)
                current_part = None
            continue

        # Part heading
        if (s == "HD" and text.startswith("Part")) or re.match(r"^Part\s+\d+\.\d+\s", text):
            m = re.match(r"Part\s+(\d+\.\d+)\s+(.*)", text)
            if m:
                part_num = m.group(1)
                part_title = m.group(2).strip()
                current_part = {
                    "num": part_num,
                    "title": f"Part {part_num} — {part_title}",
                    "title_raw": part_title,
                    "sections": [],
                }
                if current_chapter is not None:
                    current_chapter["parts"].append(current_part)
            continue

    print(f"Found {len(chapters)} chapters")
    for ch in chapters:
        print(f"  Ch {ch['num']}: {ch['title_raw'] or ''} ({len(ch['parts'])} parts)")

    # Second pass: extract sections from the body
    # Section patterns: "1 Name of Instrument", "1.1.1 Section title", "11.1 Section title"
    sec_re = re.compile(r"^(\d+(?:\.\d+)*)[\t\s]+(.+)")

    for i in range(body_start, body_end):
        p = pars[i]
        s = p.style.name if p.style else ""
        text = p.text.strip()
        if not text:
            continue

        # Only process section-like paragraphs
        # Sections are Normal, Paragraph, Default, or R1 style
        if s in ("HP", "HD", "HR", "Note Heading", "Header", "Title", "CoverMade", "CoverAct", "CoverUpdate", "SigningPageBreak", "MainBody Section Break", "ContentsHead", "TOC", "toc 2", "toc 3", "toc 5", "toc 9"):
            continue

        m = sec_re.match(text)
        if not m:
            continue

        sec_num = m.group(1)
        sec_title = m.group(2).strip()

        # Validate section number: should be simple digit, C.P.S, or Ch.S
        parts = sec_num.split(".")
        is_valid = False
        if len(parts) == 1 and sec_num.isdigit():
            # Standalone section e.g. "1 Name of Instrument", "2 Rules"
            # Only sections 1 and 2 are standalone prelim sections
            if sec_num in ("1", "2", "3"):
                is_valid = True
        elif len(parts) == 2:
            # Ch.S format: 11.1, 12.1, etc. (chapters without parts)
            try:
                if int(parts[0]) >= 11:
                    is_valid = True
            except ValueError:
                pass
        elif len(parts) == 3:
            # C.P.S format: 1.1.1, 4.2.14, etc.
            is_valid = True

        if not is_valid:
            continue

        # Find which chapter/part this section belongs to
        assigned = False
        sec_num_parts = sec_num.split(".")
        for ch in chapters:
            ch_str = str(ch["num"])
            if sec_num_parts[0] == ch_str:
                for part in ch["parts"]:
                    if len(sec_num_parts) >= 2:
                        part_prefix = sec_num_parts[0] + "." + sec_num_parts[1]
                        part_num = part["num"]
                        if part_prefix == part_num:
                            # Check for duplicate
                            if not any(s["num"] == sec_num for s in part["sections"]):
                                part["sections"].append({
                                    "num": sec_num,
                                    "title": sec_title,
                                    "para_num": i,
                                })
                            assigned = True
                            break
                if not assigned and len(sec_num_parts) == 2:
                    # Chapter-level section (no parts) e.g. 11.1
                    # Add to a synthetic part
                    if not ch["parts"]:
                        # Create a default part
                        ch["parts"].append({
                            "num": f"{ch['num']}.0",
                            "title": f"Ch {ch['num']}",
                            "title_raw": "",
                            "sections": [],
                        })
                    if not any(s["num"] == sec_num for s in ch["parts"][-1]["sections"]):
                        ch["parts"][-1]["sections"].append({
                            "num": sec_num,
                            "title": sec_title,
                            "para_num": i,
                        })
                    assigned = True
                break

        # Standalone sections (1, 2, 3 — before any chapter)
        if not assigned and sec_num in ("1", "2", "3"):
            if not chapters:
                chapters.append({
                    "num": 0,
                    "title": "Preliminary",
                    "title_raw": "",
                    "parts": [{
                        "num": "0",
                        "title": "Preliminary",
                        "title_raw": "",
                        "sections": [],
                    }],
                })
            if not any(s["num"] == sec_num for s in chapters[0]["parts"][0]["sections"]):
                chapters[0]["parts"][0]["sections"].append({
                    "num": sec_num,
                    "title": sec_title,
                    "para_num": i,
                })

    # Report
    total_sections = sum(len(s["sections"]) for ch in chapters for p in ch["parts"] for s in [p])
    total_parts = sum(len(ch["parts"]) for ch in chapters)
    print(f"\nTotal sections found: {total_sections}")

    # Third pass: extract section body content
    # Build section ordering
    all_sections = []
    for ch in chapters:
        for part in ch["parts"]:
            for sec in part["sections"]:
                all_sections.append((ch, part, sec))

    # Sort by para_num to get correct order
    all_sections.sort(key=lambda x: x[2]["para_num"])

    # Extract content for each section
    section_contents = {}
    for idx, (ch, part, sec) in enumerate(all_sections):
        start = sec["para_num"]
        # End: next section, next part, next chapter, or body_end
        end = body_end
        for j in range(idx + 1, len(all_sections)):
            next_sec = all_sections[j][2]
            end = next_sec["para_num"]
            break

        lines = []
        for pi in range(start, end):
            p = pars[pi]
            t = p.text
            s = p.style.name if p.style else ""
            if not t.strip():
                continue
            if s in ("Note Heading", "Header", "ContentsHead", "TOC", "toc 2", "toc 3", "toc 5"):
                continue
            lines.append(t.rstrip())

        section_contents[sec["num"]] = "\n".join(lines)

    # Build tree.json and write section files
    tree_parts = []
    for ch in sorted(chapters, key=lambda x: x["num"]):
        for part in ch["parts"]:
            part_slug = part_id(part["num"])
            ch_slug = f"ch-{ch['num']}"

            sec_list = []
            for sec in part["sections"]:
                sec_path = f"{ch_slug}/{part_slug}/{sec['num']}.md"
                sec_list.append({
                    "id": sec["num"],
                    "title": sec["title"],
                    "path": sec_path,
                })

                # Write section file
                section_dir = SECTION_DIR / ch_slug / part_slug
                section_dir.mkdir(parents=True, exist_ok=True)
                md_path = section_dir / f"{sec['num']}.md"

                body = section_contents.get(sec["num"], "")
                md = f"""---
act: "{ACT_TITLE}"
chapter: "Ch {ch['num']}{' — ' + ch['title_raw'] if ch['title_raw'] else ''}"
part: "{part['num']}"
part_title: "{part['title']}"
section: "{sec['num']}"
section_title: "{sec['title']}"
compilation_no: {COMPILATION_NO}
compilation_date: "{COMPILATION_DATE}"
---

# {sec['num']} {sec['title']}

{body}

---
*{ACT_TITLE}*
"""
                md_path.write_text(md, encoding="utf-8")

            tree_parts.append({
                "id": part_slug,
                "title": part["title"],
                "divisions": [],
                "sections": sec_list,
            })

    tree = {
        "act": ACT_TITLE,
        "compilation_no": COMPILATION_NO,
        "compilation_date": COMPILATION_DATE,
        "parts": tree_parts,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tree_path = OUT_DIR / "tree.json"
    tree_path.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")

    # Count files
    total_md = len(list(SECTION_DIR.rglob("*.md")))
    print(f"\n===== SUMMARY =====")
    print(f"Chapters: {len(chapters)}")
    print(f"Parts: {total_parts}")
    print(f"Section files written: {total_sections}")
    print(f"Section files on disk: {total_md}")
    print(f"Tree file: {tree_path}")
    print("Done.")


def part_id(num: str) -> str:
    return f"part-{num.replace('.', '-')}"


if __name__ == "__main__":
    parse()
