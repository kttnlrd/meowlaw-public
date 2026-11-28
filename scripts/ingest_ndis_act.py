#!/usr/bin/env python3
"""Chapter-aware FR DOCX ingestion — National Disability Insurance Scheme Act 2013.

Derived from scripts/ingest_fr_act.py, which could not parse this Act correctly.
Two defects were found by comparing the produced tree against the DOCX body:

1. SECTION NUMBERS WITH MULTI-LETTER SUFFIXES WERE DROPPED.
   ingest_fr_act.py's extract_num()/extract_title() use `(\\d+[A-Z]?)` — at most
   ONE optional capital letter. The NDIS Act uses two-letter suffixes
   (32BA, 32KA, 32KB, 59AB-59AF, 73ZA-73ZSG, 181ZA, 202AA). Those 49 sections
   were silently omitted: 322 written vs 371 actually present.
   Fixed to `(\\d+[A-Z]*)`.

2. THE ACT IS CHAPTER-BASED AND PART IDS REPEAT ACROSS CHAPTERS.
   Hierarchy is Chapter -> Part -> Division -> Subdivision -> Section. "Part 1"
   exists in Chapters 1, 3, 4, 5, 6, 6A and 7. ingest_fr_act.py keys Part
   metadata by part id alone and mixes Chapter ids into the same dictionary, so
   every "Part 1" was re-titled with the last title seen in the document
   ("Part 1 — Debt recovery"), and duplicate part ids collided in tree.json.
   Chapters 2 and 6B additionally have sections with no intervening Part.

   Representation follows the existing data/gst-1999/tree.json, the corpus's
   other Chapter->Part->Division->Subdivision Act: top-level entries are
   chapter-part units with id "C-P" (e.g. "3-2"), title = the Part's own
   heading; a chapter that has no Parts becomes a top-level entry whose id is
   the chapter id.

3. The hierarchy is read from the BODY headings (ActHead 1-5), which are
   complete, ordered and unambiguous, instead of from the TOC (toc 1-5).

Usage:
    python3 scripts/ingest_ndis_act.py \
        --docx data/<slug>/raw/<slug>.docx \
        --out-dir data/<slug> \
        --act-name "National Disability Insurance Scheme Act 2013" \
        --compilation-no 27 \
        --compilation-date "19 September 2026"

Produces data/<slug>/tree.json and data/<slug>/sections/part-*/division-*/*.md
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

_EM = "\u2014"
_NBSP = "\u00a0"

_HEAD_RE = re.compile(r'^(Chapter|Part|Division|Subdivision)\s+([0-9]+[A-Z]*|[A-Z]+)\s*[—\-–]\s*(.*)$')
_SEC_RE = re.compile(r'^(\d+[A-Z]*)(?:[\s\t]+)(.*)$')


def clean(text: str) -> str:
    return text.replace(_NBSP, " ").replace(_EM, "\u2014").strip()


def _split_sec(text: str):
    """'32BA  Notice of impairments' -> ('32BA', 'Notice of impairments')."""
    t = clean(text)
    m = _SEC_RE.match(t)
    if not m:
        return None, None
    return m.group(1), clean(m.group(2))


def _flush_content(lines: list) -> str:
    md_lines = []
    for style, text in lines:
        t = text.strip()
        if not t:
            if md_lines and md_lines[-1] != '':
                md_lines.append('')
            continue
        c = clean(t)
        if style == 'TABLE':
            if md_lines and md_lines[-1] != '':
                md_lines.append('')
            md_lines.extend(c.split('\n'))
            md_lines.append('')
            continue
        if style == 'Definition':
            md_lines.append(f'**{c}**')
        elif style in ('note(text)', 'note(para)', 'note(margin)'):
            md_lines.append(f'  > {c}')
        elif style == 'Penalty':
            md_lines.append(f'Penalty: {c}')
        elif style == 'SubsectionHead':
            md_lines.append(f'**{c}**')
        else:
            md_lines.append(c)

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


def table_to_md(tbl) -> str:
    """Render a DOCX table as a GitHub-style pipe table (proven table element, so
    tabulating is correct here — unlike PDF regions, where a run of columns may be
    a formula). Horizontally merged cells repeat in `row.cells`; dedupe by tc."""
    rows = []
    for row in tbl.rows:
        vals, seen = [], set()
        for c in row.cells:
            if id(c._tc) in seen:
                continue
            seen.add(id(c._tc))
            vals.append(' '.join(c.text.split()))
        if any(vals):
            rows.append(vals)
    if not rows:
        return ''
    width = max(len(r) for r in rows)
    if width < 2:
        return ''
    out = []
    for i, r in enumerate(rows):
        r = r + [''] * (width - len(r))
        out.append('| ' + ' | '.join(x.replace('|', '\\|') for x in r) + ' |')
        if i == 0:
            out.append('|' + '---|' * width)
    return '\n'.join(out)


def parse_body(doc: Document):
    """Walk the body in document order (paragraphs AND tables) and return an
    ordered list of section records.

    Each record: dict(chapter_id, chapter_title, part_id, part_title,
                     div_id, div_title, subdiv_id, subdiv_title,
                     sec_id, sec_title, content)
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = doc.element.body

    ch = pt = dv = sd = None
    records = []
    cur = None
    lines = []
    started = False

    def close():
        nonlocal cur, lines
        if cur is not None:
            cur['content'] = _flush_content(lines)
            records.append(cur)
        cur = None
        lines = []

    for el in body.iterchildren():
        if el.tag == qn('w:tbl'):
            if started and cur is not None:
                md = table_to_md(Table(el, doc))
                if md:
                    lines.append(('TABLE', md))
            continue
        if el.tag != qn('w:p'):
            continue

        p = Paragraph(el, doc)
        st = p.style.name if p.style else ''
        text = p.text.strip()

        if not started:
            if st == 'LongT':
                started = True
            continue

        if st == 'ENotesHeading 1' and text.lower().startswith('endnote'):
            break

        if st == 'ActHead 5':
            close()
            sid, stitle = _split_sec(text)
            if sid is None:
                continue
            cur = {
                'chapter_id': ch[0] if ch else None,
                'chapter_title': ch[1] if ch else None,
                'part_id': pt[0] if pt else None,
                'part_title': pt[1] if pt else None,
                'div_id': dv[0] if dv else None,
                'div_title': dv[1] if dv else None,
                'subdiv_id': sd[0] if sd else None,
                'subdiv_title': sd[1] if sd else None,
                'sec_id': sid,
                'sec_title': stitle,
            }
            continue

        if st in ('ActHead 1', 'ActHead 2', 'ActHead 3', 'ActHead 4'):
            close()
            m = _HEAD_RE.match(clean(text))
            if not m:
                continue
            kind, ident, rest = m.group(1), m.group(2), clean(m.group(3))
            label = f"{kind} {ident} \u2014 {rest}" if rest else f"{kind} {ident}"
            if kind == 'Chapter':
                ch, pt, dv, sd = (ident, label), None, None, None
            elif kind == 'Part':
                pt, dv, sd = (ident, label), None, None
            elif kind == 'Division':
                dv, sd = (ident, label), None
            else:
                sd = (ident, label)
            continue

        if cur is not None and st != 'Header':
            lines.append((st, p.text))

    close()
    return records


