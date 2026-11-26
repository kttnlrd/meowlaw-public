#!/usr/bin/env python3
"""Constitution-specific FR DOCX -> legislation-explorer ingestion.

The Commonwealth of Australia Constitution Act is structured differently from a
normal Commonwealth Act, so ``ingest_fr_act.py`` cannot parse it:

  * the hierarchy is Chapter (Roman) -> Part (Roman) -> Section, with Divisions
    absent entirely (the generic script expects Part -> Division -> Subdivision);
  * sections are numbered "1.  Short title." (Roman chapters/parts, "N." +
    period + two spaces), not the "1  Short title" form the generic script
    keys on;
  * the body opens with 9 *covering clauses* before Chapter I, then repeats
    the "The Constitution." marker and a non-structural listing of
    chapters/parts (style ``TofSects(Section)``) that must be skipped;
  * a small SCHEDULE (oaths/affirmations) closes the text.

This variant walks the paragraphs directly and emits the same on-disk contract
as ``ingest_fr_act.py``: ``tree.json`` plus ``sections/part-*/division-*/N.md``.

Mapping used for tree.json (mirrors the corporAct convention where the tree
"part" level holds chapters and the "division" level holds Parts):

    tree.parts[]      <- Chapter (Roman)      [or Preamble / Covering clauses / Schedule]
    tree.parts[].divisions[] <- Part (Roman)  [implicit single division if the chapter has no Parts]
    ...sections[]     <- sections 1..128      [plus covering clauses 1..9]

Usage:
    python3 scripts/ingest_constitution.py \
        --docx data/constitution/raw/constitution.docx \
        --out-dir data/constitution \
        --act-name "Commonwealth of Australia Constitution Act" \
        --compilation-no 6 \
        --compilation-date "29 July 1977"
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from docx import Document

_EM = "\u2014"  # em dash
_NBSP = "\u00a0"


def clean(text: str) -> str:
    text = text.replace(_NBSP, " ")
    return re.sub(r"\s+", " ", text).strip()


def strip_period(text: str) -> str:
    return re.sub(r"\.\s*$", "", text).strip()


_ROMAN_RE = re.compile(r"^([IVXLCDM]+)$")


def roman_to_int(roman: str) -> int:
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    prev = 0
    for ch in reversed(roman.upper()):
        v = vals[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def parse_chapter(text: str) -> tuple[str, str] | None:
    m = re.match(r"^Chapter\s+([IVXLCDM]+)\s*[.\u2014\-\u2013]+\s*(.*)$", text)
    if not m:
        return None
    return m.group(1), strip_period(clean(m.group(2)))


def parse_part(text: str) -> tuple[str, str] | None:
    m = re.match(r"^Part\s+([IVXLCDM]+)\s*[.\u2014\-\u2013]+\s*(.*)$", text)
    if not m:
        return None
    return m.group(1), strip_period(clean(m.group(2)))


def parse_section(text: str) -> tuple[str, str] | None:
    # Body form: "1.  Short title."  /  "125.  Seat of Government."
    # Sections 86 and 87 carry no heading text ("86."), so the title is optional.
    m = re.match(r"^(\d+[A-Z]?)\.(?:\s+(.*))?$", text)
    if not m:
        return None
    title = strip_period(clean(m.group(2) or ""))
    return m.group(1), title


def flush_content(lines: list[tuple[str, str]]) -> str:
    md: list[str] = []
    for style, text in lines:
        t = clean(text.replace(_EM, _EM))
        if not t:
            if md and md[-1] != "":
                md.append("")
            continue
        if style == "Definition":
            md.append(f"**{t}**")
        elif style in ("note(text)", "note(para)", "note(margin)"):
            md.append(f"  > {t}")
        elif style == "Penalty":
            md.append(f"Penalty: {t}")
        elif style in ("SubsectionHead",):
            md.append(f"**{t}**")
        else:
            md.append(t)
    out: list[str] = []
    prev_blank = False
    for line in md:
        if line == "":
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(line)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


class Part:
    """A tree 'part': a chapter, or one of the special covering groups."""

    def __init__(self, pid: str, title: str, chapter: str, chapter_title: str):
        self.id = pid
        self.title = title
        self.chapter = chapter          # frontmatter chapter id
        self.chapter_title = chapter_title
        self.divisions: dict[str, dict] = {}
        self.order: list[str] = []

    def division(self, did: str, title: str, part_id: str, part_title: str) -> dict:
        if did not in self.divisions:
            self.divisions[did] = {
                "id": did,
                "title": title,
                "subdivisions": [],
                "sections": [],
                "part_id": part_id,
                "part_title": part_title,
            }
            self.order.append(did)
        return self.divisions[did]


def build_tree(doc: Document) -> tuple[list[Part], int]:
    parts: list[Part] = []
    current: Part | None = None
    current_div: dict | None = None
    current_sec: dict | None = None
    content: list[tuple[str, str]] = []
    preamble_buf: list[tuple[str, str]] = []
    in_body = False
    pre_chapter = True

    def flush_section() -> None:
        nonlocal content, current_sec
        if current_sec is not None:
            current_sec["_content"] = flush_content(content)
        content = []

    for p in doc.paragraphs:
        text = p.text
        stripped = text.strip()
        style = p.style.name if p.style else ""

        if not in_body:
            if style == "LongT":
                in_body = True
            continue

        # End of the operative text.
        if style in ("ENotesHeading 1", "ENotesHeading 2"):
            break

        # Non-structural listing of chapters/parts ("Chapter I.  The Parliament:").
        if style == "TofSects(Section)":
            continue

        if style == "ActHead 1":
            flush_section()
            current_sec = None
            current_div = None
            pre_chapter = False
            if re.match(r"^SCHEDULE\b", stripped, re.I):
                current = Part("schedule", "Schedule", "Schedule", "Schedule")
                parts.append(current)
                current_div = current.division(
                    "schedule", "Schedule", "", "")
                entry = {"id": "schedule", "title": "Schedule", "_content": ""}
                current_div["sections"].append(entry)
                current_sec = entry
            else:
                ch = parse_chapter(stripped)
                if not ch:
                    current = None
                    continue
                cid, ctitle = ch
                current = Part(cid, f"Chapter {cid}\u2014{ctitle}", cid,
                               f"Chapter {cid}\u2014{ctitle}")
                parts.append(current)
            continue

        if style == "ActHead 2":
            flush_section()
            current_sec = None
            if current is None:
                continue
            pt = parse_part(stripped)
            if not pt:
                current_div = None
                continue
            did, dtitle = pt
            current_div = current.division(
                did, f"Part {did}\u2014{dtitle}", did, f"Part {did}\u2014{dtitle}")
            continue

        if style == "ActHead 5":
            sec = parse_section(stripped)
            if not sec:
                # Unnumbered structural marker ("The Constitution.") — close
                # the open provision and skip it.
                flush_section()
                current_sec = None
                continue
            flush_section()

            if pre_chapter:
                # Covering clauses (1-9). Emit the preamble first if present.
                if preamble_buf:
                    pre = Part("preamble", "Preamble", "Preamble", "Preamble")
                    parts.append(pre)
                    d = pre.division("preamble", "Preamble", "", "")
                    d["sections"].append({
                        "id": "preamble", "title": "Preamble",
                        "_content": flush_content(preamble_buf),
                    })
                    preamble_buf = []
                if current is None or current.id != "covering":
                    current = Part("covering", "Covering clauses", "Covering clauses",
                                   "Covering clauses")
                    parts.append(current)
                    current_div = current.division(
                        "covering", "Covering clauses", "", "")

            sid, stitle = sec
            if current is None:
                current = Part("I", "Chapter I\u2014The Parliament", "I",
                               "Chapter I\u2014The Parliament")
                parts.append(current)
            if current_div is None:
                current_div = current.division(current.id, current.title, "", "")
            entry = {"id": sid, "title": stitle, "_content": ""}
            current_div["sections"].append(entry)
            current_sec = entry
            continue

        # Ordinary body paragraph.
        if current_sec is not None:
            content.append((style, text))
        elif current is None and pre_chapter:
            preamble_buf.append((style, text))

    flush_section()
    return parts, 0


def write_section(sec_dir: Path, sec_id: str, sec_title: str, body_text: str,
                  act_name: str, compilation_no, compilation_date,
                  chapter, chapter_title, part_id, part_title):
    sec_dir.mkdir(parents=True, exist_ok=True)
    path = sec_dir / f"{sec_id}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f'act: "{act_name}"\n')
        f.write(f'chapter: "{chapter}"\n')
        f.write(f'chapter_title: "{chapter_title}"\n')
        f.write(f'part: "{part_id}"\n')
        f.write(f'part_title: "{part_title}"\n')
        f.write('division: ""\n')
        f.write('division_title: ""\n')
        f.write(f'section: "{sec_id}"\n')
        f.write(f'section_title: "{sec_title}"\n')
        f.write(f"compilation_no: {compilation_no}\n")
        f.write(f'compilation_date: "{compilation_date}"\n')
        f.write("---\n\n")
        f.write(f"# {sec_id} {sec_title}\n\n")
        if body_text:
            f.write(body_text)
            f.write("\n")
        f.write("\n---\n")
        f.write(f"*{act_name}*\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest the Commonwealth of Australia Constitution Act DOCX")
    ap.add_argument("--docx", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--act-name", required=True)
    ap.add_argument("--compilation-no", required=True)
    ap.add_argument("--compilation-date", required=True)
    args = ap.parse_args()

    docx_path = Path(args.docx)
    out_dir = Path(args.out_dir)
    section_dir = out_dir / "sections"

    print(f"Reading DOCX: {docx_path}")
    doc = Document(str(docx_path))

    parts, _ = build_tree(doc)

    tree = {
        "act": args.act_name,
        "compilation_no": args.compilation_no,
        "compilation_date": args.compilation_date,
        "parts": [],
    }

    total = 0
    for part in parts:
        part_entry = {"id": part.id, "title": part.title, "divisions": []}
        for did in part.order:
            div = part.divisions[did]
            div_entry = {"id": div["id"], "title": div["title"],
                         "subdivisions": [], "sections": []}
            sec_dir = section_dir / f"part-{part.id}" / f"division-{did}"
            for sec in div["sections"]:
                sid = sec["id"]
                st = sec["title"]
                body = sec.get("_content", "")
                path = f"part-{part.id}/division-{did}/{sid}.md"
                div_entry["sections"].append({"id": sid, "title": st, "path": path})
                write_section(sec_dir, sid, st, body, args.act_name,
                              args.compilation_no, args.compilation_date,
                              part.chapter, part.chapter_title,
                              div["part_id"], div["part_title"])
                total += 1
            part_entry["divisions"].append(div_entry)
        tree["parts"].append(part_entry)

    tree_path = out_dir / "tree.json"
    with open(tree_path, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print(f"Written: {tree_path}")

    sec_files = list(section_dir.rglob("*.md"))
    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    print(f"Parts (chapters/special groups): {len(parts)}")
    print(f"Divisions (real Parts): {sum(len(p.order) for p in parts)}")
    print(f"Section files written: {total}")
    print(f"Section files on disk: {len(sec_files)}")


if __name__ == "__main__":
    main()
