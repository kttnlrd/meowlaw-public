#!/usr/bin/env python3
"""Criminal Code Act 1995 (Cth) — chapter-aware, multi-volume ingestion.

Why this is not the generic ``scripts/ingest_fr_act.py``:

1. DEPTH. The generic script assumes the FR-of-Legislation layout
   Part > Division > Subdivision > section (``toc 2/3/4/5``). The Criminal
   Code is one level deeper: Chapter > Part > Division > Subdivision >
   section, and it puts *both* Division and Subdivision at ``toc 4``.

2. VOLUMES. Compilation 174 ships as three DOCX volumes (the Schedule is
   split); the Act's own ss 1-5 live only in volume 1.

3. NUMBERING. Criminal Code numbers are dotted and letter-suffixed
   (Part 2.1, s 261.3, s 400.2AA, Division 114A). The generic regexes only
   match ``\\d+[A-Z]?``.

4. BODY GATE. Volumes 2 and 3 carry no ``LongT`` paragraph, so the generic
   script's "body starts at LongT" rule extracts nothing from them. Here the
   body starts at the first ``LongT`` *or* ``ActHead 1``.

Mapping onto the corpus tree schema (parts / divisions / subdivisions /
sections), following the precedent set by data/corporations-act-2001:

    Chapter              -> parts[]
    Part                 -> divisions[]     (synthetic from the Chapter when
                                             the Chapter has no Part, e.g. Ch 1)
    Division, Subdivision -> subdivisions[]  (flat, in document order; both sit
                                             at ``toc 4`` in the source)
    section              -> sections[]

Titles stay self-describing ("Chapter 10—National infrastructure",
"Part 10.2—Money laundering", "Division 400—Money laundering"), exactly as
corporations-act-2001 keeps "Ch 2A — ..." / "Part 2A.1—..." / "Div 4 — ...".

Usage:
    python3 scripts/ingest_criminal_code.py \\
        --docx data/criminal-code-1995/raw/criminal-code-1995-vol1.docx \\
        --docx data/criminal-code-1995/raw/criminal-code-1995-vol2.docx \\
        --docx data/criminal-code-1995/raw/criminal-code-1995-vol3.docx \\
        --out-dir data/criminal-code-1995 \\
        --act-name "Criminal Code Act 1995" \\
        --compilation-no 174 --compilation-date "30 June 2026"

Source (compilation 174, register C2026C00243, series C2004A04868):
    https://www.legislation.gov.au/C2004A04868/2026-06-30/2026-06-30/text/original/word/{1,2,3}
Note the URL uses the SERIES id (C2004A04868), not the compilation register
id — the register-id form 404s.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from docx import Document

_EM = "\u2014"
_NBSP = "\u00a0"
_TAB = "\t"

# Word wraps text in <w:smartTag> elements; python-docx's Paragraph.text only
# walks DIRECT <w:r> children, so anything inside a smartTag silently vanishes.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _collect(el, out: list) -> None:
    for ch in el:
        tag = ch.tag
        if tag == _W + "pPr":
            continue
        if tag == _W + "t":
            out.append(ch.text or "")
        elif tag == _W + "tab":
            out.append("\t")
        elif tag == _W + "noBreakHyphen":
            out.append("-")
        elif tag == _W + "softHyphen":
            out.append("\u00ad")
        elif tag in (_W + "br", _W + "cr"):
            out.append("\n")
        else:
            _collect(ch, out)


def para_text(p) -> str:
    out: list = []
    _collect(p._p, out)
    return "".join(out)


def clean(text: str) -> str:
    text = text.replace(_EM, "\u2014")
    text = text.replace(_NBSP, " ")
    return text.strip()


# Dotted + letter-suffixed ids, including a letter suffix *before* the dot:
# "1", "3A", "1.1", "2.1", "261.3", "11.2A", "92A.1", "105A.2A", "273B.4",
# "400.2AA". The generic `\d+[A-Z]?` form used elsewhere misses every
# "NNNa.N" number, which silently dropped 85 sections on the first pass.
_NUM = r'\d+[A-Z]*(?:\.\d+[A-Z]*)*'
# Section number at the head of a toc 5 / ActHead 5 line, then a tab, NBSP or
# 2+ spaces before the title ("268.1\tPurpose of Division", "1  Short title").
_NUM_SEP = r'(?:[' + _TAB + _NBSP + r']|\s{2,}|\s*$)'


def toc_chapter_id(text: str) -> str | None:
    m = re.match(r'Chapter\s+(' + _NUM + r')\s*[\u2014\-\u2013]', text)
    return m.group(1) if m else None


def toc_part_id(text: str) -> str | None:
    m = re.match(r'Part\s+(' + _NUM + r')\s*[\u2014\-\u2013]', text)
    return m.group(1) if m else None


def toc_div_id(text: str) -> str | None:
    m = re.match(r'Division\s+(' + _NUM + r')\s*[\u2014\-\u2013]?', text)
    return m.group(1) if m else None


def toc_subdiv_id(text: str) -> str | None:
    m = re.match(r'Subdivision\s+([A-Z])\s*[\u2014\-\u2013]', text)
    return m.group(1) if m else None


def extract_num(text: str) -> str | None:
    m = re.match(r'(' + _NUM + r')' + _NUM_SEP, text)
    return m.group(1) if m else None


def extract_title(text: str) -> str:
    return re.sub(r'^' + _NUM + _NUM_SEP, '', text).strip()


def strip_toc_page(text: str) -> str:
    return re.sub(r'\t\d+$', '', text).strip()


# The Act's own ss 1-5 sit in front of the Schedule and carry no Part or
# Chapter. They need a container or parse_toc drops every one of them.
_NO_PART_ID = "sections"
_NO_PART_TITLE = "Sections"


def _new_part(pid: str, title: str) -> dict:
    return {"id": pid, "title": clean(title), "divisions": []}


def _new_div(did: str, title: str) -> dict:
    return {"id": did, "title": clean(title), "subdivisions": [], "sections": []}


def _new_subdiv(sid: str, title: str) -> dict:
    return {"id": sid, "title": clean(title), "sections": []}


def parse_toc(doc: Document) -> list[dict]:
    """TOC -> [Chapter ...] with Part/Division/Subdivision nesting."""
    parts: list[dict] = []
    cur_ch = None          # Chapter  -> tree parts[]
    cur_pd = None          # Part     -> tree divisions[]
    cur_sd = None          # Division/Subdivision -> tree subdivisions[]
    in_toc = False

    def ensure_ch() -> bool:
        """Sections before any Chapter (or an Act with no Chapter) need a home."""
        nonlocal cur_ch, cur_pd, cur_sd
        if cur_ch is None:
            cur_ch = _new_part(_NO_PART_ID, _NO_PART_TITLE)
            parts.append(cur_ch)
            cur_pd = None
            cur_sd = None
        return True

    def ensure_pd() -> None:
        nonlocal cur_pd, cur_sd
        ensure_ch()
        if cur_pd is None:
            # Chapter with no Part (Ch 1, Ch 8): synthesise one from the Chapter.
            cur_pd = _new_div(cur_ch['id'], cur_ch['title'])
            cur_ch['divisions'].append(cur_pd)
            cur_sd = None

    for p in doc.paragraphs:
        text = para_text(p).strip()
        if not text:
            continue
        style = p.style.name if p.style else ''

        if not in_toc:
            if text == 'Contents':
                in_toc = True
            continue

        # TOC ends where the body begins (LongT long title, or ActHead).
        if style in ('LongT', 'ActHead'):
            break

        if style == 'toc 1':
            if text.startswith('Schedule'):
                # A volume boundary marker, not a container: Chapters are the
                # top level. Reset so a Chapter-less lead-in cannot leak across.
                cur_ch = cur_pd = cur_sd = None
            elif text.startswith(('Dictionary', 'Endnotes')):
                break
            continue

        if style == 'toc 2':
            if text.startswith('Chapter'):
                cid = toc_chapter_id(text)
                if cid:
                    cur_ch = _new_part(cid, strip_toc_page(text))
                    parts.append(cur_ch)
                    cur_pd = cur_sd = None
            elif text.startswith('Endnote'):
                break
            continue

        if style == 'toc 3':
            if text.startswith('Part'):
                pid = toc_part_id(text)
                if pid:
                    ensure_ch()
                    cur_pd = _new_div(pid, strip_toc_page(text))
                    cur_ch['divisions'].append(cur_pd)
                    cur_sd = None
            continue

        if style == 'toc 4':
            sd = toc_subdiv_id(text)
            if sd is not None:
                ensure_pd()
                cur_sd = _new_subdiv(sd, strip_toc_page(text))
                cur_pd['subdivisions'].append(cur_sd)
            else:
                did = toc_div_id(text)
                if did is not None:
                    ensure_pd()
                    cur_sd = _new_subdiv(did, strip_toc_page(text))
                    cur_pd['subdivisions'].append(cur_sd)
            continue

        if style == 'toc 5':
            num = extract_num(text)
            if num:
                ensure_pd()
                entry = {'id': num, 'title': clean(strip_toc_page(extract_title(text)))}
                target = cur_sd if cur_sd is not None else cur_pd
                target['sections'].append(entry)
            continue

    return parts


def build_body_map(doc: Document) -> dict:
    """(section_id, para_index) -> {title, content} for one volume."""
    body_map: dict = {}
    current_section_id = None
    current_title = None
    current_para = None
    content_lines: list = []
    in_body = False

    def flush() -> None:
        nonlocal content_lines
        if current_section_id is not None and current_para is not None:
            body_map[(current_section_id, current_para)] = {
                'title': current_title,
                'content': _flush_content(content_lines),
            }
        content_lines = []

    for i, p in enumerate(doc.paragraphs):
        text = para_text(p).strip()
        style = p.style.name if p.style else ''

        # Body starts at the long title, or (volumes 2-3, which have no long
        # title) at the Schedule's ActHead 1.
        if not in_body:
            if style == 'LongT' or (style == 'ActHead 1' and text.startswith('Schedule')):
                in_body = True
            else:
                continue

        if style.startswith('ENotes') or style.startswith('EndNotes'):
            flush()
            break

        if style == 'ActHead 5':
            flush()
            num = extract_num(text)
            if num:
                current_section_id = num
                current_title = clean(extract_title(text))
                current_para = i
            else:
                current_section_id = current_title = current_para = None
            continue

        if style in ('ActHead 1', 'ActHead 2', 'ActHead 3', 'ActHead 4'):
            # A container boundary always terminates the section above it, so
            # heading furniture never leaks into the previous section's body.
            flush()
            current_section_id = current_title = current_para = None
            continue

        if current_section_id is not None:
            content_lines.append((style, para_text(p)))

    flush()
    return body_map


def _flush_content(lines: list) -> str:
    md_lines = []
    for style, text in lines:
        t = text.strip()
        if not t:
            if md_lines and md_lines[-1] != '':
                md_lines.append('')
            continue
        cleaned = clean(t)
        if style == 'Definition':
            md_lines.append(f'**{cleaned}**')
        elif style in ('note(text)', 'note(para)', 'note(margin)'):
            md_lines.append(f'  > {cleaned}')
        elif style == 'Penalty':
            # The DOCX already carries the full penalty label inline — bare
            # "Penalty:", but also "Penalty (aggravated offence):" and "Penalty
            # for a contravention of this subsection:". Blindly prepending
            # produced "Penalty: Penalty:" (a defect present in ~700 corpus
            # files). Keep the source label, normalise only the tab after it,
            # and synthesise a label solely when the text has none.
            m = re.match(r'(?i)^(penalty[^:\t]*:)[\t ]*(.*)$', cleaned, re.S)
            if m:
                md_lines.append(f'{m.group(1)} {m.group(2)}'.rstrip())
            else:
                md_lines.append(f'Penalty: {cleaned}')
        elif style == 'SubsectionHead':
            md_lines.append(f'**{cleaned}**')
        else:
            md_lines.append(cleaned)

    result: list = []
    prev_blank = False
    for line in md_lines:
        if line == '':
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        result.append(line)
    while result and result[0] == '':
        result.pop(0)
    while result and result[-1] == '':
        result.pop()
    return '\n'.join(result)


def write_section(sec_dir, sec_id, sec_title, body_text, act_name, compilation_no,
                  compilation_date, part_id, part_title, div_id, div_title,
                  subdiv_id=None, subdiv_title=None):
    sec_dir.mkdir(parents=True, exist_ok=True)
    path = sec_dir / f"{sec_id}.md"
    with open(path, 'w', encoding='utf-8') as f:
        f.write("---\n")
        f.write(f'act: "{act_name}"\n')
        f.write(f'part: "{part_id}"\n')
        f.write(f'part_title: "{part_title}"\n')
        f.write(f'division: "{div_id}"\n')
        f.write(f'division_title: "{div_title}"\n')
        if subdiv_id is not None:
            f.write(f'subdivision: "{subdiv_id}"\n')
            f.write(f'subdivision_title: "{subdiv_title}"\n')
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


def main():
    ap = argparse.ArgumentParser(description="Ingest the Criminal Code Act 1995")
    ap.add_argument("--docx", action='append', required=True,
                    help="one DOCX per volume; repeat the flag per volume")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--act-name", required=True)
    ap.add_argument("--compilation-no", required=True)
    ap.add_argument("--compilation-date", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    section_dir = out_dir / "sections"
    out_dir.mkdir(parents=True, exist_ok=True)

    parts: list[dict] = []
    body_by_vol: dict = {}
    dup_ids: dict = {}
    owner: dict = {}

    for vi, docx_path in enumerate(args.docx):
        print(f"Reading DOCX: {docx_path}")
        doc = Document(str(docx_path))
        vparts = parse_toc(doc)
        print(f"  toc: {len(vparts)} top-level container(s)")
        for k in range(len(parts), len(parts) + len(vparts)):
            owner[k] = vi
        parts.extend(vparts)
        bm = build_body_map(doc)
        print(f"  body: {len(bm)} section block(s)")
        for sid, _ in bm:
            dup_ids[sid] = dup_ids.get(sid, 0) + 1
        body_by_vol[vi] = bm

    tree = {
        "act": args.act_name,
        "compilation_no": args.compilation_no,
        "compilation_date": args.compilation_date,
        "parts": [],
    }
    total_sections = 0
    empty_body: list[str] = []

    for pi, part in enumerate(parts):
        pid = part['id']
        part_entry = {"id": pid, "title": part['title'], "divisions": []}
        vbm = body_by_vol[owner[pi]]

        for div in part['divisions']:
            did = div['id']
            div_entry = {"id": did, "title": div['title'],
                         "subdivisions": [], "sections": []}

            def emit(sec, did, dtitle, sdir, sdid=None, sdt=None):
                nonlocal total_sections
                sid = sec['id']
                info = _lookup(vbm, sid)
                st = info.get('title') or sec['title']
                body_text = info.get('content', '')
                if not body_text:
                    empty_body.append(sid)
                sec_path = f"part-{pid}/division-{did}/{sid}.md"
                write_section(sdir, sid, st, body_text, args.act_name,
                              args.compilation_no, args.compilation_date,
                              pid, part['title'], did, dtitle, sdid, sdt)
                total_sections += 1
                return {"id": sid, "title": st, "path": sec_path}

            for sec in div['sections']:
                div_entry['sections'].append(
                    emit(sec, did, div['title'],
                         section_dir / f"part-{pid}" / f"division-{did}"))

            for subdiv in div['subdivisions']:
                sd_entry = {"id": subdiv['id'], "title": subdiv['title'],
                            "sections": []}
                for sec in subdiv['sections']:
                    sd_entry['sections'].append(
                        emit(sec, did, div['title'],
                             section_dir / f"part-{pid}" / f"division-{did}",
                             subdiv['id'], subdiv['title']))
                div_entry['subdivisions'].append(sd_entry)

            part_entry['divisions'].append(div_entry)
        tree['parts'].append(part_entry)

    tree_path = out_dir / "tree.json"
    with open(tree_path, 'w', encoding='utf-8') as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print(f"Written: {tree_path}")

    sec_files = list(section_dir.rglob("*.md"))
    n_div = sum(len(p['divisions']) for p in tree['parts'])
    n_sub = sum(len(d['subdivisions']) for p in tree['parts'] for d in p['divisions'])
    print("\n" + "=" * 60 + "\nSUMMARY\n" + "=" * 60)
    print(f"Top-level containers (Chapters): {len(tree['parts'])}")
    print(f"Divisions  (Parts):              {n_div}")
    print(f"Subdivisions (Divisions):        {n_sub}")
    print(f"Section files written:           {total_sections}")
    print(f"Section files on disk:           {len(sec_files)}")
    print(f"Sections with empty body:        {len(empty_body)}")
    if empty_body:
        print(f"  e.g. {empty_body[:15]}")
    dupes = {k: v for k, v in dup_ids.items() if v > 1}
    print(f"Duplicate section ids per volume:{len(dupes)}")
    if dupes:
        print(f"  {list(dupes.items())[:10]}")


def _lookup(bm: dict, sid: str) -> dict:
    """First body block for a section id within one volume."""
    best = None
    best_key = None
    for k, v in bm.items():
        if k[0] == sid:
            if best_key is None or k[1] < best_key:
                best_key, best = k[1], v
    return best or {}


if __name__ == "__main__":
    main()