def part_key(r):
    """Top-level tree entry id: 'C-P' for a Part, else the chapter id (GST convention)."""
    return f"{r['chapter_id']}-{r['part_id']}" if r['part_id'] else r['chapter_id']


def div_key(r):
    """Division id within the part node; a Part (or chapter) with no Division gets a
    synthetic one keyed by its own number, mirroring ingest_fr_act.py."""
    return r['div_id'] or (r['part_id'] or r['chapter_id'])


def build_tree(records, act_name, compilation_no, compilation_date):
    parts = []
    index = {}

    def part_node(r):
        key = part_key(r)
        title = r['part_title'] if r['part_id'] else r['chapter_title']
        if key not in index:
            node = {
                'id': key,
                'title': title,
                'chapter': r['chapter_id'],
                'chapter_title': r['chapter_title'],
                'divisions': [],
            }
            index[key] = node
            parts.append(node)
        return index[key]

    def div_node(pnode, r):
        did = div_key(r)
        title = r['div_title'] or pnode['title']
        for d in pnode['divisions']:
            if d['id'] == did:
                return d
        node = {'id': did, 'title': title, 'subdivisions': [], 'sections': []}
        pnode['divisions'].append(node)
        return node

    for r in records:
        pnode = part_node(r)
        dnode = div_node(pnode, r)
        path = f"part-{pnode['id']}/division-{dnode['id']}/{r['sec_id']}.md"
        entry = {'id': r['sec_id'], 'title': r['sec_title'], 'path': path}
        if r['subdiv_id'] is None:
            dnode['sections'].append(entry)
        else:
            sub = None
            for s in dnode['subdivisions']:
                if s['id'] == r['subdiv_id']:
                    sub = s
                    break
            if sub is None:
                sub = {'id': r['subdiv_id'], 'title': r['subdiv_title'], 'sections': []}
                dnode['subdivisions'].append(sub)
            sub['sections'].append(entry)

    tree = {
        'act': act_name,
        'compilation_no': compilation_no,
        'compilation_date': compilation_date,
        'parts': parts,
    }
    return tree, parts


