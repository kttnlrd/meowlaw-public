#!/usr/bin/env python3
"""Ingest the State tax Acts (QLD, TAS, SA, WA) into data/<slug>/.

Sources are the registers' own downloads, already saved under data/<slug>/raw/ by the
fetch step (see data/<slug>/licence.json for URL, status, bytes and fetch date):

  QLD, TAS  whole-Act HTML (EnAct)        -> lin_enact()
  WA        HTML rendition of the reprint -> lin_wa()   (authorised PDF kept for the gate)
  SA        RTF rendition, via LibreOffice -> DOCX -> lin_sa() (authorised PDF kept for the gate)
  NSW       rendered text of the whole-Act HTML view (site furniture + table of contents around it) -> lin_nsw()

Every linearizer turns its document into one flat stream of Blk records (headings, body text
with an indentation measure, table rows, image placeholders, and SKIP records for what is
deliberately left out), so tree building, markdown formatting and the completeness gate are
shared by all four registers.

Representation (corpus convention: evidence-act-1995 / fair-work-2009 / gst-1999):
  * tree parts = the Act's Parts; a Chapter is folded into `chapter`/`chapter_title` and, when the
    Act has Chapters, qualifies the part id (`2-1` = Chapter 2 Part 1) because Part numbers restart;
  * a Part with no Divisions gets one division with the Part's id (evidence-act convention);
  * a Schedule is a top-level part `schedule-N`; its Parts are divisions, its Divisions subdivisions;
  * schedule clauses restart at 1, so their ids are `schedule-N-<clause>` (printed number kept in
    frontmatter `printed_number`); otherwise find_section_path()'s first match would shadow them with
    the Act's own s 1, s 2 ... (same reason evidence-act-1995 namespaces its Dictionary items);
  * an unnumbered schedule body is one section `schedule-N`.

Left out on purpose (counted by the gate as excluded classes, never silently):
  amendment-history notes, tables of contents, front matter (long title, enacting words),
  endnotes / legislative history / defined-terms index, endnote reference markers in WA headings.
Images (formulas, figures) cannot be reproduced as text: each becomes a visible placeholder and is
listed per section as unrepresented.

Usage:  /usr/bin/python3.12 scripts/ingest_state_tax_acts.py [slug ...] [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# --------------------------------------------------------------------------------------------
# Act registry
# --------------------------------------------------------------------------------------------

ACTS: dict[str, dict] = {
    "qld-duties-2001": dict(jur="QLD", title="Duties Act 2001", raw="act-2001-071.html", fmt="enact"),
    "qld-landtax-2010": dict(jur="QLD", title="Land Tax Act 2010", raw="act-2010-015.html", fmt="enact"),
    "qld-payrolltax-1971": dict(jur="QLD", title="Payroll Tax Act 1971", raw="act-1971-037.html", fmt="enact"),
    "tas-duties-2001": dict(jur="TAS", title="Duties Act 2001", raw="act-2001-015.html", fmt="enact"),
    "tas-landtax-2000": dict(jur="TAS", title="Land Tax Act 2000", raw="act-2000-074.html", fmt="enact"),
    "tas-payrolltax-2008": dict(jur="TAS", title="Payroll Tax Act 2008", raw="act-2008-016.html", fmt="enact"),
    "sa-stampduties-1923": dict(jur="SA", title="Stamp Duties Act 1923", raw="1923.1569.un.rtf", pdf="1923.1569.auth.pdf", fmt="sa"),
    "sa-landtax-1936": dict(jur="SA", title="Land Tax Act 1936", raw="1936.2318.un.rtf", pdf="1936.2318.auth.pdf", fmt="sa"),
    "sa-payrolltax-2009": dict(jur="SA", title="Payroll Tax Act 2009", raw="2009.22.un.rtf", pdf="2009.22.auth.pdf", fmt="sa"),
    "wa-duties-2008": dict(jur="WA", title="Duties Act 2008", raw="mrdoc_49812.htm", pdf="mrdoc_49812.pdf", fmt="wa"),
    "wa-landtaxassessment-2002": dict(jur="WA", title="Land Tax Assessment Act 2002", raw="mrdoc_49123.htm", pdf="mrdoc_49123.pdf", fmt="wa"),
    "wa-payrolltaxassessment-2002": dict(jur="WA", title="Pay-roll Tax Assessment Act 2002", raw="mrdoc_42096.htm", pdf="mrdoc_42096.pdf", fmt="wa"),
    "nsw-duties-1997": dict(jur="NSW", title="Duties Act 1997", raw="duties-1997.viewer.txt", fmt="nsw"),
    "nsw-landtaxmgmt-1956": dict(jur="NSW", title="Land Tax Management Act 1956", raw="landtaxmgmt-1956.viewer.txt", fmt="nsw"),
    "nsw-landtax-1956": dict(jur="NSW", title="Land Tax Act 1956", raw="landtax-1956.viewer.txt", fmt="nsw"),
    "nsw-payrolltax-2007": dict(jur="NSW", title="Payroll Tax Act 2007", raw="payrolltax-2007.viewer.txt", fmt="nsw"),
}
JUR_SUFFIX = {"QLD": "Qld", "TAS": "Tas", "SA": "SA", "WA": "WA", "NSW": "NSW"}


def act_name(slug: str) -> str:
    a = ACTS[slug]
    return f"{a['title']} ({JUR_SUFFIX[a['jur']]})"


IMAGE_NOTE = ("*[Formula or figure published as an image in the source; not reproduced here. "
              "See the authorised version on the register.]*")

# --------------------------------------------------------------------------------------------
# Block stream
# --------------------------------------------------------------------------------------------


@dataclass
class Blk:
    kind: str                 # chapter part division subdivision schedule section clause text row image skip
    text: str = ""
    ind: float = 0.0          # indentation measure (DOM depth / twips / style rank); only order matters
    num: str = ""
    title: str = ""
    note: bool = False        # inside a note container
    defterm: str = ""         # leading defined term, when the source marks one up
    reason: str = ""          # for kind == 'skip'


def clean(s: str) -> str:
    s = unicodedata.normalize("NFKC", s.replace("­", ""))
    s = s.replace("‑", "-")
    return re.sub(r"\s+", " ", s).strip()


HEAD_RE = re.compile(r"^(chapter|part|division|subdivision|schedule)\b\s*(\d+[A-Z]*)?\b\s*(?:[-–—:.]\s*)?(.*)$", re.I | re.S)


def heading(text: str) -> Blk | None:
    m = HEAD_RE.match(clean(text))
    if not m:
        return None
    kind, num, title = m.group(1).lower(), m.group(2) or "", clean(m.group(3))
    return Blk(kind, text=clean(text), num=num, title=title)


# --------------------------------------------------------------------------------------------
# QLD / TAS: EnAct whole-Act HTML
# --------------------------------------------------------------------------------------------

BLOCK_TAGS = {"p", "blockquote", "div", "table", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dt", "dd"}
HEAD_CLASSES = ("ChapterHeadingParagraph", "PartHeadingParagraph", "OtherHeadingParagraph", "ScheduleHeadingParagraph")
FRONT_CLASSES = ("TopHeadingSpan", "TopHeadingParagraph", "LongTitleParagraph", "LongTitle", "RoyalAssent")


def _cls(el: Tag) -> str:
    return " ".join(el.get("class") or [])


def _is_history(el: Tag) -> bool:
    c = _cls(el)
    return "history-note" in c or "view-history-note" in c


def _has_block(el: Tag) -> bool:
    return any(isinstance(d, Tag) and (d.name in BLOCK_TAGS or d.name == "img") for d in el.descendants)


def _inline_text(el) -> str:
    """Text of an inline subtree, minus history notes and comments."""
    if isinstance(el, Comment):
        return ""
    if isinstance(el, NavigableString):
        return str(el)
    if _is_history(el):
        return ""
    if el.name == "br":
        return " "
    out = "".join(_inline_text(c) for c in el.children)
    if "ListNumber" in _cls(el) or el.name in BLOCK_TAGS or el.name in ("td", "th", "tr"):
        out = out + " " if "ListNumber" in _cls(el) else " " + out + " "
    return out


def _restore_rows(html: str) -> str:
    """TAS EnAct writes table rows as `<td>..</td></TR>` with no opening <tr>; lxml drops the stray
    closers and every row of the table runs together. Re-open a row after each closer."""
    if len(re.findall(r"</tr>", html, re.I)) <= len(re.findall(r"<tr\b", html, re.I)):
        return html
    html = re.sub(r"(<table\b[^>]*>)", r"\1<tr>", html, flags=re.I)
    return re.sub(r"</tr>", "</tr><tr>", html, flags=re.I)


def lin_enact(path: Path) -> list[Blk]:
    soup = BeautifulSoup(_restore_rows(path.read_text(encoding="utf-8")), "lxml")
    root = soup.find(id="fragview")
    out: list[Blk] = []
    started = [False]

    def history(el: Tag):
        t = clean(el.get_text(" "))
        if t:
            out.append(Blk("skip", text=t, reason="amendment history note"))

    def visit(el: Tag, depth: int, in_note: bool):
        c = _cls(el)
        if _is_history(el):
            return history(el)
        if any(k in c for k in FRONT_CLASSES):
            t = clean(el.get_text(" "))
            if t:
                out.append(Blk("skip", text=t, reason="front matter"))
            return
        if el.name in ("p", "blockquote") and any(k in c.split() for k in HEAD_CLASSES):
            started[0] = True
            h = heading(_inline_text(el))
            if h is None:
                raise SystemExit(f"{path}: unclassifiable heading {clean(el.get_text(' '))!r}")
            out.append(h)
            return
        if el.name == "p" and "HeadingParagraph" in c.split():
            started[0] = True
            bs = el.find_all("b", class_="HeadingStyle")
            name = el.find("span", class_="HeadingName")
            if name is not None:                      # QLD: <B>8</B><span HeadingName>title</span>
                num, title = clean(_inline_text(bs[0])), clean(_inline_text(name))
            else:                                     # TAS: <B>8.</B>&nbsp;<B>title</B>
                num, title = clean(_inline_text(bs[0])), clean(" ".join(_inline_text(b) for b in bs[1:]))
            out.append(Blk("section", text=clean(_inline_text(el)), num=num.rstrip("."), title=title))
            return
        if el.name == "table":
            rows = [tr for tr in el.find_all("tr") if tr.find_parent("table") is el] or [el]   # EnAct emits <td> without <tr>
            for tr in rows:
                cells = [clean(_inline_text(td)) for td in tr.find_all(["td", "th"], recursive=tr is el) if td.find_parent("table") is el]
                cells = [x for x in cells if x]
                for img in tr.find_all("img"):
                    out.append(Blk("image", text=img.get("alt") or img.get("src") or ""))
                if not cells:
                    continue
                if len(cells) == 1:
                    out.append(Blk("text", text=cells[0], ind=depth + 1, note=in_note or bool(NOTE_RE.match(cells[0]))))
                else:
                    out.append(Blk("row", text=" | ".join(cells)))
            return
        if el.name == "img":
            out.append(Blk("image", text=el.get("alt") or el.get("src") or ""))
            return
        in_note = in_note or "note" in c.split()
        seg: list[str] = []
        after = [False]
        lead = [None]

        def flush():
            t = clean("".join(seg))
            seg.clear()
            if t:
                if not started[0]:
                    out.append(Blk("skip", text=t, reason="front matter"))
                else:
                    out.append(Blk("text", text=t, ind=depth + (0.5 if after[0] else 0), note=in_note, defterm=lead[0] or ""))
            lead[0] = None

        for ch in el.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                seg.append(str(ch))
                continue
            if not isinstance(ch, Tag):
                continue
            if _is_history(ch):
                flush()
                history(ch)
                continue
            if ch.name == "b" and "HeadingStyle" in _cls(ch) and ch.find("b"):
                # TAS schedule clause heading: <b HeadingStyle>1.&nbsp;<B HeadingStyle>Definitions</B></b>
                flush()
                inner = ch.find("b")
                title = clean(_inline_text(inner))
                num = clean(_inline_text(ch))[: -len(title) or None].strip().rstrip(".")
                out.append(Blk("clause", text=clean(_inline_text(ch)), num=num, title=title))
                started[0] = True
                continue
            if ch.name in BLOCK_TAGS or ch.name == "img" or _has_block(ch):
                flush()
                visit(ch, depth + 1, in_note)
                after[0] = True
                continue
            if not clean("".join(seg)) and lead[0] is None:
                # defined term: leading bold-italic run (QLD <B><I>, TAS <B><I>)
                if (ch.name == "b" and ch.find("i")) or (ch.name == "i" and ch.find("b")):
                    lead[0] = clean(_inline_text(ch))
                elif ch.name == "span" and "quote" in _cls(ch) and ch.find("b"):
                    lead[0] = clean(_inline_text(ch)).strip('"“”')
            seg.append(_inline_text(ch))
        flush()

    for ch in root.children:
        if isinstance(ch, Tag):
            visit(ch, 0, False)
    return out


# --------------------------------------------------------------------------------------------
# WA: HTML rendition of the reprint (Word export with paragraph styles)
# --------------------------------------------------------------------------------------------

WA_IND = {
    "Subsection": 1, "Defstart": 1, "MiscellaneousBody": 1, "Penstart": 1, "Equation": 1, "Normal": 1,
    "Indenta": 2, "Defpara": 2, "Indenti0": 2, "IndentI0": 2, "Penpara": 2,
    "Indenti": 3, "Defsubpara": 3, "IndentA": 4,
}


def _wa_style(p: Tag) -> str:
    c = _cls(p)
    return c[:-2] if c.endswith("-P") else c


def _wa_text(el, drop_sup: bool) -> str:
    if isinstance(el, Comment):
        return ""
    if isinstance(el, NavigableString):
        return str(el)
    if el.name == "br":
        return " "
    if drop_sup and "top: -0.4em" in (el.get("style") or ""):
        return " "
    return "".join(_wa_text(c, drop_sup) for c in el.children)


def lin_wa(path: Path) -> list[Blk]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")
    out: list[Blk] = []
    phase = "front"            # front -> body -> end
    for el in soup.body.find_all(["p", "table"]):
        if el.find_parent("table") is not None:
            continue
        if el.name == "table":
            if phase != "body":
                out.append(Blk("skip", text=clean(el.get_text(" ")), reason="front matter" if phase == "front" else "endnotes"))
                continue
            for tr in el.find_all("tr"):
                cells = [clean(_wa_text(td, False)) for td in tr.find_all(["td", "th"], recursive=False)]
                cells = [x for x in cells if x]
                for img in tr.find_all("img"):
                    out.append(Blk("image", text=img.get("alt") or img.get("src") or ""))
                if cells:
                    out.append(Blk("row", text=" | ".join(cells)) if len(cells) > 1 else Blk("text", text=cells[0], ind=1))
            continue
        st = _wa_style(el)
        base = st[1:] if st.startswith("y") and st[1:2].isupper() else st
        if phase != "end" and st in ("nHeading2",) and clean(el.get_text(" ")).startswith("Notes"):
            phase = "end"
        if phase == "front" and st in ("Heading2", "Heading3", "Heading5", "Heading1", "Heading4") :
            phase = "body"
        is_head5 = base == "Heading5"
        text = clean(_wa_text(el, drop_sup=is_head5))
        imgs = el.find_all("img")
        if phase != "body":
            if text or imgs:
                out.append(Blk("skip", text=text, reason="front matter / table of contents" if phase == "front" else "endnotes / defined-terms index"))
            continue
        if st.startswith("TOC"):
            out.append(Blk("skip", text=text, reason="table of contents"))
            continue
        if base.startswith("Footnote"):
            out.append(Blk("skip", text=text, reason="amendment history note"))
            continue
        if is_head5:
            raw = clean(_wa_text(el, drop_sup=False))
            if raw != text:
                out.append(Blk("skip", text=raw, reason="endnote reference marker in heading"))
            sec = el.find("span", class_=re.compile("CharSectno|CharSClsNo"))
            num = clean(sec.get_text()) if sec else re.match(r"^(\S+?)\s*\.", text).group(1)
            title = clean(re.sub(r"^\s*" + re.escape(num) + r"\s*\.\s*", "", text))
            out.append(Blk("clause" if st.startswith("y") else "section", text=text, num=num, title=title))
            continue
        if base in ("Heading1", "Heading2", "Heading3", "Heading4", "ScheduleHeading"):
            h = heading(text)
            if h is None and base == "ScheduleHeading":     # an unnumbered end matter block, e.g. "Glossary"
                h = Blk("schedule", text=text, title=text)
            if h is None:
                raise SystemExit(f"{path}: unclassifiable heading {text!r} ({st})")
            out.append(h)
            continue
        for img in imgs:
            out.append(Blk("image", text=img.get("alt") or img.get("src") or ""))
        if not text:
            continue
        dt = el.find("span", class_="CharDefText-H")
        out.append(Blk("text", text=text, ind=WA_IND.get(base, 1), note=base.startswith(("Note", "nNote", "PermNote")),
                       defterm=clean(dt.get_text()) if dt is not None and text.startswith(clean(dt.get_text())) else ""))
    return out


# --------------------------------------------------------------------------------------------
# SA: RTF -> DOCX (LibreOffice), walked in body order
# --------------------------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def rtf_to_docx(rtf: Path, tmp: Path) -> Path:
    subprocess.run(["soffice", "--headless", "--convert-to", "docx", "--outdir", str(tmp), str(rtf)],
                   check=True, capture_output=True, timeout=600)
    return tmp / (rtf.stem + ".docx")


def _sa_para_text(p) -> str:
    """All run text in reading order (smart-tag safe), skipping pPr tab-stop definitions."""
    parts = []
    for node in p.iter():
        if node.tag == _W + "pPr":
            continue
        if any(a.tag == _W + "pPr" for a in node.iterancestors()):
            continue
        if node.tag == _W + "t" and node.text:
            parts.append(node.text)
        elif node.tag in (_W + "tab", _W + "br", _W + "cr"):
            parts.append(" ")
        elif node.tag == _W + "noBreakHyphen":
            parts.append("-")
    return "".join(parts)


def _sa_has_image(p) -> bool:
    return any(n.tag.endswith(("}drawing", "}pict", "}imagedata")) for n in p.iter())


def _sa_lead_bold(p) -> str:
    """Leading bold run(s) of a paragraph — SA marks defined terms in bold-italic."""
    txt = []
    for r in p.iter(_W + "r"):
        rpr = r.find(_W + "rPr")
        t = "".join(x.text or "" for x in r.iter(_W + "t"))
        if not t.strip() and not txt:
            continue
        if rpr is not None and rpr.find(_W + "b") is not None and (rpr.find(_W + "b").get(_W + "val") in (None, "1", "true", "on")):
            txt.append(t)
        else:
            break
    return clean("".join(txt))


SA_SEC_RE = re.compile(r"^(\d+[A-Z]*)\s*—\s*(.*)$")


def lin_sa(path: Path) -> list[Blk]:
    from docx import Document
    with tempfile.TemporaryDirectory() as tmp:
        d = Document(str(rtf_to_docx(path, Path(tmp))))
    styles = {s.style_id: s.name for s in d.styles}
    out: list[Blk] = []
    phase = "front"
    in_schedule = False
    for el in d.element.body.iterchildren():
        if el.tag == _W + "tbl":
            for tr in el.iter(_W + "tr"):
                cells = [clean(" ".join(_sa_para_text(p) for p in tc.iter(_W + "p"))) for tc in tr.iter(_W + "tc")]
                cells = [x for x in cells if x]
                if not cells:
                    continue
                if phase != "body":
                    out.append(Blk("skip", text=" | ".join(cells), reason="table of contents" if phase == "front" else "legislative history"))
                    continue
                if any(_sa_has_image(p) for p in tr.iter(_W + "p")):
                    out.append(Blk("image"))
                out.append(Blk("row", text=" | ".join(cells)))
            continue
        if el.tag != _W + "p":
            continue
        ppr = el.find(_W + "pPr")
        sid = ppr.find(_W + "pStyle").get(_W + "val") if ppr is not None and ppr.find(_W + "pStyle") is not None else None
        style = styles.get(sid, sid) or "Normal"
        ind_el = ppr.find(_W + "ind") if ppr is not None else None
        ind = int((ind_el.get(_W + "left") or ind_el.get(_W + "start") or 0)) if ind_el is not None else 0
        text = clean(_sa_para_text(el))
        img = _sa_has_image(el)
        if style == "leghistoryheadlevel1":
            phase = "end"
        if phase == "front" and (style == "partheadlevel1" or (style.startswith("clausehead") and SA_SEC_RE.match(text))):
            phase = "body"
        if phase != "body":
            if text:
                out.append(Blk("skip", text=text, reason="front matter / table of contents" if phase == "front" else "legislative history"))
            continue
        if style == "partheadlevel1" or style == "divisionheadlevel2" or style == "subdivisionheadlevel3":
            h = heading(text)
            if h is None:
                raise SystemExit(f"{path}: unclassifiable heading {text!r}")
            out.append(h)
            continue
        # w:ind is in twips: 567 = heading margin, 794 = subsection, 1588 = paragraph, 2381 = subparagraph
        if ind <= 567 and text:
            h = heading(text)
            if h is not None and h.kind == "schedule":
                in_schedule = True
                out.append(h)
                continue
            if h is not None and in_schedule and h.kind in ("part", "division", "subdivision"):
                out.append(h)
                continue
            m = SA_SEC_RE.match(text)
            if m and ind == 567 and (style.startswith("clausehead") or style.startswith("clausegroup") or in_schedule):
                out.append(Blk("clause" if in_schedule else "section", text=text, num=m.group(1), title=clean(m.group(2))))
                continue
        if img:
            out.append(Blk("image"))
        if not text:
            continue
        lead = _sa_lead_bold(el)
        out.append(Blk("text", text=text, ind=ind, defterm=lead if lead and text.startswith(lead) and not lead.startswith("(") else ""))
    return out


# --------------------------------------------------------------------------------------------
# NSW: rendered text of the register's whole-Act view. No markup survives, but the text keeps
# the register's own spacing: a provision heading is "<num>\xa0\xa0\xa0<title>" and a subsection or
# paragraph is "(<mark>)\xa0 <text>", whereas the table of contents above the body uses plain
# spaces. A table cell boundary is a line holding only a tab; a row boundary is two empty lines.
# --------------------------------------------------------------------------------------------

NSW_SEC_RE = re.compile(r"^(\d+[A-Z]*(?:(?:[–-]|, )\d+[A-Z]*)*)\xa0[\xa0 ]*(.*)$")
NSW_HEAD_RE = re.compile(r"^(Chapters?|Parts?|Divisions?|Subdivisions?|Schedule)\s+(\d+[A-Z]*(?:\.\d+)?(?:(?:[–-]|, | and )\d+[A-Z]*)*)(?:\s+(.*))?$")
NSW_MARK_RE = re.compile(r"^\(([0-9]+[A-Za-z]*|[a-z]{1,4}|[A-Z]{1,3})\)\xa0")
NSW_DEFCTX_RE = re.compile(r"^(?:\(\w+\)\s*)?In this (?:Act|Chapter|Part|Division|Subdivision|section|Schedule|clause|Dictionary)\b.*—$")
NSW_TERM_RE = re.compile(r"^([^\s(].{0,70}?)(?:,\s[^,]{1,120},)?\s+(?:means|includes|has the meaning|has the same meaning|does not include|refers to)\b"
                         r"|^([^\s(].{0,70}?)—see\b")


def nsw_body_range(lines: list[str]) -> tuple[int, int]:
    """(first body line, footer line): the body follows the long title ("An Act ...") after the last
    "Search Act" toolbar line; the site footer ends it."""
    tb = max(i for i, l in enumerate(lines) if l.strip() == "Search Act")
    lt = next(i for i in range(tb + 1, len(lines)) if lines[i].startswith("An Act"))
    return lt + 1, next(i for i in range(lt, len(lines)) if lines[i].strip() == "Site footer")


def _nsw_heading(line: str) -> Blk | None:
    if line.strip() == "Dictionary":
        return Blk("schedule", text="Dictionary", title="Dictionary")
    m = NSW_HEAD_RE.match(line.strip())
    if not m or line.rstrip().endswith((".", ",", ";", ":")):
        return None                       # "Schedule 1 has effect." is a sentence, not a heading
    title = clean(m.group(3) or "")
    if title and not (title[0].isupper() or title[0] in "(“\""):
        return None
    kind = m.group(1).lower().rstrip("s")
    return Blk(kind, text=clean(line), num=re.sub(r"–|, | and ", "-", m.group(2)), title=title)


def lin_nsw(path: Path) -> list[Blk]:
    lines = path.read_text(encoding="utf-8").split("\n")
    start, end = nsw_body_range(lines)
    out: list[Blk] = [Blk("skip", text=clean(l), reason="site furniture / table of contents / status information / front matter")
                      for l in lines[:start] if clean(l)]
    body = lines[start:end]
    marks = [NSW_MARK_RE.match(l).group(1) if NSW_MARK_RE.match(l) else None for l in body]
    in_sched, alpha, note, defctx, note_list = False, None, False, False, False
    i = 0
    while i < len(body):
        raw = body[i]
        t = clean(raw)
        if not t:
            i += 1
            continue
        if i + 1 < len(body) and body[i + 1] == "\t":         # table: rows of tab-separated cells
            rows, cells, cell = [], [], [t]
            i += 1
            while i < len(body):
                l = body[i]
                if l == "\t":
                    cells.append(" ".join(cell)); cell = []
                elif l.strip() == "" and i + 1 < len(body) and body[i + 1].strip() == "":
                    cells.append(" ".join(cell)); rows.append(cells); cells, cell = [], []
                    i += 1
                elif l.strip() == "" or NSW_SEC_RE.match(l) or _nsw_heading(l) or (marks[i] or "")[:1].isdigit():
                    break                     # a cell never holds a heading or a numbered subsection
                else:
                    cell.append(clean(l))
                i += 1
            if cell or cells:
                cells.append(" ".join(cell)); rows.append(cells)
            for r in rows:
                r = [c for c in r if c]
                if r:
                    out.append(Blk("row", text=" | ".join(r)))
            note = False
            continue
        m = NSW_SEC_RE.match(raw)
        h = None if m else _nsw_heading(raw)
        if NSW_DEFCTX_RE.match(t):
            defctx = True
        if m:
            out.append(Blk("clause" if in_sched else "section", text=t, num=re.sub(r"–|, ", "-", m.group(1)), title=clean(m.group(2))))
            alpha, note, defctx = None, False, False
        elif h is not None:
            if not h.title and i + 1 < len(body) and re.fullmatch(r"\((Repealed|Omitted|Expired)\)", clean(body[i + 1])):
                h.title, h.text = clean(body[i + 1]), f"{h.text} {clean(body[i + 1])}"     # "Part 9" / "(Repealed)" on two lines
                i += 1
            if h.kind == "schedule":
                in_sched = True
            elif h.kind == "chapter":
                in_sched = False
            out.append(h)
            alpha, note, defctx = None, False, False
        elif re.fullmatch(r"(Notes?|Examples?)[.—]", t):
            note, note_list = True, False
            out.append(Blk("text", text=t[:-1] + "—", ind=1, note=True))
        elif re.match(r"(Notes?|Examples?)(\s+\d+)?\s*[:—–]|Examples? for\b", t) and not marks[i]:
            note, note_list = True, t.endswith(("—", ":"))
            out.append(Blk("text", text=t, ind=1, note=True))
        elif marks[i]:
            tok = marks[i]
            if not tok[0].isdigit() and note and not note_list:
                note = False                  # a paragraph after a note belongs to the provision, not the note
            if tok[0].isdigit():
                ind, alpha, note, defctx = 1, None, False, bool(NSW_DEFCTX_RE.match(t))
            elif tok.isupper():
                ind = 4
            elif ROMAN_RE.match(tok) and alpha is not None and not (
                    len(tok) == 1 and alpha == chr(ord(tok) - 1)
                    and f"({tok}i)" not in next((l[:6] for l in body[i + 1:] if NSW_MARK_RE.match(l)), "")):
                ind = 3                       # (i) after (h) is a paragraph unless an (ii) follows
            else:
                ind, alpha = 2, tok
            out.append(Blk("text", text=t, ind=ind, note=note))
            note_list = note
        else:
            dm = NSW_TERM_RE.match(t) if defctx else None
            term = (dm.group(1) or dm.group(2)) if dm else ""
            if term and (len(term.split()) > 8 or re.match(r"(The|A|An|This|In|For|If|Where)\s", term)):
                term = ""
            note = note and not term          # the next definition ends a note inside a definitions list
            note_list = note and t.endswith(("—", ":"))
            out.append(Blk("text", text=t, ind=1, note=note, defterm=term))
            if term:
                alpha = None
        i += 1
    out += [Blk("skip", text=clean(l), reason="site furniture") for l in lines[end:] if clean(l)]
    return out


# --------------------------------------------------------------------------------------------
# Markdown formatting (legislation-markdown-formatting.md conventions)
# --------------------------------------------------------------------------------------------

MARK_RE = re.compile(r"^\(([0-9]+[A-Za-z]*|[a-z]{1,4}|[A-Z]{1,3})\)\s*(.*)$", re.S)
ROMAN_RE = re.compile(r"^(?=[ivxlc]+$)(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
NOTE_RE = re.compile(r"^(Notes?|Examples?|Editor['’]s notes?)(\s+\d+)?\s*[—–:\-]\s*(.*)$", re.S)


def format_body(blocks: list[Blk]) -> tuple[str, int]:
    lines: list[tuple[str, str]] = []      # (kind, line)
    stack: list[tuple[float, int, str]] = []
    images = 0
    held = ""                                 # a note label printed as its own paragraph ("Note—")
    for b in blocks:
        if b.kind == "text" and NOTE_RE.match(b.text) and not NOTE_RE.match(b.text).group(3).strip():
            held = b.text
            continue
        if held and b.kind == "text":
            b = Blk(b.kind, text=f"{held} {b.text}", ind=b.ind, note=True)
        elif held:
            lines.append(("text", f"> **{held.rstrip(chr(0x2014)).rstrip(':').strip()}:**"))
        held = ""
        if b.kind == "row":
            lines.append(("row", b.text))
            continue
        if b.kind == "image":
            images += 1
            lines.append(("text", IMAGE_NOTE))
            continue
        while stack and stack[-1][0] >= b.ind:
            stack.pop()
        t = b.text
        n = NOTE_RE.match(t)
        m = MARK_RE.match(t)
        if n:
            label = n.group(1).rstrip("s") if n.group(1).lower().startswith(("notes", "examples")) and n.group(2) else n.group(1)
            label = f"{label}{n.group(2) or ''}:"
            rest = n.group(3)
            line = f"> *{label}* {rest}" if label.lower().startswith("example") else f"> **{label}** {rest}"
            lines.append(("text", line.rstrip()))
            continue
        if m:
            tok, rest = m.group(1), m.group(2)
            if tok[0].isdigit():
                level, kind = 0, "num"
            elif tok.isupper():
                level, kind = 3, "cap"
            elif ROMAN_RE.match(tok) and any(k == "alpha" for _, _, k in stack):
                level, kind = 2, "roman"
            else:
                level, kind = 1, "alpha"
            if b.note:
                level = max(level, 1)
            stack.append((b.ind, level, kind))
            lines.append(("text", ("> " * level) + f"**({tok})** {rest}".rstrip()))
            continue
        level = stack[-1][1] if stack else 0
        if b.note:
            level = max(level, 1)
        lines.append(("text", ("> " * level) + t))
    if held:
        lines.append(("text", f"> **{held.rstrip(chr(0x2014)).rstrip(':').strip()}:**"))
    out = []
    for i, (k, line) in enumerate(lines):
        if i:
            out.append("\n" if k == "row" and lines[i - 1][0] == "row" else "\n\n")
        out.append(line)
    return "".join(out), images


# --------------------------------------------------------------------------------------------
# Assembly: block stream -> parts/divisions/subdivisions/sections
# --------------------------------------------------------------------------------------------


FURNITURE_RE = re.compile(r"^[\[(]?(sections?|ss?\.|clauses?|cl\.)\s*\d[\w().,\- ]*(and\s+\d[\w().,\- ]*)?[\])]?$", re.I)


@dataclass
class Sec:
    id: str
    title: str
    printed: str
    ctx: dict
    blocks: list = field(default_factory=list)


def _pid(s: str) -> str:
    return s.replace(".", "-")


def _ht(b: Blk) -> str:
    if re.match(r"(Chapters|Parts|Divisions|Subdivisions)\b", b.text):
        return b.text                         # NSW repealed range, printed "Chapters 4A–6" / "Divisions 1B, 2"
    kind = b.kind.capitalize()
    return f"{kind} {b.num} — {b.title}".replace("  ", " ") if b.num else (f"{kind} — {b.title}" if b.title else kind)


def assemble(blocks: list[Blk]) -> tuple[list[dict], list[Sec]]:
    has_chapters = any(b.kind == "chapter" for b in blocks)
    parts: list[dict] = []
    secs: list[Sec] = []
    ids: set[str] = set()
    st = dict(chapter=None, part=None, division=None, subdivision=None, schedule=None)
    pending: list[Blk] = []
    cur: list[Sec | None] = [None]

    def new_part(pid: str, title: str, extra: dict) -> dict:
        p = dict(id=pid, title=title, **extra, divisions=[])
        if any(x["id"] == pid for x in parts):
            raise SystemExit(f"duplicate part id {pid}")
        parts.append(p)
        st.update(part=p, division=None, subdivision=None)
        return p

    def ensure_division() -> dict:
        if st["part"] is None:
            ch = st["chapter"]
            if ch is not None:
                new_part(ch["num"], ch["title"], dict(chapter=ch["num"], chapter_title=ch["title"]))
            else:
                new_part("sections", "Provisions", {})
        if st["division"] is None:
            p = st["part"]
            d = dict(id=p["id"], title=p["title"], subdivisions=[], sections=[])
            p["divisions"].append(d)
            st["division"] = d
        return st["division"]

    def open_section(b: Blk, sid: str, printed: str):
        d = ensure_division()
        sub = st["subdivision"]
        p = st["part"]
        if sid in ids:
            raise SystemExit(f"duplicate section id {sid}")
        ids.add(sid)
        path = f"part-{_pid(p['id'])}/division-{_pid(d['id'])}/{sid}.md"
        entry = dict(id=sid, title=b.title, path=path)
        (sub["sections"] if sub is not None else d["sections"]).append(entry)
        ctx = dict(part=p["id"], part_title=p["title"], division=d["id"], division_title=d["title"],
                   subdivision=sub["id"] if sub else "", subdivision_title=sub["title"] if sub else "", path=path)
        if p.get("chapter"):
            ctx.update(chapter=p["chapter"], chapter_title=p["chapter_title"])
        if p.get("schedule"):
            ctx.update(schedule=p["schedule"], schedule_title=p["schedule_title"])
        s = Sec(sid, b.title, printed, ctx, blocks=list(pending))
        pending.clear()
        secs.append(s)
        cur[0] = s

    def open_unnumbered():
        # unnumbered schedule content: the whole schedule (`schedule-N`) or the schedule Part /
        # Division / Subdivision it sits in (`schedule-N-part-M[-subdivision-K]`); the schedule's
        # furniture line, if pending, goes in with it
        sch, d, sub = st["schedule"], st["division"], st["subdivision"]
        if d is not None and d["id"] != sch["id"]:
            sid = f"{sch['id']}-{d['_from']}-{_pid(d['id'])}" + (f"-{sub['_from']}-{_pid(sub['id'])}" if sub else "")
            open_section(Blk("section", title=(sub or d)["title"]), sid, "")
        else:
            open_section(Blk("section", title=sch["title"]), sch["id"], "")

    def close_unnumbered_schedule(next_kind: str = ""):
        # furniture still pending when the schedule ends (no content followed it)
        if pending and st["schedule"] is not None and cur[0] is None and next_kind not in ("part", "division", "subdivision"):
            open_unnumbered()

    for b in blocks:
        k = b.kind
        if k == "skip":
            continue
        if k in ("chapter", "part", "division", "subdivision", "schedule"):
            close_unnumbered_schedule(k if st["schedule"] is not None else "")
            cur[0] = None
        if k == "chapter":
            st.update(chapter=dict(num=b.num, title=_ht(b)), part=None, division=None, subdivision=None, schedule=None)
        elif k == "schedule":
            named = not re.match(r"(?i)schedule\b", b.text)     # e.g. WA "Glossary"
            sid = f"schedule-{b.num}" if b.num else (re.sub(r"[^a-z0-9]+", "-", b.title.lower()).strip("-") if named else "schedule")
            ttl = b.title if named else _ht(b)
            st.update(chapter=None)
            p = new_part(sid, ttl, dict(schedule=sid, schedule_title=ttl))
            st["schedule"] = p
        elif k == "part":
            if st["schedule"] is not None:
                sch = st["schedule"]
                d = dict(id=b.num or str(len(sch["divisions"]) + 1), title=_ht(b), subdivisions=[], sections=[], _from="part")
                sch["divisions"].append(d)
                st.update(division=d, subdivision=None)
            else:
                ch = st["chapter"]
                pid = f"{ch['num']}-{b.num}" if (has_chapters and ch) else b.num
                extra = dict(chapter=ch["num"], chapter_title=ch["title"]) if ch else {}
                new_part(pid, _ht(b), extra)
        elif k == "division":
            if st["schedule"] is not None and st["division"] is not None and st["division"].get("_from") == "part":
                sub = dict(id=b.num, title=_ht(b), sections=[], _from="division")
                st["division"]["subdivisions"].append(sub)
                st["subdivision"] = sub
            else:
                if st["part"] is None:
                    ensure_division()
                    st["part"]["divisions"].pop()     # drop the synthetic division; this one is real
                did = b.num
                if any(x["id"] == did for x in st["part"]["divisions"]):
                    did = f"{did}-{len(st['part']['divisions'])}"
                d = dict(id=did, title=_ht(b), subdivisions=[], sections=[], _from="division")
                st["part"]["divisions"].append(d)
                st.update(division=d, subdivision=None)
        elif k == "subdivision":
            d = ensure_division()
            sub = dict(id=b.num, title=_ht(b), sections=[], _from="subdivision")
            d["subdivisions"].append(sub)
            st["subdivision"] = sub
        elif k in ("section", "clause"):
            sch = st["schedule"]
            if sch is not None:
                sid = f"{sch['id']}-{b.num}"
                d = st["division"]
                if sid in ids and d is not None and d.get("_from") == "part":
                    sid = f"{sch['id']}-part-{_pid(d['id'])}-{b.num}"   # numbering restarts in a later schedule Part
                open_section(b, sid, b.num)
            else:
                open_section(b, b.num, b.num)
        else:                                       # text / row / image
            if cur[0] is None and st["schedule"] is not None and not (b.kind == "text" and FURNITURE_RE.match(b.text)):
                open_unnumbered()
            if cur[0] is None:
                pending.append(b)
            else:
                cur[0].blocks.append(b)
    close_unnumbered_schedule()
    if pending:
        raise SystemExit(f"{len(pending)} body blocks after the last section had nowhere to go: {pending[0].text[:80]!r}")
    for p in parts:                                   # drop empty containers
        for d in p["divisions"]:
            d["subdivisions"] = [s for s in d["subdivisions"] if s["sections"]]
        p["divisions"] = [d for d in p["divisions"] if d["sections"] or d["subdivisions"] or re.search(r"[\[(](Repealed|Expired|Omitted)", d["title"])]
    # a repealed Part/Schedule keeps its printed heading even though nothing is left under it
    parts = [p for p in parts if p["divisions"] or re.search(r"[\[(](Repealed|Expired|Omitted)", p["title"])]
    for p in parts:
        for d in p["divisions"]:
            d.pop("_from", None)
            for sb in d["subdivisions"]:
                sb.pop("_from", None)
    return parts, secs


# --------------------------------------------------------------------------------------------
# Emit
# --------------------------------------------------------------------------------------------

def _q(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_act(slug: str, out_root: Path, lic: dict) -> dict:
    a = ACTS[slug]
    raw = DATA / slug / "raw" / a["raw"]
    blocks = {"enact": lin_enact, "wa": lin_wa, "sa": lin_sa, "nsw": lin_nsw}[a["fmt"]](raw)
    parts, secs = assemble(blocks)
    name = act_name(slug)
    vdate = lic["version"]["date"]
    out = out_root / slug
    if (out / "sections").exists():
        shutil.rmtree(out / "sections")
    defs: dict[str, dict] = {}
    unrep: list[dict] = []
    for s in secs:
        body, images = format_body(s.blocks)
        if images:
            unrep.append(dict(section=s.id, images=images, reason="formula/figure published as an image only"))
        fm = [("act", name), ("jurisdiction", a["jur"])]
        for k in ("chapter", "chapter_title", "schedule", "schedule_title"):
            if s.ctx.get(k):
                fm.append((k, s.ctx[k]))
        fm += [(k, s.ctx[k]) for k in ("part", "part_title", "division", "division_title", "subdivision", "subdivision_title")]
        fm += [("section", s.id), ("section_title", s.title)]
        if s.printed and s.printed != s.id:
            fm.append(("printed_number", s.printed))
        fm += [("compilation_date", vdate or ""), ("source_url", lic["source_url"])]
        head = f"# {s.printed} {s.title}".strip() if s.printed else f"# {s.title}"
        md = "---\n" + "".join(f"{k}: {_q(v)}\n" for k, v in fm) + "---\n\n" + head + "\n\n" + (body + "\n\n" if body else "") + f"---\n*{name}*\n"
        f = out / "sections" / s.ctx["path"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(md, encoding="utf-8")
        for b in s.blocks:
            if b.kind == "text" and b.defterm:
                term = b.defterm.strip(" ,;:—-")
                is_dict = bool(re.search(r"dictionary|interpretation|definitions|meaning|terms used|glossary", s.title, re.I))
                if re.fullmatch(r"[A-Z][A-Z0-9]{0,3}", term) and not is_dict:
                    continue                # a formula variable ("A means the number of days ..."), not a defined term
                key = next((k for k in defs if k.lower() == term.lower()), term)
                if key != term and term[:1].islower():
                    defs[term] = defs.pop(key)  # prefer the lower-case spelling the definition itself uses
                    key = term
                term = key
                if term and (term not in defs or (is_dict and not defs[term]["_dict"])):
                    defs[term] = dict(term=term, section=s.id, anchor=f"s{s.id}-" + re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-"),
                                      act=name, _dict=is_dict)
    tree = dict(act=name, jurisdiction=a["jur"], compilation_no=None, compilation_date=vdate,
                version_label=lic["version"].get("label"), source_url=lic["source_url"], parts=parts)
    (out / "tree.json").write_text(json.dumps(tree, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    definitions = {"_provenance": dict(generator="scripts/ingest_state_tax_acts.py", source=f"data/{slug}/raw/{a['raw']}",
                                       extracted_terms=len(defs))}
    for t in sorted(defs, key=str.lower):
        definitions[t] = {k: v for k, v in defs[t].items() if k != "_dict"}
    (out / "definitions.json").write_text(json.dumps(definitions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dict(blocks=blocks, parts=parts, secs=secs, unrepresented=unrep, terms=len(defs))


# --------------------------------------------------------------------------------------------
# Completeness gate — source paragraphs are extracted INDEPENDENTLY of the linearizers above:
# every text run of the raw document, split wherever its nearest block element changes.
# A paragraph is matched when its token sequence occurs contiguously in the act's emitted
# corpus (all section files + tree titles).
# --------------------------------------------------------------------------------------------

GATE_BLOCKS = BLOCK_TAGS | {"td", "th", "tr"}


def toks(s: str) -> list[str]:
    return re.findall(r"[a-z]+|[0-9]+", unicodedata.normalize("NFKC", s).lower())


def _src_paras_html(root: Tag, excl) -> list[tuple[str, str | None]]:
    """[(text, excluded_reason|None)] in document order."""
    paras: list[tuple[str, str | None]] = []
    cur_key, buf, reason = None, [], None
    for node in root.descendants:
        if not isinstance(node, NavigableString) or isinstance(node, Comment) or node.parent.name in ("script", "style"):
            continue
        blk = next((a for a in node.parents if a.name in GATE_BLOCKS), None)
        r = excl(node)
        key = (id(blk), r)
        if key != cur_key:
            if buf and "".join(buf).strip():
                paras.append((clean("".join(buf)), reason))
            cur_key, buf, reason = key, [], r
        buf.append(str(node))
    if buf and "".join(buf).strip():
        paras.append((clean("".join(buf)), reason))
    return paras


def source_paragraphs(slug: str) -> list[tuple[str, str | None]]:
    a = ACTS[slug]
    raw = DATA / slug / "raw" / a["raw"]
    if a["fmt"] == "enact":
        root = BeautifulSoup(raw.read_text(encoding="utf-8"), "lxml").find(id="fragview")
        seen_head = [False]

        def excl(node):
            anc = list(node.parents)
            classes = " ".join(_cls(x) for x in anc if isinstance(x, Tag))
            if "history-note" in classes:
                return "amendment history note"
            if any(k in classes for k in ("HeadingParagraph", "ChapterHeading", "PartHeading", "OtherHeading", "ScheduleHeading")) \
                    and not any(k in classes for k in FRONT_CLASSES):
                seen_head[0] = True
            if not seen_head[0]:
                return "front matter"
            return None
        return _src_paras_html(root, excl)
    if a["fmt"] == "wa":
        root = BeautifulSoup(raw.read_text(encoding="utf-8"), "lxml").body
        phase = ["front"]

        def excl(node):
            p = next((x for x in node.parents if x.name == "p"), None)
            st = _wa_style(p) if p is not None else ""
            if phase[0] == "front" and st in ("Heading1", "Heading2", "Heading3", "Heading4", "Heading5"):
                phase[0] = "body"
            if phase[0] == "body" and st == "nHeading2" and node.strip().startswith("Notes"):
                phase[0] = "end"
            if phase[0] == "front":
                return "front matter / table of contents"
            if phase[0] == "end":
                return "endnotes / defined-terms index"
            if st.startswith("TOC"):
                return "table of contents"
            if st.lstrip("y").startswith("Footnote"):
                return "amendment history note"
            if st.lstrip("y") == "Heading5" and any("top: -0.4em" in (x.get("style") or "") for x in node.parents if isinstance(x, Tag)):
                return "endnote reference marker in heading"
            return None
        return _src_paras_html(root, excl)
    if a["fmt"] == "nsw":
        # every line of the saved text; site furniture, contents, status panel and long title are
        # everything up to the long title after the last toolbar line, the footer everything after
        lines = raw.read_text(encoding="utf-8").split("\n")
        tb = max(i for i, l in enumerate(lines) if l.strip() == "Search Act")
        lt = next(i for i in range(tb, len(lines)) if lines[i].startswith("An Act"))
        ft = next(i for i in range(lt, len(lines)) if lines[i].strip() == "Site footer")
        return [(clean(l), None if lt < i < ft else ("site furniture / table of contents / status information / front matter"
                                                      if i <= lt else "site furniture"))
                for i, l in enumerate(lines) if clean(l)]
    # SA: every w:p of the converted RTF, in body order (table cells included)
    from docx import Document
    with tempfile.TemporaryDirectory() as tmp:
        d = Document(str(rtf_to_docx(raw, Path(tmp))))
    styles = {x.style_id: x.name for x in d.styles}
    out, phase = [], "front"
    for p in d.element.body.iter(_W + "p"):
        ppr = p.find(_W + "pPr")
        sid = ppr.find(_W + "pStyle").get(_W + "val") if ppr is not None and ppr.find(_W + "pStyle") is not None else None
        style = styles.get(sid, sid) or ""
        t = clean("".join(x.text or "" for x in p.iter(_W + "t")))
        if style == "leghistoryheadlevel1":
            phase = "end"
        if phase == "front" and style == "partheadlevel1":
            phase = "body"
        if t:
            out.append((t, None if phase == "body" else ("front matter / table of contents" if phase == "front" else "legislative history")))
    return out


def pdf_token_recall(slug: str, stream: list[str]) -> dict:
    """Secondary check for SA/WA: word-token recall of the AUTHORISED PDF's body text against the
    output. Lines repeated on >= 20% of pages (running heads/feet) are page furniture and dropped."""
    from collections import Counter
    a = ACTS[slug]
    txt = subprocess.run(["pdftotext", "-layout", str(DATA / slug / "raw" / a["pdf"]), "-"], capture_output=True, text=True).stdout
    pages = txt.split("\f")
    norm = lambda l: re.sub(r"\d+", "#", " ".join(toks(l)))
    freq = Counter(n for pg in pages for n in {norm(l) for l in pg.splitlines() if l.strip()})
    furniture = {n for n, c in freq.items() if c >= max(3, len(pages) * 0.2)}
    # cut the body region on the RAW text (its end marker is itself a running head), then drop furniture
    if a["jur"] == "SA":
        start = txt.find("enacts as follows")
        start = txt.find("\n", start) if start >= 0 else 0
        end = txt.find("Legislative history", start)
    else:
        # WA: the first body Part heading is the 2nd occurrence of the contents' first Part line
        first = re.search(r"(?m)^\s*(?:Chapter|Part) 1\s*[\u2014-].*$", txt)
        start = txt.find(first.group(0).strip(), first.end()) if first else 0
        m = [x for x in re.finditer(r"(?m)^\s*Notes\s*$", txt) if x.start() > start]
        end = m[0].start() if m else len(txt)
    body = "\n".join(l for l in txt[start:end if end > start else len(txt)].splitlines()
                     if l.strip() and norm(l) not in furniture)
    if a["jur"] == "WA":   # the same excluded class as the HTML side: "[Section 5 inserted: No. 12 of 2019 s. 205.]"
        body = re.sub(r"\[[^\[\]]{0,800}?(inserted|amended|deleted|repealed|replaced|Modification|expired)[^\[\]]{0,800}?\]", " ", body, flags=re.S)
    src = Counter(toks(body))
    out = Counter(stream)
    missing = {t: c - out[t] for t, c in src.items() if c > out[t]}
    total = sum(src.values())
    return dict(pdf_body_tokens=total, recalled=total - sum(missing.values()),
                missing_types=len(missing), missing_top=sorted(missing.items(), key=lambda x: -x[1])[:25])


def gate(slug: str, out_root: Path) -> dict:
    act_dir = out_root / slug
    tree = json.loads((act_dir / "tree.json").read_text())
    tree_paths, titles = [], []

    def walk(n):
        for sct in n.get("sections", []):
            tree_paths.append(sct["path"])
        for k in ("divisions", "subdivisions"):
            for c in n.get(k, []):
                titles.append(c.get("title", ""))
                walk(c)
    for p in tree["parts"]:
        titles.append(p.get("title", ""))
        walk(p)
    disk = sorted(str(f.relative_to(act_dir / "sections")) for f in (act_dir / "sections").rglob("*.md"))
    stream_toks: list[str] = []
    empty_bodies = []
    for rel in tree_paths:
        md = (act_dir / "sections" / rel).read_text(encoding="utf-8")
        body = md.split("\n---\n", 1)[1] if md.startswith("---") else md
        if not re.sub(r"(?m)^#.*$|^---$|^\*.*\*$", "", body).strip():
            empty_bodies.append(rel)
        stream_toks += toks(md.replace(IMAGE_NOTE, " "))
    for t in titles:
        stream_toks += toks(t)
    # space-free: the source often runs a number into the next word ("7ADeclaration", "(1)The")
    stream = "".join(stream_toks)
    paras = source_paragraphs(slug)
    excluded: dict[str, int] = {}
    matched, unrep, nonword = 0, [], 0
    for text, reason in paras:
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        tk = toks(text)
        if not tk:
            nonword += 1
            continue
        if "".join(tk) in stream:
            matched += 1
        else:
            unrep.append(text)
    res = dict(source_paragraphs=len(paras), excluded=excluded, punctuation_only=nonword,
               substantive=len(paras) - sum(excluded.values()) - nonword, matched=matched,
               unrepresented=len(unrep), unrepresented_text=unrep[:50],
               sections=len(tree_paths), files_on_disk=len(disk),
               tree_equals_disk=sorted(tree_paths) == disk, duplicate_paths=len(tree_paths) - len(set(tree_paths)),
               heading_only_sections=len(empty_bodies), heading_only_list=empty_bodies[:40])
    if "pdf" in ACTS[slug]:
        res["pdf_recall"] = pdf_token_recall(slug, stream_toks)
    return res


def index_fts(slugs: list[str]) -> dict:
    """Add the acts' sections to the app's FTS index (search_index.db) with the same routine a full
    rebuild uses (search_service._index_section). Idempotent: an act's rows are replaced, never added
    twice; no other act's rows are touched."""
    import sqlite3
    sys.path.insert(0, str(ROOT))
    from backend.config import SEARCH_DB
    from backend.services.data_loader import load_tree
    from backend.services.search_service import _index_section
    conn = sqlite3.connect(str(SEARCH_DB), timeout=60)
    counts = {}
    with conn:
        for act in slugs:
            conn.execute("DELETE FROM sections_fts WHERE act = ?", (act,))
            conn.execute("DELETE FROM sections_meta WHERE act = ?", (act,))
            seen: set = set()
            for part in load_tree(act).get("parts", []):
                for sec in part.get("sections", []):
                    _index_section(conn, act, sec, part.get("id", ""), "", seen)
                for div in part.get("divisions", []):
                    for sec in div.get("sections", []):
                        _index_section(conn, act, sec, part.get("id", ""), div.get("id", ""), seen)
                    for sub in div.get("subdivisions", []):
                        for sec in sub.get("sections", []):
                            _index_section(conn, act, sec, part.get("id", ""), div.get("id", ""), seen)
            counts[act] = conn.execute("SELECT count(*) FROM sections_meta WHERE act = ?", (act,)).fetchone()[0]
    conn.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--out", default=str(DATA))
    ap.add_argument("--gate", action="store_true", help="run the completeness gate on already-written output")
    ap.add_argument("--index", action="store_true", help="(re)index the acts' sections into search_index.db")
    args = ap.parse_args()
    if args.index:
        for act, n in index_fts(args.slugs or list(ACTS)).items():
            print(f"{act}: fts_rows={n}")
        return
    if args.gate:
        report = {}
        for slug in args.slugs or list(ACTS):
            g = report[slug] = gate(slug, Path(args.out))
            print(f"{slug}: source={g['source_paragraphs']} excluded={sum(g['excluded'].values())} substantive={g['substantive']} "
                  f"matched={g['matched']} unrepresented={g['unrepresented']} sections={g['sections']} disk={g['files_on_disk']} "
                  f"tree==disk={g['tree_equals_disk']} heading_only={g['heading_only_sections']}"
                  + (f" pdf_recall={g['pdf_recall']['recalled']}/{g['pdf_recall']['pdf_body_tokens']}" if 'pdf_recall' in g else ""))
        prev = Path(args.out, "state_tax_gate.json")
        report = {**(json.loads(prev.read_text()) if prev.exists() else {}), **report}
        prev.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
        return
    for slug in args.slugs or list(ACTS):
        lic = json.loads((DATA / slug / "licence.json").read_text())
        r = write_act(slug, Path(args.out), lic)
        print(f"{slug}: parts={len(r['parts'])} sections={len(r['secs'])} terms={r['terms']} "
              f"image_sections={len(r['unrepresented'])}")


if __name__ == "__main__":
    main()
