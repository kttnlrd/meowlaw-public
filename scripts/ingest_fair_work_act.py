#!/usr/bin/env python3
"""Chapter/Schedule-aware, multi-volume FR DOCX ingestion — Fair Work Act 2009.

The Fair Work Act 2009 (compilation 73) is published by the Federal Register as
FOUR Word volumes, and its hierarchy is Chapter -> Part -> Division ->
Subdivision -> Section.  ``scripts/ingest_fr_act.py`` cannot ingest it:

1. MULTI-VOLUME.  Only volume 1 carries the ``LongT`` ("An Act ...") marker that
   ``build_body_map`` uses to enter body mode, so volumes 2-4 produce no section
   text at all (see ``scripts/ingest_fr_act_multivol.py`` for the same defect).

2. CHAPTER -> PART IDS.  Parts are named ``Part 2-2`` (chapter 2, part 2) and the
   same Part number repeats in every chapter.  ``toc_part_id`` truncates
   "Part 2-2" to the id ``2`` (its ``_PART_ID`` pattern stops at the first dash),
   so parts from different chapters collide: 8 TOC entries all keyed ``1``/``2``
   in volume 1 alone, and section files from distinct Parts overwrite each other
   under the shared ``part-<n>/division-<n>/`` path.

3. SCHEDULES.  Volume 4 holds Schedules 1-5, each numbered from clause 1 again,
   and Schedule 1 numbers its clauses from 1 again inside every internal Part.
   A schedule therefore has to be its own container, with its internal Parts as
   divisions, or clause files collide.

Representation follows the corpus's other Chapter->Part Acts
(``data/gst-1999/tree.json``, ``data/ndis-act-2013/tree.json``): a top-level
entry is a chapter-part unit with id ``C-P`` ("2-2"); a Part with no Division
gets a synthetic division keyed by its own id; a Schedule is a top-level entry
``schedule-N`` whose divisions are the schedule's internal Parts.

The hierarchy is read from the BODY headings (ActHead 1-5), which are complete,
ordered and unambiguous, rather than from the Contents page.

Usage:
    python3 scripts/ingest_fair_work_act.py \\
        --docx data/fair-work-2009/raw/fair-work-2009-vol1.docx \\
        --docx data/fair-work-2009/raw/fair-work-2009-vol2.docx \\
        --docx data/fair-work-2009/raw/fair-work-2009-vol3.docx \\
        --docx data/fair-work-2009/raw/fair-work-2009-vol4.docx \\
        --out-dir data/fair-work-2009 \\
        --act-name "Fair Work Act 2009" \\
        --compilation-no 73 --compilation-date "7 July 2026"

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

# Chapter/Part ids may be "2-2" or "6-4A" (chapter-part); Division/Subdivision
# ids are "1", "17A", "A".  Anchored on the em/en hyphen or ASCII hyphen that
# always separates the label from the heading text.
_HEAD_RE = re.compile(
    r'^(Chapter|Part|Division|Subdivision|Schedule)\s+'
    r'([0-9]+[A-Z]*(?:-[0-9]+[A-Z]*)*|[A-Z]+)\s*[\u2014\-\u2013]\s*(.*)$'
)
_SEC_RE = re.compile(r'^(\d+[A-Z]*)(?:[\s\t]+)(.*)$')
_HEAD_STYLES = ("ActHead 1", "ActHead 2", "ActHead 3", "ActHead 4", "ActHead 5")


def clean(text: str) -> str:
    return text.replace(_NBSP, " ").replace(_EM, "\u2014").strip()


def split_sec(text: str):
    """'32BA  Notice of impairments' -> ('32BA', 'Notice of impairments')."""
    m = _SEC_RE.match(clean(text))
    if not m:
        return None, None
    return m.group(1), clean(m.group(2))


def flush_content(lines: list) -> str:
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
        elif style in ('note(text)', 'note(para)', 'note(margin)', 'noteToPara'):
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
    """Render a DOCX table as a GitHub-style pipe table.  Merged cells repeat in
    ``row.cells``; dedupe by tc so a span is not duplicated."""
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


def body_start(doc: Document) -> int:
    """Index into doc.paragraphs of the first body paragraph of one volume.

    Volume 1 opens the Act with the LongT ("An Act ..."), which is the marker
    ``ingest_fr_act.build_body_map`` expects.  Volumes 2-4 have no LongT: their
    body starts at the first ActHead heading after the last Contents entry, so
    the volume's front matter and Contents page are not swallowed.
    """
    for i, p in enumerate(doc.paragraphs):
        if (p.style.name or '') == 'LongT':
            return i
    last_toc = -1
    for i, p in enumerate(doc.paragraphs):
        if (p.style.name or '').startswith('toc'):
            last_toc = i
    for i, p in enumerate(doc.paragraphs):
        if i > last_toc and (p.style.name or '') in _HEAD_STYLES:
            return i
    return 0


def parse_volume(doc: Document, start: int, records: list) -> None:
    """Walk one volume's body in document order (paragraphs AND tables), appending
    a section record per ActHead 5 heading, with its enclosing headings attached."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    ch = pt = dv = sd = sch = None
    cur = None
    lines = []
    pi = -1

    def close():
        nonlocal cur, lines
        if cur is not None:
            cur['content'] = flush_content(lines)
            records.append(cur)
        cur = None
        lines = []

    for el in doc.element.body.iterchildren():
        if el.tag == qn('w:tbl'):
            if cur is not None:
                md = table_to_md(Table(el, doc))
                if md:
                    lines.append(('TABLE', md))
            continue
        if el.tag != qn('w:p'):
            continue
        pi += 1
        if pi < start:
            continue

        p = Paragraph(el, doc)
        st = p.style.name if p.style else ''
        text = p.text.strip()

        if st.startswith('ENotes') or st.startswith('EndNotes'):
            close()
            break

        if st == 'ActHead 5':
            close()
            sid, stitle = split_sec(text)
            if sid is None:
                continue
            cur = {
                'chapter_id': ch[0] if ch else None,
                'chapter_title': ch[1] if ch else None,
                'schedule_id': sch[0] if sch else None,
                'schedule_title': sch[1] if sch else None,
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
            if kind == 'Schedule':
                sch, ch, pt, dv, sd = (f"schedule-{ident}", label), None, None, None, None
            elif kind == 'Chapter':
                ch, sch, pt, dv, sd = (ident, label), None, None, None, None
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


def top_key(r) -> str:
    """Top-level tree entry id: 'schedule-N', or the chapter-part unit.

    The Fair Work Act numbers its Parts chapter-first ("Part 2-2" in Chapter 2),
    so the Part heading already carries the chapter and must not be prefixed
    again.  Acts that number Parts per chapter (NDIS: "Part 1" in Chapter 1) get
    the 'C-P' form instead — both end up "2-2" / "1-1".
    """
    if r['schedule_id']:
        return r['schedule_id']
    if r['part_id']:
        if r['chapter_id'] and not r['part_id'].startswith(f"{r['chapter_id']}-"):
            return f"{r['chapter_id']}-{r['part_id']}"
        return r['part_id']
    return r['chapter_id'] or 'sections'


def div_key(r) -> str:
    """Division id within the top-level node.  Must be unique within it.

    Inside a Schedule the schedule's internal Parts are the divisions, and a
    Part's own Divisions are folded into that key ("13-2") because Schedule 1
    renumbers its clauses from 1 inside every Part and Division.
    """
    if r['schedule_id']:
        if r['part_id']:
            return f"{r['part_id']}-{r['div_id']}" if r['div_id'] else r['part_id']
        return r['schedule_id']
    return r['div_id'] or r['part_id'] or r['chapter_id'] or 'sections'


def build_tree(records, act_name, compilation_no, compilation_date):
    parts = []
    index = {}

    for r in records:
        key = top_key(r)
        if key not in index:
            node = {
                'id': key,
                'title': r['schedule_title'] or r['part_title'] or r['chapter_title'],
                'divisions': [],
            }
            if r['chapter_id']:
                node['chapter'] = r['chapter_id']
                node['chapter_title'] = r['chapter_title']
            if r['schedule_id']:
                node['schedule'] = r['schedule_id']
                node['schedule_title'] = r['schedule_title']
            index[key] = node
            parts.append(node)
        pnode = index[key]

        did = div_key(r)
        dnode = None
        for d in pnode['divisions']:
            if d['id'] == did:
                dnode = d
                break
        if dnode is None:
            dnode = {'id': did,
                     'title': r['div_title'] or r['part_title'] or pnode['title'],
                     'subdivisions': [], 'sections': []}
            pnode['divisions'].append(dnode)

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

    tree = {'act': act_name, 'compilation_no': compilation_no,
            'compilation_date': compilation_date, 'parts': parts}
    return tree


def write_section(sec_dir, r, act_name, compilation_no, compilation_date,
                  part_node, div_id, div_title):
    sec_dir.mkdir(parents=True, exist_ok=True)
    with open(sec_dir / f"{r['sec_id']}.md", 'w', encoding='utf-8') as f:
        f.write("---\n")
        f.write(f'act: "{act_name}"\n')
        if r['chapter_id']:
            f.write(f'chapter: "{r["chapter_id"]}"\n')
            f.write(f'chapter_title: "{r["chapter_title"]}"\n')
        if r['schedule_id']:
            f.write(f'schedule: "{r["schedule_id"]}"\n')
            f.write(f'schedule_title: "{r["schedule_title"]}"\n')
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
    ap = argparse.ArgumentParser(description="Chapter/Schedule-aware multi-volume FR DOCX act ingestion")
    ap.add_argument("--docx", action="append", required=True,
                    help="volume DOCX (repeat once per volume, in order)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--act-name", required=True)
    ap.add_argument("--compilation-no", required=True)
    ap.add_argument("--compilation-date", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    section_dir = out_dir / "sections"

    records: list = []
    for vi, path in enumerate(args.docx):
        print(f"Reading volume {vi + 1}/{len(args.docx)}: {path}")
        doc = Document(str(path))
        start = body_start(doc)
        n0 = len(records)
        parse_volume(doc, start, records)
        print(f"  body starts at paragraph {start} of {len(doc.paragraphs)}; "
              f"{len(records) - n0} sections")

    print(f"Found {len(records)} body sections")
    if not records:
        raise SystemExit("No body sections found — check the volume order/front matter.")

    # --- invariants: the defects this script exists to prevent -----------------
    keys = [top_key(r) for r in records]
    assert None not in keys, "a record has no container key"
    dup_path = {}
    for r in records:
        p = f"part-{top_key(r)}/division-{div_key(r)}/{r['sec_id']}.md"
        dup_path.setdefault(p, []).append(r['sec_title'])
    collided = {k: v for k, v in dup_path.items() if len(v) > 1}
    if collided:
        for k, v in list(collided.items())[:20]:
            print(f"  COLLISION {k} <- {v}")
        raise SystemExit(f"{len(collided)} section paths collide")

    tree = build_tree(records, args.act_name, args.compilation_no,
                      args.compilation_date)
    part_ids = [p['id'] for p in tree['parts']]
    assert len(part_ids) == len(set(part_ids)), "duplicate top-level part ids"
    assert all(p['divisions'] for p in tree['parts']), "a top-level part has no divisions"

    node_by_key = {p['id']: p for p in tree['parts']}
    for r in records:
        pnode = node_by_key[top_key(r)]
        did = div_key(r)
        dtitle = r['div_title'] or r['part_title'] or pnode['title']
        sec_dir = section_dir / f"part-{pnode['id']}" / f"division-{did}"
        write_section(sec_dir, r, args.act_name, args.compilation_no,
                      args.compilation_date, pnode, did, dtitle)

    tree_path = out_dir / "tree.json"
    with open(tree_path, 'w', encoding='utf-8') as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print(f"Written: {tree_path}")

    empty = [f"{top_key(r)}/{div_key(r)}/{r['sec_id']}" for r in records
             if not r['content']]
    sec_files = list(section_dir.rglob("*.md"))
    print("\n" + "=" * 60 + "\nSUMMARY\n" + "=" * 60)
    print(f"Volumes: {len(args.docx)}")
    print(f"Top-level parts: {len(tree['parts'])}")
    print(f"Divisions: {sum(len(p['divisions']) for p in tree['parts'])}")
    print(f"Subdivisions: {sum(len(d['subdivisions']) for p in tree['parts'] for d in p['divisions'])}")
    print(f"Section files written: {len(records)}")
    print(f"Section files on disk: {len(sec_files)}")
    print(f"Empty-body sections: {len(empty)}")
    if empty:
        print("  first 15:", empty[:15])
    print("Done.")


if __name__ == '__main__':
    main()