def write_section(sec_dir, r, act_name, compilation_no, compilation_date,
                  part_node, div_id, div_title):
    sec_dir.mkdir(parents=True, exist_ok=True)
    path = sec_dir / f"{r['sec_id']}.md"
    with open(path, 'w', encoding='utf-8') as f:
        f.write("---\n")
        f.write(f'act: "{act_name}"\n')
        if r['chapter_id']:
            f.write(f'chapter: "{r["chapter_id"]}"\n')
            f.write(f'chapter_title: "{r["chapter_title"]}"\n')
        f.write(f'part: "{part_node["id"]}"\n')
        f.write(f'part_title: "{part_node["title"]}"\n')
        f.write(f'division: "{div_id}"\n')
        f.write(f'division_title: "{div_title}"\n')
        if r['subdiv_id']:
            f.write(f'subdivision: "{r["subdiv_id"]}"\n')
            f.write(f'subdivision_title: "{r["subdiv_title"]}"\n')
        f.write(f'section: "{r["sec_id"]}"\n')
        f.write(f'section_title: "{r["sec_title"]}"\n')
        f.write(f"compilation_no: {compilation_no}\n")
        f.write(f'compilation_date: "{compilation_date}"\n')
        f.write("---\n\n")
        f.write(f"# {r['sec_id']} {r['sec_title']}\n\n")
        if r['content']:
            f.write(r['content'])
            f.write("\n")
        f.write("\n---\n")
        f.write(f"*{act_name}*\n")


def main():
    ap = argparse.ArgumentParser(description="Chapter-aware FR DOCX act ingestion")
    ap.add_argument("--docx", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--act-name", required=True)
    ap.add_argument("--compilation-no", required=True)
    ap.add_argument("--compilation-date", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    section_dir = out_dir / "sections"

    print(f"Reading DOCX: {args.docx}")
    doc = Document(args.docx)

    print("Parsing body headings (ActHead 1-5)...")
    records = parse_body(doc)
    print(f"Found {len(records)} body sections")

    tree, parts = build_tree(records, args.act_name, args.compilation_no,
                             args.compilation_date)

    node_by_key = {p['id']: p for p in parts}
    for r in records:
        pnode = node_by_key[part_key(r)]
        dtitle = r['div_title'] or pnode['title']
        sec_dir = section_dir / f"part-{pnode['id']}" / f"division-{div_key(r)}"
        write_section(sec_dir, r, args.act_name, args.compilation_no,
                      args.compilation_date, pnode, div_key(r), dtitle)

    tree_path = out_dir / "tree.json"
    with open(tree_path, 'w', encoding='utf-8') as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print(f"Written: {tree_path}")

    sec_files = list(section_dir.rglob("*.md"))
    print("\n" + "=" * 60 + "\nSUMMARY\n" + "=" * 60)
    print(f"Chapters: {len({r['chapter_id'] for r in records})}")
    print(f"Parts (top-level entries): {len(parts)}")
    print(f"Divisions: {sum(len(p['divisions']) for p in parts)}")
    print(f"Subdivisions: {sum(len(d['subdivisions']) for p in parts for d in p['divisions'])}")
    print(f"Section files written: {len(records)}")
    print(f"Section files on disk: {len(sec_files)}")
    print("Done.")


if __name__ == "__main__":
    main()
