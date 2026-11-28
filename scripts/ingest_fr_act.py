#!/usr/bin/env python3
"""Generalized Federal Register DOCX → legislation-explorer ingestion.

Adapted from ingest_aml_act.py (single-pass TOC + body). Handles the standard
FR-of-Legislation DOCX: Part → Division → Subdivision → Section hierarchy,
with Schedule support.

Usage:
    python3 scripts/ingest_fr_act.py \
        --docx data/<slug>/raw/<slug>.docx \
        --out-dir data/<slug> \
        --act-name "Migration Act 1958" \
        --compilation-no 171 \
        --compilation-date "4 June 2026"

Produces:
  - data/<slug>/tree.json
  - data/<slug>/sections/{part-*/division-*}/{section}.md
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from docx import Document

_EM = "\u2014"
_NBSP = "\u00a0"

# Word wraps text in <w:smartTag> elements (e.g. place names, defined terms).
# python-docx's Paragraph.text only walks DIRECT <w:r> children, so anything
# inside a smartTag silently disappears (a section number, a territory name,
# a whole definition). Collect text from every descendant node in reading
# order instead, mapping the run-level pieces exactly as python-docx does.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _collect(el, out: list) -> None:
    for ch in el:
        tag = ch.tag
        if tag == _W + "pPr":          # paragraph properties: tab stops etc., not text
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


_PART_ID = r'(?:\d+|[IVXLCDM]+)[A-Z]*'
# Section numbers: plain (487) or hyphenated for the Insolvency Practice
# Schedule (Bankruptcy), which numbers its sections 1-1, 5-10, 90-35, ...
_SECTION_NUM = r'\d+[A-Z]*(?:-\d+[A-Z]*)*'

# Some Acts (e.g. the ADJR Act 1977) carry no Part at all: the operative
# sections hang directly off the Act. They still need a container in tree.json
# or parse_toc drops every one of them on the floor. Follows the corpus
# convention established by ingest_constitution.py ("Part-less content gets a
# semantic lowercase id mirroring what it holds").
_NO_PART_ID = "sections"
_NO_PART_TITLE = "Sections"


def toc_part_id(text: str) -> str | None:
    m = re.match(r'Part\s+(' + _PART_ID + r')\s*[—\-–]', text)
    return m.group(1) if m else None


def toc_div_id(text: str) -> str | None:
    m = re.match(r'Division\s+(\d+[A-Z]*)\s*[—\-–]', text)
    return m.group(1) if m else None


def toc_subdiv_id(text: str) -> str | None:
    m = re.match(r'Subdivision\s+([A-Z])\s*[—\-–]', text)
    return m.group(1) if m else None


def toc_chapter_id(text: str) -> str | None:
    m = re.match(r'Chapter\s+(\d+[A-Z]?)\s*[—\-–]', text)
    return m.group(1) if m else None


def extract_num(text: str) -> str | None:
    m = re.match(r'(' + _SECTION_NUM + r')(?:\s{2,}|\t)', text)
    return m.group(1) if m else None


def extract_title(text: str) -> str:
    return re.sub(r'^' + _SECTION_NUM + r'(?:\s{2,}|\t)', '', text).strip()


def strip_toc_page(text: str) -> str:
    return re.sub(r'\t\d+$', '', text).strip()


def parse_toc(doc: Document, nest_schedule_parts: bool = False) -> list[dict]:
    parts = []
    current_part = None
    current_div = None
    current_subdiv = None
    current_schedule = None
    in_toc = False

    for p in doc.paragraphs:
        text = para_text(p).strip()
        if not text:
            continue
        style = p.style.name if p.style else ''

        if not in_toc:
            if text == 'Contents':
                in_toc = True
            continue

        if style in ('LongT', 'ActHead'):
            break

        if style == 'toc 1':
            m = re.match(r'Schedule\s+(\d+)\s*[—\-–]', text)
            if m:
                sch_id = m.group(1)
                current_schedule = f'schedule-{sch_id}'
                current_part = {
                    'id': f'schedule-{sch_id}',
                    'title': clean(strip_toc_page(text)),
                    'divisions': [{
                        'id': '1',
                        'title': clean(strip_toc_page(text)),
                        'subdivisions': [],
                        'sections': []
                    }]
                }
                parts.append(current_part)
                current_div = current_part['divisions'][0]
                current_subdiv = None
            continue

        if style == 'toc 2' and text.startswith('Part'):
            pid = toc_part_id(text)
            if pid:
                if (nest_schedule_parts
                        and current_schedule is not None
                        and current_part is not None
                        and current_part['id'] == current_schedule):
                    # A Schedule that carries its own internal Parts (Privacy Act
                    # 1988 Schedules 1 and 2). The Part is a DIVISION of that
                    # schedule, not a sibling top-level Part: making it a sibling
                    # reused ids 1..5 for both schedules, so part-1/division-1/1.md
                    # was written by both and Schedule 2's clauses 1-2 were lost,
                    # and part_titles were overwritten across schedules.
                    if (len(current_part['divisions']) == 1
                            and not current_part['divisions'][0]['sections']):
                        # Drop the placeholder division created by the schedule
                        # heading: it only exists to hold sections that hang
                        # directly off the schedule.
                        current_part['divisions'].clear()
                    current_div = {
                        'id': pid,
                        'title': clean(strip_toc_page(text)),
                        'subdivisions': [],
                        'sections': []
                    }
                    current_part['divisions'].append(current_div)
                    current_subdiv = None
                    continue
                current_schedule = None
                current_part = {
                    'id': pid,
                    'title': clean(strip_toc_page(text)),
                    'divisions': []
                }
                parts.append(current_part)
                current_div = None
                current_subdiv = None
            continue

        if style == 'toc 2' and text.startswith('Chapter'):
            cid = toc_chapter_id(text)
            if cid:
                current_part = {
                    'id': cid,
                    'title': clean(strip_toc_page(text)),
                    'divisions': []
                }
                parts.append(current_part)
                current_div = None
                current_subdiv = None
            continue

        if style == 'toc 2' and text.startswith('Endnotes'):
            break

        if style == 'toc 3':
            if current_part is None:
                continue
            did = toc_div_id(text)
            if did:
                current_div = {
                    'id': did,
                    'title': clean(strip_toc_page(text)),
                    'subdivisions': [],
                    'sections': []
                }
                current_part['divisions'].append(current_div)
                current_subdiv = None
            continue

        if style == 'toc 4':
            if current_div is None:
                continue
            sd = toc_subdiv_id(text)
            if sd:
                current_subdiv = {
                    'id': sd,
                    'title': clean(strip_toc_page(text)),
                    'sections': []
                }
                current_div['subdivisions'].append(current_subdiv)
            continue

        if style == 'toc 5':
            num = extract_num(text)
            if num:
                title = clean(strip_toc_page(extract_title(text)))
                entry = {'id': num, 'title': title}
                if current_subdiv is not None:
                    current_subdiv['sections'].append(entry)
                else:
                    if current_div is None:
                        if current_part is None:
                            # Part-less Act (e.g. the ADJR Act 1977): the operative
                            # sections hang directly off the Act, so give them a
                            # synthetic container instead of dropping every one.
                            current_part = {
                                'id': _NO_PART_ID,
                                'title': _NO_PART_TITLE,
                                'divisions': []
                            }
                            parts.append(current_part)
                        current_div = {
                            'id': current_part['id'],
                            'title': current_part['title'],
                            'subdivisions': [],
                            'sections': []
                        }
                        current_part['divisions'].append(current_div)
                    current_div['sections'].append(entry)
            continue

    return parts


def build_body_map(doc: Document) -> dict:
    body_map = {}
    current_section_id = None
    current_title = None
    current_para = None
    content_lines = []
    in_body = False
    current_schedule = None      # e.g. 'schedule-1' while a schedule block is open
    schedule_para = None
    schedule_title = None
    schedule_lines: list = []

    def _flush_schedule() -> None:
        """Store the captured body of an unnumbered schedule block."""
        nonlocal current_schedule, schedule_para, schedule_title, schedule_lines
        if current_schedule is not None and schedule_lines:
            body_map[(current_schedule, schedule_para)] = {
                'title': schedule_title or current_schedule,
                'content': _flush_content(schedule_lines),
            }
        current_schedule = None
        schedule_para = None
        schedule_title = None
        schedule_lines = []

    for i, p in enumerate(doc.paragraphs):
        text = para_text(p).strip()
        style = p.style.name if p.style else ''

        # body starts at the long title (style LongT, "An Act ...")
        if not in_body and style == 'LongT':
            in_body = True
            continue

        if not in_body:
            continue

        if style == 'ActHead 5':
            if current_section_id is not None and current_para is not None:
                body_map[(current_section_id, current_para)] = {
                    'title': current_title,
                    'content': _flush_content(content_lines)
                }
                content_lines = []
            _flush_schedule()
            num = extract_num(text)
            title = extract_title(text) if num else text
            if num:
                current_section_id = num
                current_title = clean(title)
                current_para = i
            else:
                current_section_id = None
                current_title = None
                current_para = None
            continue

        if style.startswith('ENotes') or style.startswith('EndNotes'):
            # Endnotes are terminal reference material, never part of a section body.
            if current_section_id is not None and current_para is not None:
                body_map[(current_section_id, current_para)] = {
                    'title': current_title,
                    'content': _flush_content(content_lines)
                }
            _flush_schedule()
            break

        if style == 'ActHead 1':
            # A schedule boundary (e.g. "The Schedule"): close off the preceding
            # section so schedule furniture is not appended to it; any schedule
            # sections restart when their own ActHead 5 heading appears.
            if current_section_id is not None and current_para is not None:
                body_map[(current_section_id, current_para)] = {
                    'title': current_title,
                    'content': _flush_content(content_lines)
                }
                content_lines = []
            _flush_schedule()
            current_section_id = None
            # Many schedules carry NO ActHead 5 clause headings at all (Schedules 1
            # and 2 of the ADJR Act are single unnumbered blocks). Without a block
            # keyed on the schedule, that entire body is discarded. Capture it so
            # main() can attach it to the schedule's division.
            m = re.match(r'Schedule\s+(\d+)\s*[—\-–]', text)
            if m:
                current_schedule = f'schedule-{m.group(1)}'
                schedule_para = i
                schedule_title = f"Schedule {m.group(1)}"
                schedule_lines = []
            continue

        if style in ('ActHead 2', 'ActHead 3', 'ActHead 4'):
            continue

        if current_section_id is not None:
            content_lines.append((style, para_text(p)))
        elif current_schedule is not None:
            schedule_lines.append((style, para_text(p)))

    if current_section_id is not None and current_para is not None:
        body_map[(current_section_id, current_para)] = {
            'title': current_title,
            'content': _flush_content(content_lines)
        }
    _flush_schedule()

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
            md_lines.append(f'Penalty: {cleaned}')
        elif style == 'SubsectionHead':
            md_lines.append(f'**{cleaned}**')
        else:
            md_lines.append(cleaned)

    result = []
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


def get_structure_titles(doc: Document) -> tuple[dict, dict, dict]:
    part_titles = {}
    div_titles = {}
    subdiv_titles = {}
    current_part = None
    current_div = None

    for p in doc.paragraphs:
        text = para_text(p).strip()
        style = p.style.name if p.style else ''

        if style == 'ActHead 2':
            m = re.match(r'Part\s+(' + _PART_ID + r')\s*[—\-–]\s*(.*)', text)
            if m:
                pid = m.group(1)
                rest = m.group(2).strip()
                part_titles[pid] = clean(f"Part {pid} — {rest}" if rest else f"Part {pid}")
                current_part = pid
                current_div = None
            else:
                m2 = re.match(r'Schedule\s+(\d+)\s*[—\-–]\s*(.*)', text)
                if m2:
                    current_part = f'schedule-{m2.group(1)}'
                    current_div = None
                else:
                    m3 = re.match(r'Chapter\s+(\d+[A-Z]?)\s*[—\-–]\s*(.*)', text)
                    if m3:
                        pid = m3.group(1)
                        rest = m3.group(2).strip()
                        part_titles[pid] = clean(f"Chapter {pid} — {rest}" if rest else f"Chapter {pid}")
                        current_part = pid
                        current_div = None

        elif style == 'ActHead 3':
            m = re.match(r'Division\s+(\d+[A-Z]*)\s*[—\-–]\s*(.*)', text)
            if m and current_part is not None:
                did = m.group(1)
                rest = m.group(2).strip()
                div_titles[(current_part, did)] = clean(f"Division {did} — {rest}" if rest else f"Division {did}")
                current_div = did
            else:
                m2 = re.match(r'Part\s+(' + _PART_ID + r')\s*[—\-–]\s*(.*)', text)
                if m2 and current_part is not None:
                    did = m2.group(1)
                    rest = m2.group(2).strip()
                    div_titles[(current_part, did)] = clean(f"Part {did} — {rest}" if rest else f"Part {did}")
                    current_div = did

        elif style == 'ActHead 4':
            if current_part is not None and current_div is not None:
                m = re.match(r'Subdivision\s+([A-Z])\s*[—\-–]\s*(.*)', text)
                if m:
                    sd = m.group(1)
                    rest = m.group(2).strip()
                    subdiv_titles[(current_part, current_div, sd)] = clean(f"Subdivision {sd} — {rest}" if rest else f"Subdivision {sd}")

    return part_titles, div_titles, subdiv_titles


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


_PARA_CACHE = {}


def _doc_paras(doc):
    """Cache (text, style) tuples per document.

    _find_body_entry is called once per section and used to re-enumerate
    doc.paragraphs every time (new lxml wrappers each access). For large
    compilations (Privacy Act 1988: ~6k paragraphs) that made ingestion
    take many minutes; caching makes it linear.
    """
    key = id(doc)
    if key not in _PARA_CACHE:
        _PARA_CACHE[key] = [
            (para_text(p).strip(), p.style.name if p.style else '')
            for p in doc.paragraphs
        ]
    return _PARA_CACHE[key]


def _find_body_entry(body_map, section_id, part_id, doc):
    candidates = [(k, v) for k, v in body_map.items() if k[0] == section_id]
    if not candidates:
        return {}
    if len(candidates) == 1:
        return candidates[0][1]
    target_para = None
    for i, (text, style) in enumerate(_doc_paras(doc)):
        if style == 'ActHead 2':
            m = re.match(r'Part\s+' + re.escape(part_id) + r'\s*[—\-–]', text)
            if m:
                target_para = i
                break
            m = re.match(r'Chapter\s+' + re.escape(part_id) + r'\s*[—\-–]', text)
            if m:
                target_para = i
                break
        if style == 'ActHead 1' and part_id.startswith('schedule-'):
            m = re.match(r'Schedule\s+' + re.escape(part_id.replace('schedule-', '')) + r'\s*[—\-–]', text)
            if m:
                target_para = i
                break
    if target_para is None:
        return candidates[0][1]
    best = None
    best_dist = float('inf')
    for (sid, pnum), v in candidates:
        if pnum >= target_para:
            dist = pnum - target_para
            if dist < best_dist:
                best_dist = dist
                best = v
    return best or candidates[0][1]


def main():
    parser = argparse.ArgumentParser(description="Ingest a Federal Register DOCX act")
    parser.add_argument("--docx", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--act-name", required=True)
    parser.add_argument("--compilation-no", required=True)
    parser.add_argument("--compilation-date", required=True)
    parser.add_argument(
        "--nest-schedule-parts",
        action="store_true",
        help="Treat a 'Part N' heading that appears INSIDE a Schedule as a "
             "division of that schedule instead of a sibling top-level Part. "
             "Off by default: with it off, schemes like Privacy Act 1988 "
             "Schedules 1 and 2 (both numbered Parts 1..n) collide on ids and "
             "section paths.",
    )
    args = parser.parse_args()

    docx_path = Path(args.docx)
    out_dir = Path(args.out_dir)
    section_dir = out_dir / "sections"
    act_name = args.act_name
    compilation_no = args.compilation_no
    compilation_date = args.compilation_date

    print(f"Reading DOCX: {docx_path}")
    doc = Document(str(docx_path))

    print("Parsing TOC...")
    parts = parse_toc(doc, nest_schedule_parts=args.nest_schedule_parts)
    print(f"Found {len(parts)} parts")

    part_titles, div_titles, subdiv_titles = get_structure_titles(doc)

    print("Building section content map...")
    body_map = build_body_map(doc)
    print(f"Found {len(body_map)} body section entries")

    tree = {
        "act": act_name,
        "compilation_no": compilation_no,
        "compilation_date": compilation_date,
        "parts": []
    }
    total_sections = 0

    for part in parts:
        pid = part['id']
        pt = clean(part_titles.get(pid, part['title']))
        part_entry = {"id": pid, "title": pt, "divisions": []}

        # Content captured directly under the schedule heading, before any ActHead 5
        # clause heading: either the whole body of an unnumbered schedule, or a
        # margin note sitting ahead of the first clause. Both must survive.
        pre = [(k, v) for k, v in body_map.items() if k[0] == pid]
        if pre and part['divisions']:
            first_div = part['divisions'][0]
            if not first_div['sections']:
                first_div['sections'].append({'id': pid, 'title': pt})
            else:
                sched_para = pre[0][0][1]
                note = (pre[0][1].get('content') or '').strip()
                first_sid = first_div['sections'][0]['id']
                cands = sorted(k for k in body_map
                               if k[0] == first_sid and k[1] >= sched_para)
                if note and cands:
                    body_map[cands[0]] = {**body_map[cands[0]],
                                          'content': note + "\n\n" + body_map[cands[0]]['content']}

        for div in part['divisions']:
            did = div['id']
            dt = clean(div_titles.get((pid, did), div['title']))
            div_entry = {"id": did, "title": dt, "subdivisions": [], "sections": []}

            for sec in div['sections']:
                sid = sec['id']
                body_info = _find_body_entry(body_map, sid, pid, doc)
                st = body_info.get('title') or clean(sec['title'])
                body_text = body_info.get('content', '')
                sec_path = f"part-{pid}/division-{did}/{sid}.md"
                div_entry['sections'].append({"id": sid, "title": st, "path": sec_path})
                sec_dir = section_dir / f"part-{pid}" / f"division-{did}"
                write_section(sec_dir, sid, st, body_text, act_name, compilation_no,
                              compilation_date, pid, pt, did, dt)
                total_sections += 1

            for subdiv in div['subdivisions']:
                sd_id = subdiv['id']
                sd_title = clean(subdiv_titles.get((pid, did, sd_id), subdiv['title']))
                subdiv_entry = {"id": sd_id, "title": sd_title, "sections": []}
                for sec in subdiv['sections']:
                    sid = sec['id']
                    body_info = _find_body_entry(body_map, sid, pid, doc)
                    st = body_info.get('title') or clean(sec['title'])
                    body_text = body_info.get('content', '')
                    sec_path = f"part-{pid}/division-{did}/{sid}.md"
                    subdiv_entry['sections'].append({"id": sid, "title": st, "path": sec_path})
                    sec_dir = section_dir / f"part-{pid}" / f"division-{did}"
                    write_section(sec_dir, sid, st, body_text, act_name, compilation_no,
                                  compilation_date, pid, pt, did, dt, sd_id, sd_title)
                    total_sections += 1
                div_entry['subdivisions'].append(subdiv_entry)

            part_entry['divisions'].append(div_entry)
        tree['parts'].append(part_entry)

    tree_path = out_dir / "tree.json"
    with open(tree_path, 'w', encoding='utf-8') as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print(f"Written: {tree_path}")

    sec_files = list(section_dir.rglob("*.md"))
    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    print(f"Parts: {len(parts)}")
    print(f"Divisions: {sum(len(p['divisions']) for p in parts)}")
    print(f"Subdivisions: {sum(len(d['subdivisions']) for p in parts for d in p['divisions'])}")
    print(f"Section files written: {total_sections}")
    print(f"Section files on disk: {len(sec_files)}")
    print("Done.")


if __name__ == "__main__":
    main()
