#!/usr/bin/env python3
"""Ingest the OECD Model Tax Convention commentaries (condensed version 2010) as its own corpus.

Source PDF supplied by the user: title "MTC2010.PDF", pages carry the footer
"MODEL TAX CONVENTION (CONDENSED VERSION) - ISBN 978-92-64-08948-8 - (c) OECD 2010".
No PDF bookmarks, so structure is read from the type: the article heading and the
subheadings are set in Caecilia-Heavy / Caecilia-HeavyItalic, body text in Caecilia-Roman,
running heads and folios small, footnotes smaller still.

Output shape demanded by the ticket: every article is a tree node ("a thing") and every
subheading under an article is its own selectable section file.

  data/oecd-mtc-commentary-2010/sections/art-1-intro.md
  data/oecd-mtc-commentary-2010/sections/art-1-01-application-of-the-convention-to-partnerships.md
  data/oecd-mtc-commentary-2010/tree.json      (parts = articles, sections = subheadings)

Dry run by default; --apply writes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

import fitz

from mtc_layout import note_blocks, table_regions, tables_of

SLUG = "oecd-mtc-commentary-2010"
ROOT = Path(__file__).resolve().parents[2]
# ponytail: parse rules ship, inputs/outputs never do — paths are repo-relative and
# env-overridable; OPTIONAL_DATA_DIR (default <repo>/data) is where outputs land.
OUT = Path(os.environ.get("OPTIONAL_DATA_DIR", ROOT / "data")) / SLUG
DEFAULT_PDF = os.environ.get("MTC2010_PDF", str(ROOT / "source" / "oecd-mtc-commentary-2010" / "MTC2010.PDF"))

ARTICLE_RE = re.compile(r"^COMMENTARY ON ARTICLES?\s+(\d+)\s*([AB])?\b(.*)$")
JOINT_RE = re.compile(r"^COMMENTARY ON ARTICLES?\s+(\d+\s*[A-Z]?)\s+AND\s+(\d+\s*[A-Z]?)\b(.*)$")
FOOTER_RE = re.compile(r"(\(c\) OECD|© OECD|ISBN 978-92-64-08948-8|MODEL TAX CONVENTION \(CONDENSED)")
FOLIO_RE = re.compile(r"^\d{1,3}$")
MARKER_RE = re.compile(r"^(\d+(?:\.\d+)*\.|[a-z]\)|\((?:[a-z]|[ivx]{1,4})\)|[ivx]{1,4}\.)$")
BARE_NUMERAL_RE = re.compile(r"^(([IVXLC]+\.)|([A-Z]\.)|(\([a-z]\)))$")
ANNEX_RE = re.compile(r"^ANNEX\b(.*)$")
ARTICLE_LEVEL_SECTIONS = re.compile(
    r"^(Observations on the Commentary|Reservation[s]? on the Article|Reservations? and Observations)",
    re.I,
)


def lines_of(doc) -> list[dict]:
    """Flatten the PDF into one record per visual line, keeping the dominant type."""
    out = []
    for pno in range(doc.page_count):
        for block in doc[pno].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s["text"].strip()]
                if not spans:
                    continue
                big = max(spans, key=lambda s: s["size"])
                text = " ".join(s["text"].strip() for s in spans)
                text = re.sub(r"\s+", " ", text).strip()
                # italic glyph runs come back as separate spans: "case ( i)" is "case (i)"
                text = re.sub(r"\(\s+([a-zA-Z]{1,4})\s*\)", r"(\1)", text)
                out.append(
                    {
                        "page": pno + 1,
                        "y": round(line["bbox"][1], 1),
                        "text": text,
                        "size": round(big["size"], 1),
                        "font": big["font"],
                    }
                )
    return out


def is_heading(ln: dict) -> bool:
    t, size, font = ln["text"], ln["size"], ln["font"]
    if FOOTER_RE.search(t) or FOLIO_RE.match(t):
        return False
    boldish = "Heavy" in font
    if not boldish:
        return False
    if size >= 9.0:
        return True
    # small bold: article-level sections and table captions
    if size >= 7.2 and ARTICLE_LEVEL_SECTIONS.match(t):
        return True
    return False


def is_running_head(ln: dict) -> bool:
    t, size = ln["text"], ln["size"]
    if size > 7.2:
        return False
    return bool(re.match(r"^(COMMENTARY ON ARTIC|MODEL TAX CONVENTION|Chapter [IVX]+$)", t))


def merge_headings(lines: list[dict]) -> list[dict]:
    """Consecutive bold lines with nothing between them are one wrapped heading."""
    merged: list[dict] = []
    for ln in lines:
        prev = merged[-1] if merged else None
        if (
            prev
            and prev.get("is_heading")
            and ln["page"] in (prev["page"], prev["page"] + 1)
            and abs(ln["size"] - prev["size"]) <= 0.6
            and not ARTICLE_RE.match(ln["text"])
            and not BARE_NUMERAL_RE.match(ln["text"])
            and (
                BARE_NUMERAL_RE.match(prev["text"])
                or re.match(r"^[a-z(]", ln["text"])
                or (prev["text"].isupper() and ln["text"].isupper() and len(prev["text"]) > 2)
            )
        ):
            prev["text"] = f"{prev['text']} {ln['text']}"
            continue
        ln = dict(ln)
        ln["is_heading"] = is_heading(ln)
        merged.append(ln)
    return merged


def article_key(heading: str) -> tuple[str, str, list[int], str]:
    """('1', 'CONCERNING ...', [1], 'Article 1') for a COMMENTARY ON ARTICLE heading."""
    if " AND " in heading:
        m = JOINT_RE.match(heading)
        if m:
            a = m.group(1).replace(" ", "")
            b = m.group(2).replace(" ", "")
            nums = [int(re.match(r"\d+", a).group()), int(re.match(r"\d+", b).group())]
            return f"{a.lower()}-{b.lower()}", m.group(3).strip(), nums, f"Articles {a} and {b}"
    m = ARTICLE_RE.match(heading)
    if m:
        num, letter, rest = m.group(1), m.group(2), m.group(3).strip()
        key = f"{num}{letter.lower()}" if letter else num
        return key, rest, [int(num)], f"Article {key.upper()}"
    raise ValueError(f"not an article heading: {heading!r}")


def clean_subtitle(sub: str) -> str:
    s = re.sub(r"^(CONCERNING|RELATING TO)\s+", "", sub.strip(), flags=re.I)
    s = re.sub(r"\s+OF THE CONVENTION$", "", s, flags=re.I)
    s = s.strip(" .")
    if s.isupper():
        s = s.lower()
        s = re.sub(r"\barticle", "Article", s)
        s = re.sub(r"\boecd\b", "OECD", s)
        s = s[:1].upper() + s[1:]
    return s


def slugify(text: str, limit: int = 44) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > limit:
        s = s[:limit].rsplit("-", 1)[0]
    return s or "section"


def para_text(raw_lines: list[str]) -> str:
    """Join hard-wrapped lines into paragraphs, preserving numbered paragraph markers."""
    paras: list[str] = []
    cur: list[str] = []
    marker: str | None = None
    last_no = 0

    def flush():
        nonlocal cur, marker
        if cur:
            body = " ".join(cur).strip()
            if marker:
                paras.append(f"**{marker}** {body}")
            else:
                paras.append(body)
        cur, marker = [], None

    for ln in raw_lines:
        t = ln.strip()
        if not t:
            continue
        if t.startswith("\x00TBL\x00"):
            flush()
            paras.append(t[len("\x00TBL\x00"):])
            continue
        if MARKER_RE.match(t):
            m_no = re.match(r"^(\d+)\.$", t)
            if m_no and int(m_no.group(1)) <= last_no:
                # The source restarts numbering inside a sub-block ("1." "2." under a lettered
                # heading). Read as a paragraph header it would renumber the commentary, so it
                # becomes a list item instead.
                flush()
                cur.append(f"- **{t}**")
                continue
            flush()
            if m_no:
                last_no = int(m_no.group(1))
            marker = t
            continue
        if cur and cur[-1].endswith("-") and re.match(r"^[a-z]", t):
            # A word hyphenated across the line break keeps its hyphen: "non-" + "employment"
            # is "non-employment", not "nonemployment". Dropping it silently forges new words.
            cur[-1] = cur[-1] + t
            continue
        cur.append(t)
    flush()
    return "\n\n".join(paras)


def extract(pdf: Path) -> list[dict]:
    """Walk the type-set document into parts.

    A part is an article's commentary, or an ANNEX. The annex at p112 reproduces the
    PREVIOUS version of Article 7 and its commentary, so its "COMMENTARY ON ARTICLE 7"
    heading must not be read as a second Article 7 - the part already open is the annex.
    """
    doc = fitz.open(pdf)
    lines = [ln for ln in lines_of(doc) if not FOOTER_RE.search(ln["text"]) and not FOLIO_RE.match(ln["text"])]
    lines = [ln for ln in lines if not is_running_head(ln)]
    # Single-column book with tables floated into the text: block order can put a table ahead of
    # the prose that precedes it, so read the page top-to-bottom instead.
    lines.sort(key=lambda ln: (ln["page"], ln["y"]))
    lines = merge_headings(lines)

    # Tables and footnotes are recovered from page geometry before the text walk: reading order
    # flattens a table into prose and weaves its footnote through the rows, so both have to be
    # taken out of the line stream and put back as structured blocks at the same position.
    layout: dict[int, dict] = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        regions = table_regions(page)
        layout[pno + 1] = {"tables": tables_of(page), "notes": note_blocks(page, regions),
                           "regions": regions}
    emitted: set[tuple[int, int]] = set()
    notes_done: set[tuple[int, int]] = set()

    parts: list[dict] = []
    cur: dict | None = None
    cur_sec: dict | None = None
    in_annex = False
    annex_n = 0

    def new_section(title: str, level: int):
        nonlocal cur_sec
        cur_sec = {"title": title, "level": level, "body": [], "notes": []}
        cur["sections"].append(cur_sec)

    for ln in lines:
        t = ln["text"]
        lay = layout.get(ln["page"], {"tables": [], "notes": []})
        for ti, tb in enumerate(lay["tables"]):
            if (ln["page"], ti) in emitted or not (tb["y0"] - 3 <= ln["y"] <= tb["y1"] + 3):
                continue
            if cur is None:
                continue
            if cur_sec is None:
                new_section("Introduction", 0)
            cur_sec["body"].append("\x00TBL\x00" + tb["markdown"])
            emitted.add((ln["page"], ti))
            break
        if any(tb["y0"] - 3 <= ln["y"] <= tb["y1"] + 3 for tb in lay["tables"]):
            continue
        for ni, nb in enumerate(lay["notes"]):
            if nb["y0"] - 2 <= ln["y"] <= nb["y1"] + 2:
                if (ln["page"], ni) not in notes_done:
                    target = cur_sec["notes"] if cur_sec else (cur.setdefault("notes", []) if cur else [])
                    target.append(nb["text"])
                    notes_done.add((ln["page"], ni))
                break
        else:
            ni = -1
        if ni >= 0:
            continue
        heading = ln["is_heading"]
        if heading and ANNEX_RE.match(t):
            annex_n += 1
            cur = {"key": f"annex-{annex_n}", "kind": "annex", "heading": t, "label": "Annex",
                   "subtitle": ANNEX_RE.match(t).group(1).strip(),
                   "articles": [], "page": ln["page"], "sections": [], "notes": []}
            parts.append(cur)
            cur_sec = None
            in_annex = True
            continue
        if heading and in_annex and not cur["subtitle"] and t.isupper():
            cur["subtitle"] = t
            continue
        if heading and (ARTICLE_RE.match(t) or JOINT_RE.match(t)):
            key, subtitle, nums, label = article_key(t)
            if in_annex:
                # The annex reproduces the previous version of Article 7 AND its commentary, so a
                # commentary heading for that same article belongs to the annex. Anything else
                # (Article 8 after the annex, Article 26 after the arbitration annex) closes it.
                subj = re.findall(r"\d+", cur["subtitle"])
                if nums and subj and str(nums[0]) in subj:
                    new_section(t, int(round(ln["size"] * 10)))
                    continue
                in_annex = False
            cur = {"key": key, "kind": "article", "heading": t, "label": label,
                   "subtitle": subtitle, "articles": nums, "page": ln["page"],
                   "sections": [], "notes": []}
            parts.append(cur)
            cur_sec = None
            continue
        if cur is None:
            continue
        if heading:
            if cur["kind"] == "article" and not cur["sections"] and t.isupper() and ln["size"] >= 10.0:
                cur["subtitle"] = f"{cur['subtitle']} {t}".strip()
                continue
            new_section(t, int(round(ln["size"] * 10)))
            continue
        if ln["size"] <= 6.2:
            (cur_sec["notes"] if cur_sec else cur.setdefault("notes", [])).append(t)
            continue
        if cur_sec is None:
            new_section("Introduction", 0)
        cur_sec["body"].append(t)
    return parts


def heading_shape(title: str) -> str:
    """Title with numerals blanked, so "Paragraph 1" and "Paragraph 2" share a shape."""
    return re.sub(r"\d+", "#", title.strip())


def render(sec: dict, act_title: str, article_label: str, sid: str) -> str:
    fm = {
        "act": act_title,
        "slug": SLUG,
        "section": sid,
        "title": sec["title"],
        "article": article_label,
    }
    fm.update({"compilation_no": None, "compilation_date": None, "version": "2010 condensed version"})
    head = "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fm.items()) + "---\n\n"
    body = sec["body_md"]
    if sec["notes"]:
        clean = [n for n in sec["notes"] if not re.match(r"^\d{1,3}$", n)]
        if clean:
            body += "\n\n**Notes**\n\n" + "\n\n".join(clean)
    return head + f"# {sec['title']}\n\n" + body + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    articles = extract(Path(args.pdf))
    if not articles:
        print("no articles found", file=sys.stderr)
        return 1

    tree_parts = []
    files: list[tuple[str, str]] = []
    report = []
    for art in articles:
        key = art["key"]
        sub = clean_subtitle(art["subtitle"]) if art["subtitle"] else ""
        label = art.get("label") or f"Article {art['articles'][0]}"
        if art.get("kind") == "annex":
            part_title = "Annex" + (f" \u2014 {sub}" if sub else "")
        else:
            part_title = f"Commentary on {label}" + (f" \u2014 {sub}" if sub else "")
        part: dict = {"id": f"art-{key}", "title": part_title, "sections": []}
        seen: dict[str, int] = {}
        labels: list[dict] = []
        title_counts: dict[str, int] = {}
        top_title: str | None = None

        def make_sid(title: str, idx: int, level: int) -> str:
            if idx == 0 and level == 0:
                sid = f"art-{key}-intro"
            else:
                sid = f"art-{key}-{idx:02d}-{slugify(title)}"
            n = seen.get(sid, 0) + 1
            seen[sid] = n
            return sid if n == 1 else f"{sid}-{n}"

        for i, sec in enumerate(art["sections"]):
            sec["body_md"] = para_text(sec["body"])
            if not sec["body_md"].strip():
                # A heading with no text of its own is a grouping label over the subheadings that
                # follow ("Paragraph 1", "III. Commentary on the provisions of Article 23 A"). The
                # tree holds no interleaved children, so the label rides on the child's title and the
                # print order stays exact. Labels nest, so they are kept as a stack.
                label = sec["title"].strip()
                lvl = sec["level"]
                nested = [x for x in labels if x["level"] > lvl]
                if (re.match(r"^[IVXLC]+\.\s*Commentary on the provisions of the Article\s*$", label, re.I)
                        or re.match(r"^COMMENTARY ON ARTICLE\b", label, re.I)):
                    # Boilerplate over the whole article body, or the part's own title repeated --
                    # carrying either onto every child title only adds noise.
                    labels = nested
                    continue
                labels = nested + [{"level": lvl, "text": label, "shape": heading_shape(label)}]
                continue
            lvl = sec["level"]
            shape = heading_shape(sec["title"])
            # A heading closes every label less prominent than itself, and any label of its own rank
            # and shape ("Paragraph 2" after "Paragraph 1" is a peer, not a child).
            labels = [x for x in labels
                      if x["level"] > lvl or (x["level"] == lvl and x["shape"] != shape)]
            raw_title = sec["title"]
            prefix = " \u2014 ".join(x["text"] for x in labels)
            title = f"{prefix} \u2014 {raw_title}" if prefix else raw_title
            if len(title) > 150 and len(labels) > 1:
                title = f"{labels[-1]['text']} \u2014 {raw_title}"
            if title_counts.get(title) and top_title and not title.startswith(top_title):
                # Same heading text twice in one part ("Paragraph 1" under the commentary and again
                # under the reservations) would give two identically named pages in the sidebar; the
                # enclosing top-level heading tells them apart.
                title = f"{top_title} \u2014 {title}"
            title_counts[title] = title_counts.get(title, 0) + 1
            if lvl >= 100:
                top_title = raw_title
            sid = make_sid(raw_title, i, sec["level"])
            sec["title"] = title
            files.append((sid, render(sec, "OECD Model Tax Convention 2010 \u2014 Commentaries on the Articles", label, sid)))
            part["sections"].append({"id": sid, "title": title, "path": f"{sid}.md"})
        tree_parts.append(part)
        report.append(
            {
                "article": key,
                "page": art["page"],
                "title": part_title,
                "sections": len(part["sections"]),
                "headings": [s["title"][:78] for s in part["sections"]][:8],
            }
        )

    tree = {
        "act": "OECD Model Tax Convention 2010 \u2014 Commentaries on the Articles",
        "compilation_no": None,
        "compilation_date": None,
        "version": "2010 condensed version",
        "compilation_note": "Condensed version of the OECD Model Tax Convention (ISBN 978-92-64-08948-8, \u00a9 OECD 2010). The source states no compilation number; the commentaries are reproduced as printed in that edition.",
        "parts": tree_parts,
    }

    print(f"articles: {len(tree_parts)}   sections: {len(files)}")
    for r in report:
        print(f"  art {r['article']:8} p{r['page']:>3}  {r['sections']:>3} sections  {r['title'][:64]}")
        for h in r["headings"][:4]:
            print(f"       - {h}")

    if args.report:
        Path(args.report).write_text(json.dumps({"tree": tree, "files": [f[0] for f in files]}, indent=1), encoding="utf-8")

    if not args.apply:
        print("\n(dry run \u2014 pass --apply to write)")
        return 0

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "sections").mkdir(parents=True)
    for sid, md in files:
        (OUT / "sections" / f"{sid}.md").write_text(md, encoding="utf-8")
    (OUT / "tree.json").write_text(json.dumps(tree, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    lic = {
        "act": tree["act"],
        "slug": SLUG,
        "jurisdiction": "OECD",
        "source_url": "user-supplied PDF 'MTC2010.PDF' (title as printed in the file's own metadata)",
        "source_url_evidence": "PDF metadata title MTC2010.PDF; every page footer reads MODEL TAX CONVENTION (CONDENSED VERSION) \u2013 ISBN 978-92-64-08948-8 \u2013 \u00a9 OECD 2010",
        "fetched": "2026-10-27",
        "date_basis": "edition printed in the source footer (2010 condensed version)",
        "version": {"date": "2010", "evidence": "condensed version, ISBN 978-92-64-08948-8, \u00a9 OECD 2010"},
        "licence": {
            "name": "OECD copyright \u2014 \u00a9 OECD 2010; reuse on OECD terms",
            "url": "https://www.oecd.org/termsandconditions/",
            "statement": "The commentaries are \u00a9 OECD. Reproduced for research and professional reference with attribution to the OECD.",
            "reuse": "OECD terms permit personal, non-commercial use with attribution; commercial reuse needs OECD permission. Not verified against the OECD site by this ingest.",
        },
        "copyright_notice": "\u00a9 OECD 2010",
        "attribution": "OECD, Model Tax Convention on Income and on Capital (Condensed Version), \u00a9 OECD 2010",
        "attribution_display": "OECD \u00a9 2010",
        "attribution_required": True,
        "attribution_basis": "OECD copyright notice printed on every page of the source",
        "content_changed": True,
        "changes": ["split into one file per article subheading", "reformatted as markdown", "running heads and folios removed"],
        "disclaimer": None,
        "unofficial_notice": "Unofficial copy for research. The official text is published by the OECD.",
        "endorsement_notice": None,
        "excluded_material": "None reproduced beyond the commentary text itself; tables are rendered as plain text, not reproductions of the printed artwork.",
        "terms_url": "https://www.oecd.org/termsandconditions/",
        "notice_line": "OECD \u00a9 2010 \u00b7 unofficial copy for research",
        "notice_line_basis": "short form of the attribution and unofficial notice above",
        "notice_badge": "OECD",
    }
    (OUT / "licence.json").write_text(json.dumps(lic, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {len(files)} section files + tree.json + licence.json to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
