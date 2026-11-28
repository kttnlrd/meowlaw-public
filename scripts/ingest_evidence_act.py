#!/usr/bin/env python3
"""Chapter-aware FR DOCX ingestion — Evidence Act 1995.

Adapted from scripts/ingest_ndis_act.py (which in turn derives from
scripts/ingest_fr_act.py).  ``ingest_fr_act.py`` cannot parse this Act:

1. Its TOC regexes (``Part\\s+(\\d+[A-Z]?)[—]``) do not match dotted Part numbers
   ("Part 1.1—Formal matters"), so Parts 1.1 ... 4.6 were silently dropped and
   every section was re-filed under its Chapter; Part ids were also keyed by
   number alone, so "Part 1—Definitions" (Dictionary) overwrote "Chapter 1" and
   the Dictionary's items 1-11 collided with the Act's own ss 1-11 (12 section
   files were overwritten; s 1 "Short title" was lost from the tree).
2. Body content was collected by style 'ActHead 5' only, so the unnumbered
   Schedule (Oaths and Affirmations) and Dictionary Part 1 (76 definitions)
   leaked into s 197.

Representation (same convention as data/gst-1999 and data/ndis-act-2013, the
corpus's other Chapter -> Part -> Division -> Subdivision Acts):
  * top-level tree entries are the Act's Parts, id = the printed part number
    ("1.1", "3.10"), title = the Part's own heading, plus `chapter` /
    `chapter_title`;
  * a Chapter with no Parts (Chapter 5) becomes a top-level entry whose id is
    the chapter number;
  * ``divisions`` are the Act's Divisions, ``subdivisions`` its Subdivisions;
  * the unnumbered top-level items Schedule and Dictionary become entries
    'schedule' and 'dictionary-1' / 'dictionary-2';
  * Dictionary Part 2 items are re-numbered from 1, so their section ids are
    namespaced 'dict-1' ... 'dict-8A' — otherwise they would shadow the Act's
    own ss 1-12 in tree lookups (backend find_section_path returns the first
    match, so /s1 would have resolved the Dictionary item);
  * the Schedule's and Dictionary Part 1's unnumbered text is captured as one
    synthetic section each ('schedule', 'dictionary');
  * DOCX tables are emitted as markdown pipe rows attached to the section they
    follow (ss 5, 170 and 182 carry substantive tables).

Usage:
    python3 scripts/ingest_evidence_act.py \\
        --docx data/evidence-act-1995/raw/evidence-act-1995.docx \\
        --out-dir data/evidence-act-1995 \\
        --act-name "Evidence Act 1995" \\
        --compilation-no 35 \\
        --compilation-date "10 June 2025"

Produces data/<slug>/tree.json and data/<slug>/sections/part-*/division-*/*.md
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

_EM = "\u2014"
_NBSP = "\u00a0"

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def para_text(paragraph) -> str:
    """Full text of a paragraph, including runs python-docx skips.

    ``Paragraph.text`` walks only direct ``w:r`` children, so (a) text inside
    ``w:smartTag`` wrappers disappears and (b) a hyphenated word whose hyphen is
    a ``w:noBreakHyphen`` element loses the rest of the word ("re-examination"
    -> "re-").  Both occur in this DOCX (55 smartTags, 142 no-break hyphens):
    the terms 'self-incrimination', 'cross-examination', 'first-hand',
    'Torres Strait Islander', 'Australia', 'Norfolk Island' are affected.
    """
    parts = []
    for el in paragraph._p.iter():
        tag = el.tag
        if tag == _W + 't':
            parts.append(el.text or '')
        elif tag == _W + 'tab':
            parts.append('\t')
        elif tag == _W + 'br':
            parts.append('\n')
        elif tag == _W + 'noBreakHyphen':
            parts.append('-')
    return ''.join(parts)

# Parts may be dotted (Evidence Act: "Part 1.1"), Divisions may carry letters
# ("Division 1C"), Subdivisions are letters.
_HEAD_RE = re.compile(
    r'^(Chapter|Part|Division|Subdivision)\s+'
    r'([0-9]+(?:\.[0-9]+)?[A-Z]*|[A-Z]+)\s*[—\-–]\s*(.*)$'
)
_SEC_RE = re.compile(r'^(\d+[A-Z]*)(?:[\s\t]+)(.*)$')

# Unnumbered top-level items that sit outside the Chapter/Part numbering.
_SCHEDULE_RE = re.compile(r'^Schedule\s*[—\-–]\s*(.*)$')
_DICTIONARY_RE = re.compile(r'^Dictionary\s*$')


def clean(text: str) -> str:
    return text.replace(_NBSP, " ").replace(_EM, "\u2014").strip()


def _dir_seg(value: str) -> str:
    """Dotted part numbers are safe in the tree but ugly in a directory name."""
    return value.replace('.', '-')


def _split_sec(text: str):
    """'192A  Advance rulings and findings' -> ('192A', 'Advance rulings...')."""
    m = _SEC_RE.match(clean(text))
    if not m:
        return None, None
    return m.group(1), clean(m.group(2))


# House markdown formatting (scripts/ingest_corps_act.py `_INDENT_PATTERNS`) —
# the backend does not auto-format markers, so they are baked in here.
_INDENT_PATTERNS = [
    (re.compile(r'^\((\d+[A-Z]*)\)\s+(.*)'), r'**(\1)** \2'),        # (1), (1A) -> bold
    (re.compile(r'^\(([ivxlcdm]{2,}|i)\)\s+(.*)'), r'> > **(\1)** \2'),  # (i), (ii) -> double quote
    (re.compile(r'^\(([a-z]{1,3})\)\s+(.*)'), r'> **(\1)** \2'),      # (a), (aa) -> quote
    (re.compile(r'^\(([A-Z])\)\s+(.*)'), r'> > > **(\1)** \2'),       # (A), (B) -> triple quote
    (re.compile(r'^(Note\s*\d*:)\s+(.*)'), r'> **\1** \2'),           # Note: -> quote
    (re.compile(r'^(Example\s*\d*:)\s+(.*)'), r'> *\1* \2'),          # Example: -> quote
]


def format_line(style: str, text: str) -> str:
    """Apply the house markdown formatting to one DOCX paragraph."""
    c = clean(text.replace('\t', ' '))
    c = re.sub(r' {2,}', ' ', c).strip()
    if not c:
        return ''
    if style == 'Definition':
        return f'**{c}**'
    for pattern, repl in _INDENT_PATTERNS:
        m = pattern.match(c)
        if m:
            return pattern.sub(repl, c)
    if style in ('note(text)', 'note(para)', 'note(margin)'):
        return f'> {c}'
    return c


def _flush_content(lines: list) -> str:
    """One paragraph = one markdown block; tables stay contiguous."""
    blocks = []
    for style, text in lines:
        if style == '__table__':
            blocks.append('\n'.join(text))
            continue
        line = format_line(style, text)
        if line:
            blocks.append(line)
    return '\n\n'.join(b for b in blocks if b)


def _table_lines(table: Table) -> list:
    """Render a DOCX table as markdown pipe rows (real tables only).

    The FR DOCX marks some table rows with the literal text 'TABLE' / 'TABLE
    TABLE'; those are dropped, along with rows that are entirely empty.
    """
    rows = []
    for row in table.rows:
        cells = [clean(' '.join(para_text(pp) for pp in c.paragraphs)).replace('\n', ' ')
                 for c in row.cells]
        if not any(cells):
            continue
        if all(c in ('TABLE', '') for c in cells):
            continue
        rows.append(cells)
    if len(rows) < 2:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [''] * (width - len(r)) for r in rows]
    if len({len(r) for r in rows}) != 1:
        return []
    out = ['| ' + ' | '.join(rows[0]) + ' |',
           '| ' + ' | '.join('---' for _ in range(width)) + ' |']
    out += ['| ' + ' | '.join(r) + ' |' for r in rows[1:]]
    return out


def iter_block_items(doc: Document):
    for child in doc.element.body.iterchildren():
        if child.tag.endswith('}p'):
            yield 'p', Paragraph(child, doc)
        elif child.tag.endswith('}tbl'):
            yield 't', Table(child, doc)


def parse_body(doc: Document):
    """Walk the body in document order and return section records."""
    ch = pt = dv = sd = None          # (ident, label) tuples
    records: list = []
    cur = None
    lines: list = []

    def close():
        nonlocal cur, lines
        if cur is not None:
            cur['content'] = _flush_content(lines)
            records.append(cur)
        cur = None
        lines = []

    def open_section(sec_id, sec_title, **over):
        nonlocal cur
        cur = {
            'chapter_id': ch[0] if ch else None,
            'chapter_title': ch[1] if ch else None,
            'part_id': pt[0] if pt else None,
            'part_title': pt[1] if pt else None,
            'div_id': dv[0] if dv else None,
            'div_title': dv[1] if dv else None,
            'subdiv_id': sd[0] if sd else None,
            'subdiv_title': sd[1] if sd else None,
            'sec_id': sec_id,
            'sec_title': sec_title,
        }
        cur.update(over)

    blocks = list(iter_block_items(doc))
    start = None
    end = len(blocks)
    for i, (kind, obj) in enumerate(blocks):
        if kind != 'p':
            continue
        st = obj.style.name if obj.style else ''
        if start is None and st == 'LongT':
            start = i + 1
        elif start is not None and st == 'ENotesHeading 1' and para_text(obj).strip().lower().startswith('endnote'):
            end = i
            break
    if start is None:
        raise SystemExit("Could not locate body start (style 'LongT')")

    for kind, obj in blocks[start:end]:
        if kind == 't':
            if cur is not None:
                rows = _table_lines(obj)
                if rows:
                    lines.append(('__table__', rows))
            continue

        st = obj.style.name if obj.style else ''
        text = para_text(obj).strip()

        if st == 'ActHead 5':
            close()
            sid, stitle = _split_sec(text)
            if sid is None:
                continue
            # Dictionary Part 2 re-numbers from 1: namespace so it cannot shadow
            # the Act's own ss 1-12 in tree lookups.
            if ch and ch[0] == 'dictionary':
                sid = f'dict-{sid}'
            open_section(sid, stitle)
            continue

        if st in ('ActHead 1', 'ActHead 2', 'ActHead 3', 'ActHead 4'):
            close()
            plain = clean(text)

            if st == 'ActHead 1':
                m = _SCHEDULE_RE.match(plain)
                if m:
                    rest = clean(m.group(1)) or 'Schedule'
                    ch, pt, dv, sd = ('schedule', f"Schedule {_EM} {rest}"), None, None, None
                    # The Schedule is a form of words with no numbered sections.
                    open_section('schedule', rest)
                    continue
                if _DICTIONARY_RE.match(plain):
                    ch, pt, dv, sd = ('dictionary', 'Dictionary'), None, None, None
                    continue

            if st == 'ActHead 2' and ch and ch[0] == 'dictionary':
                m = re.match(r'^Part\s+([0-9]+[A-Z]*)\s*[—\-–]\s*(.*)$', plain)
                if m:
                    label = f"Part {m.group(1)} {_EM} {clean(m.group(2))}" if m.group(2) else f"Part {m.group(1)}"
                    pt, dv, sd = (m.group(1), label), None, None
                    if clean(m.group(2)).startswith('Definitions'):
                        # Part 1—Definitions: definitions are ordinary
                        # 'Definition' paragraphs with no section headings.
                        open_section('dictionary', 'Definitions')
                    continue

            m = _HEAD_RE.match(plain)
            if not m:
                continue
            kind_, ident, rest = m.group(1), m.group(2), clean(m.group(3))
            label = f"{kind_} {ident} {_EM} {rest}" if rest else f"{kind_} {ident}"
            if kind_ == 'Chapter':
                ch, pt, dv, sd = (ident, label), None, None, None
            elif kind_ == 'Part':
                pt, dv, sd = (ident, label), None, None
            elif kind_ == 'Division':
                dv, sd = (ident, label), None
            else:
                sd = (ident, label)
            continue

        if cur is not None and st != 'Header':
            lines.append((st, para_text(obj)))

    close()
    return records


def build_tree(records, act_name, compilation_no, compilation_date):
    parts = []
    index = {}

    def part_node(r):
        cid = r['chapter_id']
        if r['part_id']:
            # Evidence Act part numbers are already chapter-qualified ("1.1");
            # the Dictionary's Parts are not, so they get the chapter prefix.
            key = r['part_id'] if cid not in (None, 'dictionary') else f"{cid}-{r['part_id']}"
            title = r['part_title']
        else:
            key = cid
            title = r['chapter_title']
        if key not in index:
            node = {
                'id': key,
                'title': title,
                'chapter': cid,
                'chapter_title': r['chapter_title'],
                'divisions': [],
            }
            index[key] = node
            parts.append(node)
        return index[key]

    def div_node(pnode, r):
        did = r['div_id']
        title = r['div_title']
        if did is None:
            # Part (or chapter) has sections with no Division — synthesise one so
            # every section has a home, mirroring ingest_fr_act.py.
            did = r['part_id'] or r['chapter_id']
            title = r['part_title'] or r['chapter_title']
        for d in pnode['divisions']:
            if d['id'] == did:
                return d
        node = {'id': did, 'title': title, 'subdivisions': [], 'sections': []}
        pnode['divisions'].append(node)
        return node

    for r in records:
        pnode = part_node(r)
        dnode = div_node(pnode, r)
        path = (f"part-{_dir_seg(pnode['id'])}/"
                f"division-{_dir_seg(dnode['id'])}/{r['sec_id']}.md")
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
                  part_node, div_title):
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
        f.write(f'division: "{r["div_id"] if r["div_id"] is not None else part_node["id"]}"\n')
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
        if r['part_id']:
            key = (r['part_id'] if r['chapter_id'] not in (None, 'dictionary')
                   else f"{r['chapter_id']}-{r['part_id']}")
        else:
            key = r['chapter_id']
        pnode = node_by_key[key]
        dtitle = r['div_title'] or pnode['title']
        div_id = r['div_id'] or r['part_id'] or r['chapter_id']   # same rule as div_node()
        sec_dir = (section_dir / f"part-{_dir_seg(pnode['id'])}"
                   / f"division-{_dir_seg(div_id)}")
        write_section(sec_dir, r, args.act_name, args.compilation_no,
                      args.compilation_date, pnode, dtitle)

    tree_path = out_dir / "tree.json"
    with open(tree_path, 'w', encoding='utf-8') as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print(f"Written: {tree_path}")

    sec_files = list(section_dir.rglob("*.md"))
    ids = [r['sec_id'] for r in records]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    print("\n" + "=" * 60 + "\nSUMMARY\n" + "=" * 60)
    print(f"Chapters: {len({r['chapter_id'] for r in records})}")
    print(f"Parts (top-level entries): {len(parts)}")
    print(f"Divisions: {sum(len(p['divisions']) for p in parts)}")
    print(f"Subdivisions: {sum(len(d['subdivisions']) for p in parts for d in p['divisions'])}")
    print(f"Section files written: {len(records)}")
    print(f"Section files on disk: {len(sec_files)}")
    print(f"Duplicate section ids: {dupes if dupes else 'none'}")
    print("Done.")


if __name__ == "__main__":
    main()
