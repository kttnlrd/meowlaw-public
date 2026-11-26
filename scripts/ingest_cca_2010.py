#!/usr/bin/env python3
"""Ingest the Competition and Consumer Act 2010 (Cth) into the legislation corpus.

WHY A DEDICATED SCRIPT (scripts/ingest_fr_act.py cannot ingest this Act)

  1. Compilation 167 ships as FOUR volume DOCX files, each with its own
     Contents block.  There is no single whole-of-Act DOCX, so the
     `.../text/original/word` URL that works for one-volume Acts 404s (and the
     register-ID form of that URL is wrong for every Act — the path takes the
     TITLE id, C2004A00109, plus a volume number).
  2. Top-level divisions are Roman-numeral Parts ("Part IV"), not "Part 4".
     ingest_fr_act.py matches only `Part\\s+(\\d+[A-Z]?)`, so on this Act it
     finds zero parts.
  3. Section numbers carry multi-letter suffixes of up to 3 characters
     (45AIA, 51ABK, 53ZZC) and Part X uses dotted numbers (10.01A).  The
     generic `extract_num()` regex `(\\d+[A-Z]?)(?:\\s{2,}|\\t)` rejects every
     one of them.
  4. Schedule 2 is the Australian Consumer Law — a whole embedded Act with its
     own Chapter -> Part -> Division numbering and its own section series
     (1..310) that collides with the main Act's.  ingest_fr_act.py's
     "Chapter" branch emits ACL chapters as top-level parts with ids "1".."6",
     which would overwrite the main Act's Chapter 1..8 section paths.
  5. The volume TOCs repeat structure across volumes (Chapter 4 opens both
     vol 1 and vol 2; Chapter 6 opens vol 2 and vol 3) and vol 4's TOC lists
     Schedule 1's "Part 1" and the ACL's parts under the same heading styles,
     so TOC-driven positional matching is ambiguous.

The tree is therefore built from the BODY headings (paragraph styles
ActHead 1..5), which are consistent, ordered and authoritative:

    ActHead 1  Chapter 1—Preliminary    |  Schedule 2—The Australian Consumer Law
    ActHead 2  Part I—Preliminary       |  Chapter 2—General protections       (ACL)
    ActHead 3  Division 1—Preliminary   |  Part 2-1—Misleading or deceptive…   (ACL)
    ActHead 4  Subdivision A—…          |  Division 1—Misleading conduct       (ACL)
    ActHead 5  45AA  Simplified outline |  18  Misleading or deceptive conduct

The heading KEYWORD (Chapter/Part/Division/Subdivision/Schedule), not the
style level, determines the level — the source mis-styles a few headings
(the ACL's "Part 9" is styled ActHead 2 while its siblings are ActHead 3).

CONTAINER MAPPING

tree.json allows part -> division -> subdivision -> section: three container
levels.  Each region of the Act has four, so the level that merely regroups is
folded into `chapter` frontmatter:

    region      tree part     division slot   subdivision slot   folded
    main Act    Part (Roman)  Division        Subdivision        Chapter
    Schedule 1  schedule-1    Part 1          Division           Subdivision
    Schedule 2  schedule-2    ACL Part        ACL Division       ACL Subdivision

The folded main-Act/ACL Chapter is recoverable from the Part number and is
recorded in frontmatter.  Section ids are kept exactly as printed, including
the collisions between the main Act series and the ACL series (section 18
exists in both); `tree order decides` for the id-keyed API lookup, main Act
first.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

_ACT = "Competition and Consumer Act 2010"

HEADING_STYLE_RE = re.compile(r"^ActHead ([1-5])$")
SKIP_STYLE_RE = re.compile(
    r"^(Special TOC|toc |ENotes|Header|Footer|ShortT|CompiledActNo)"
)
# Paragraph styles that carry body text, mapped to their markdown depth:
# 0 = bold marker, no quote; 1 = "> "; 2 = "> > "; 3 = "> > > ".
BODY_STYLE_DEPTH = {
    "subsection": 0,
    "subsection2": 0,
    "paragraph": 1,
    "paragraph(sub)": 2,
    "paragraph(sub-sub)": 3,
    "SO Para": 1,
}
NOTE_PARAGRAPH_STYLES = {"note(para)"}
MARKER_RE = re.compile(r"^\(([0-9A-Za-z]{1,4})\)[\s\u00a0]*")

HEADING_PATTERNS = [
    ("chapter", re.compile(r"^Chapter\s+([0-9]+[A-Z]?)\s*[\u2014\-\u2013]\s*(.*)$")),
    ("schedule", re.compile(r"^Schedule\s+([0-9]+[A-Z]?)\s*[\u2014\-\u2013]\s*(.*)$")),
    ("part", re.compile(r"^Part\s+([0-9A-Z][0-9A-Z\-]*)\s*[\u2014\-\u2013]\s*(.*)$")),
    ("division", re.compile(r"^Division\s+([0-9]+[A-Z]*)\s*[\u2014\-\u2013]\s*(.*)$")),
    ("subdivision", re.compile(r"^Subdivision\s+([A-Z]+)\s*[\u2014\-\u2013]\s*(.*)$")),
]
LABEL = {"chapter": "Chapter", "schedule": "Schedule", "part": "Part",
         "division": "Division", "subdivision": "Subdivision"}

# level -> container depth inside the current top-level container.
# Depth 0 is the tree part; 1 the division slot; 2 the subdivision slot;
# anything >= 3 is folded (recorded in frontmatter, not in the tree).
DEPTH_MAIN = {"part": 0, "division": 1, "subdivision": 2}
DEPTH_SCHEDULE = {"schedule": 0, "part": 1, "division": 2, "subdivision": 3}

SEC_HEAD_RE = re.compile(r"^([0-9][0-9A-Za-z.]*)\s*(.*)$")


def clean(text: str) -> str:
    return text.replace("\u00a0", " ").replace("\t", " ").strip()


def classify_heading(text: str) -> tuple[str, str, str] | None:
    """(level, id, normalised title) for a structural heading, else None."""
    t = clean(text)
    for level, pat in HEADING_PATTERNS:
        m = pat.match(t)
        if m:
            hid = m.group(1)
            rest = m.group(2).strip()
            title = f"{LABEL[level]} {hid} — {rest}" if rest else f"{LABEL[level]} {hid}"
            return level, hid, title
    return None


def emphasised_text(para: Paragraph, drop_leading_italic: bool = False) -> str:
    """Paragraph text with italic runs wrapped in '*' (as the corpus does).

    Adjacent italic runs are merged into one span (Word splits a phrase across
    runs, e.g. 'non' + '-' + 'commercial', which would otherwise emit
    `*non**-**commercial*`), and whitespace at the span edge is hoisted outside
    the asterisks so `the *Criminal Code*` never becomes `the* Criminal Code*`.

    `drop_leading_italic` suppresses the marker on the leading italic run of a
    definition paragraph, matching the plain dictionary style used by the
    GST/ITAA/Corporations dictionaries so `term means …` stays regex-anchored.
    """
    spans: list[tuple[bool, str]] = []          # (is_italic, text)
    for run in para.runs:
        if not run.text:
            continue
        italic = bool(run.italic)
        if spans and spans[-1][0] == italic:
            spans[-1] = (italic, spans[-1][1] + run.text)
        else:
            spans.append((italic, run.text))

    seen_text = False
    out: list[str] = []
    for italic, text in spans:
        if not text:
            continue
        if italic and not (drop_leading_italic and not seen_text):
            lead = text[: len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            core = text.strip()
            out.append(f"{lead}*{core}*{trail}" if core else text)
        else:
            out.append(text)
        if text.strip():
            seen_text = True
    return "".join(out)


def render_paragraph(style: str, raw_text: str) -> str | None:
    """Render one body paragraph to corpus markdown (None drops it)."""
    stripped = raw_text.replace("\u00a0", " ").replace("\t", " ").strip()
    if not stripped:
        return None

    if style == "note(para)":
        # Continuation paragraph inside a note block (Word styles note bodies
        # as note(para), including a note's own (a)/(b)/(c) sub-paragraphs).
        m = MARKER_RE.match(stripped)
        if m:
            return f"> **({m.group(1)})** {stripped[m.end():]}".rstrip()
        return f"> {stripped}"
    if style in ("note(text)", "note(margin)", "noteToPara"):
        m = re.match(r"^(Note\s*\d*|Example\s*\d*)\s*:\s*", stripped)
        if m:
            label = m.group(1)
            rest = stripped[m.end():]
            if label.startswith("Example"):
                return f"> *{label}:* {rest}".rstrip()
            return f"> **{label}:** {rest}".rstrip()
        return f"> {stripped}"
    if style == "Penalty":
        t = re.sub(r"^Penalty\s*:?\s*", "", stripped)
        return f"Penalty: {t}"
    if style == "SubsectionHead":
        return f"**{stripped}**"
    if style in ("Definition", "SO Text"):
        return stripped
    if style == "SO Bullet":
        return re.sub(r"^[\u2022\u25cf\u25aa]\s*", "• ", stripped)

    depth = BODY_STYLE_DEPTH.get(style)
    if depth is not None:
        m = MARKER_RE.match(stripped)
        if m:
            marker, rest = m.group(1), stripped[m.end():]
            if depth == 0:
                return f"**({marker})** {rest}".rstrip()
            return f"{'> ' * depth}**({marker})** {rest}".rstrip()
        return stripped if depth == 0 else f"{'> ' * depth}{stripped}".rstrip()

    return stripped


def render_table(table: Table) -> str:
    """Table content, one row per line, cells joined with ' | '.

    Follows the corpus convention for tabulated provisions (single newline
    between rows; see Corporations Act s 710) rather than inventing pipe
    tables the markdown processor does not understand.  python-docx repeats
    merged cells, so an immediately repeated cell is collapsed.
    """
    rows = []
    for row in table.rows:
        cells = [clean(c.text) for c in row.cells]
        deduped = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
        line = " | ".join(c for c in deduped if c)
        if line:
            rows.append(line)
    return "\n".join(rows)


def iter_blocks(doc: Document):
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


# --------------------------------------------------------------------------
# Container state + tree
# --------------------------------------------------------------------------

class Containers:
    """Resolves (tree part, division, subdivision) for the current heading."""

    def __init__(self, parts: list[dict], index: dict[str, dict]) -> None:
        self.parts = parts
        self.index = index
        self.region = "main"
        self.chapter: str | None = None
        self.chapter_title: str | None = None
        self.part: dict | None = None
        self.division: dict | None = None
        self.subdivision: dict | None = None

    # -- tree part (depth 0) -------------------------------------------------
    def open_part(self, pid: str, title: str) -> dict:
        node = self.index.get(pid)
        if node is None:
            node = {"id": pid, "title": title, "divisions": []}
            self.index[pid] = node
            self.parts.append(node)
        self.part = node
        self.division = None
        self.subdivision = None
        return node

    # -- division slot (depth 1) --------------------------------------------
    def qualify_division_id(self, hid: str) -> str:
        """Keep Schedule 2 (ACL) part ids unique across ACL chapters.

        ACL Parts in Chapters 2-5 are printed chapter-qualified ("Part 2-1").
        Chapter 6's Parts restart at "1", which would collide with the
        synthetic Chapter 1 division, so they are qualified as "6-1".
        """
        if self.region.startswith("schedule-") and self.region != "schedule-1":
            if "-" not in hid and self.chapter:
                return f"{self.chapter}-{hid}"
        return hid

    def open_division(self, did: str, title: str, qualify: bool = True) -> dict:
        assert self.part is not None
        if qualify:
            did = self.qualify_division_id(did)
        for div in self.part["divisions"]:
            if div["id"] == did:
                self.division = div
                self.subdivision = None
                return div
        div = {"id": did, "title": title, "subdivisions": [], "sections": []}
        if self.chapter_title:
            div["chapter"] = self.chapter
            div["chapter_title"] = self.chapter_title
        self.part["divisions"].append(div)
        self.division = div
        self.subdivision = None
        return div

    # -- subdivision slot (depth 2) -----------------------------------------
    def open_subdivision(self, sid: str, title: str) -> dict:
        assert self.division is not None
        for sub in self.division["subdivisions"]:
            if sub["id"] == sid:
                self.subdivision = sub
                return sub
        sub = {"id": sid, "title": title, "sections": []}
        self.division["subdivisions"].append(sub)
        self.subdivision = sub
        return sub

    def division_for_section(self) -> dict:
        """The division slot for a section, creating a fallback if needed."""
        if self.division is not None:
            return self.division
        if self.region == "main" and self.part is not None:
            # Part with no Division level (e.g. Part I's later sections).
            return self.open_division(self.part["id"], self.part["title"],
                                      qualify=False)
        # ACL Chapter with no Part (ACL Chapter 1 — Introduction).
        did = self.chapter or "1"
        title = self.chapter_title or f"Chapter {did}"
        return self.open_division(did, title, qualify=False)


# --------------------------------------------------------------------------
# Section writing
# --------------------------------------------------------------------------

class Writer:
    def __init__(self, out_dir: Path, compiled: dict) -> None:
        self.out_dir = out_dir
        self.compiled = compiled
        self.written: set[str] = set()
        self.dupes: list[str] = []

    def write(self, cont: Containers, sec_id: str, sec_title: str, content: str) -> None:
        div = cont.division_for_section()
        sub = cont.subdivision
        part = cont.part
        if part is None:
            # Defensive: a section heading before any container is ignored.
            return
        if sub is not None:
            rel = (Path(f"part-{part['id']}") / f"division-{div['id']}"
                   / f"subdivision-{sub['id']}" / f"{sec_id}.md")
        else:
            rel = Path(f"part-{part['id']}") / f"division-{div['id']}" / f"{sec_id}.md"
        path = self.out_dir / "sections" / rel
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = ["---", f'act: "{_ACT}"']
        if cont.chapter_title:
            lines.append(f'chapter: "{cont.chapter}"')
            lines.append(f'chapter_title: "{cont.chapter_title}"')
        lines.append(f'part: "{part["id"]}"')
        lines.append(f'part_title: "{part["title"]}"')
        lines.append(f'division: "{div["id"]}"')
        lines.append(f'division_title: "{div["title"]}"')
        if sub is not None:
            lines.append(f'subdivision: "{sub["id"]}"')
            lines.append(f'subdivision_title: "{sub["title"]}"')
        lines.append(f'section: "{sec_id}"')
        lines.append(f'section_title: "{sec_title}"')
        lines.append(f'compilation_no: {self.compiled["no"]}')
        lines.append(f'compilation_date: "{self.compiled["date"]}"')
        lines.append(f'register_id: "{self.compiled["register_id"]}"')
        lines.append("---")
        lines.append("")
        lines.append(f"# {sec_id} {sec_title}".rstrip())
        if content.strip():
            lines.append("")
            lines.append(content.strip())
        lines.append("")
        lines.append("---")
        lines.append(f"*{_ACT}*")
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")

        key = str(rel)
        if key in self.written:
            self.dupes.append(key)
            return
        self.written.add(key)
        entry = {"id": sec_id, "title": sec_title, "path": key}
        target = sub if sub is not None else div
        target["sections"].append(entry)


# --------------------------------------------------------------------------
# Volume parsing
# --------------------------------------------------------------------------

def parse_volume(docx_path: Path, cont: Containers, writer: Writer) -> dict:
    doc = Document(str(docx_path))
    blocks = list(iter_blocks(doc))
    stats = {"sections": 0, "empty": 0, "tables": 0}

    # Body starts at the long title (vol 1) or the first structural heading
    # (vols 2-4 re-open mid-Act and carry no long title).
    start = None
    for i, blk in enumerate(blocks):
        if not isinstance(blk, Paragraph):
            continue
        name = blk.style.name if blk.style else ""
        if name == "LongT" or HEADING_STYLE_RE.match(name):
            start = i
            break
    if start is None:
        return stats

    state = {"sec_id": None, "sec_title": "", "folded_subdivision": None}
    buf: list[str] = []

    def flush() -> None:
        if state["sec_id"] is None:
            buf.clear()
            return
        content = "\n\n".join(x for x in buf if x)
        writer.write(cont, state["sec_id"], state["sec_title"], content)
        stats["sections"] += 1
        if not content.strip():
            stats["empty"] += 1
        buf.clear()

    for blk in blocks[start:]:
        if isinstance(blk, Table):
            if state["sec_id"] is not None:
                rendered = render_table(blk)
                if rendered:
                    stats["tables"] += 1
                    buf.append(rendered)
            continue

        name = blk.style.name if blk.style else ""
        text = blk.text

        if name.startswith("ENotes") or (name == "Normal" and clean(text) == "Endnotes"):
            break
        if SKIP_STYLE_RE.match(name):
            continue

        head_m = HEADING_STYLE_RE.match(name)
        if head_m:
            if head_m.group(1) == "5":
                flush()
                m = SEC_HEAD_RE.match(clean(text))
                if m:
                    state["sec_id"], state["sec_title"] = m.group(1), clean(m.group(2))
                else:
                    state["sec_id"], state["sec_title"] = clean(text), ""
                continue
            info = classify_heading(text)
            if info is None:
                # ActHead used for non-heading furniture — keep as content.
                if state["sec_id"] is not None:
                    rendered = render_paragraph(name, text)
                    if rendered:
                        buf.append(rendered)
                continue
            level, hid, title = info
            flush()
            state["sec_id"], state["sec_title"] = None, ""
            if level == "chapter":
                cont.chapter, cont.chapter_title = hid, title
                cont.division = cont.subdivision = None
                continue
            depth = (DEPTH_MAIN if cont.region == "main"
                     else DEPTH_SCHEDULE).get(level)
            if level == "schedule":
                cont.region = f"schedule-{hid}"
                cont.chapter = cont.chapter_title = None
                cont.open_part(f"schedule-{hid}", title)
                continue
            if depth is None:
                continue
            if depth == 0:
                cont.open_part(hid, title)
            elif depth == 1:
                cont.open_division(hid, title)
            elif depth == 2:
                cont.open_subdivision(hid, title)
            # depth >= 3 is folded: recorded in frontmatter, not in the tree.
            continue

        if state["sec_id"] is None:
            continue
        rendered = render_paragraph(
            name, emphasised_text(blk, drop_leading_italic=(name == "Definition"))
        )
        if rendered:
            buf.append(rendered)

    flush()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest the Competition and Consumer Act 2010 (multi-volume DOCX)"
    )
    ap.add_argument("--docx", nargs="+", required=True, help="volume DOCX files, in order")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--act-name", default=_ACT)
    ap.add_argument("--compilation-no", required=True)
    ap.add_argument("--compilation-date", required=True)
    ap.add_argument("--register-id", default="")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    (out_dir / "sections").mkdir(parents=True, exist_ok=True)
    compiled = {"no": args.compilation_no, "date": args.compilation_date,
                "register_id": args.register_id}

    parts: list[dict] = []
    cont = Containers(parts, {})
    writer = Writer(out_dir, compiled)
    totals = {"sections": 0, "empty": 0, "tables": 0}

    for docx in args.docx:
        docx = Path(docx)
        print(f"Reading {docx.name} ...")
        stats = parse_volume(docx, cont, writer)
        for k in totals:
            totals[k] += stats[k]
        print(f"  sections={stats['sections']} empty={stats['empty']} "
              f"tables={stats['tables']}")

    tree = {
        "act": args.act_name,
        "compilation_no": int(args.compilation_no),
        "compilation_date": args.compilation_date,
        "register_id": args.register_id,
        "volumes": len(args.docx),
        "parts": parts,
    }
    tree_path = out_dir / "tree.json"
    tree_path.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")

    n_parts = len(parts)
    n_divs = sum(len(p["divisions"]) for p in parts)
    n_subdivs = sum(len(d["subdivisions"]) for p in parts for d in p["divisions"])
    n_secs = sum(len(s["sections"]) for p in parts for d in p["divisions"]
                 for s in d["subdivisions"])
    n_secs += sum(len(d["sections"]) for p in parts for d in p["divisions"])
    on_disk = len(list((out_dir / "sections").rglob("*.md")))

    print("=" * 64)
    print(f"parts={n_parts}  divisions={n_divs}  subdivisions={n_subdivs}")
    print(f"tree sections={n_secs}  written={totals['sections']}  on disk={on_disk}")
    print(f"empty sections={totals['empty']}  tables captured={totals['tables']}")
    if writer.dupes:
        print(f"WARNING: {len(writer.dupes)} duplicate section paths:")
        for d in writer.dupes[:10]:
            print("   ", d)
    print(f"tree.json -> {tree_path}")


if __name__ == "__main__":
    main()
