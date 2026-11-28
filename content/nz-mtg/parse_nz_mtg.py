#!/usr/bin/env python3
"""Parse NZ Master Tax Guide chapter PDFs (CCH) into commentary JSON.

Input : <repo>/data/nz-master-tax-guide/source/*.pdf   (env NZ_MTG_SRC)
Output: <repo>/pipeline/output/nz_master_tax_guide.json (env NZ_MTG_OUT)

Schema (consumed by build_cch_explorer.py):
{"publication": str, "chapters": [
   {"number": "31", "title": "Accident Compensation",
    "major_headings": [
       {"title": str, "paragraph_number": "¶31-010",
        "content_blocks": [{"text": str, "section_refs": [str], "cross_refs": [str]}],
        "sub_headings": [{"title": str, "content_blocks": [...]}]}]}]}

Source format quirks handled:
  - breadcrumb line per page: "Master Tax Guide > Master Tax Guide > CHAPTER > Topic"
  - heading line: "<Title> [¶NN-NNN]" followed by a duplicate "[¶NN-NNN]" line
  - boilerplate: "Click to open document in a browser", "© CCH", bare page numbers
  - chapters split across files ("GST pt 1..10") are merged and ordered by first ¶
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = Path(os.environ.get("NZ_MTG_SRC", ROOT / "data" / "nz-master-tax-guide" / "source"))
OUT = Path(os.environ.get("NZ_MTG_OUT", ROOT / "pipeline" / "output" / "nz_master_tax_guide.json"))

BREADCRUMB_RE = re.compile(r'^\s*Master Tax Guide\s*>.*$', re.M)
CLICK_RE = re.compile(r'^\s*Click to open document in a browser\s*$', re.M | re.I)
COPYRIGHT_RE = re.compile(r'^\s*©\s*CCH.*$', re.M)
PAGENUM_RE = re.compile(r'^\s*\d{1,4}\s*$', re.M)
NAV_RE = re.compile(r'^\s*\[(?:Index|Search|Noteup|Help|Download)\].*$', re.M)
MARKER_ONLY_RE = re.compile(r'^\s*\[¶(?P<para>\d+-\d+)\]\s*$')
HEADING_RE = re.compile(r'^(?P<title>.+?)\s*\[¶(?P<para>\d+-\d+)\]\s*$')
PARA_RE = re.compile(r'¶(?P<para>\d+-\d+)')
# NZ statutory references: numeric (s 170) and alphanumeric (s BD 1, ss CX 5(2))
SECREF_RE = re.compile(
    r'\b(?:s|ss|sec|section)\s+(?:\d{1,3}[A-Z]?|[A-Z]{1,3}\s?\d{1,3}[A-Z]?)(?:\([0-9a-z]{1,4}\))*',
    re.I)
ACTNAME_RE = re.compile(
    r'\b(?:Income Tax Act 2007|Income Tax Act 2004|Goods and Services Tax Act 1985|'
    r'Tax Administration Act 1994|Accident Compensation Act 2001|Companies Act 1993|'
    r'KiwiSaver Act 2006|Student Loan Scheme Act 2011|Estate and Gift Duties Act 1968|'
    r'Taxation \(International Tax\) Act 1994)\b')


def pdftotext(pdf: Path, limit: int | None = None) -> str:
    cmd = ["pdftotext", "-layout"]
    if limit:
        cmd += ["-l", str(limit)]
    cmd += [str(pdf), "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return r.stdout


def strip_breadcrumbs(t: str) -> str:
    """Drop the per-page breadcrumb line plus any wrapped remainder (CDN-0197).

    pdftotext -layout puts the breadcrumb on one line; when the final crumb is
    too long for the text column it wraps, and the remainder lands on the next
    line where BREADCRUMB_RE (anchored to 'Master Tax Guide >') cannot see it.
    Those remainders — 'paymen' / 'assessment', 'child' / 'support' — leaked
    into section bodies as stray lone words (376 across the corpus). The page
    header is always separated from body text by a blank line, and a remainder
    never carries a ¶ marker or sentence punctuation, so both are the guard.
    """
    out, lines, i = [], t.split("\n"), 0
    while i < len(lines):
        if BREADCRUMB_RE.match(lines[i]):
            i += 1
            wrapped = 0
            while i < len(lines) and wrapped < 2:
                nxt = lines[i]
                if not nxt.strip() or "¶" in nxt or len(nxt) > 90 \
                        or nxt.rstrip().endswith(('.', ':', ';', ',')):
                    break
                i += 1
                wrapped += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def clean(raw: str) -> str:
    t = raw.replace("\f", "\n")
    t = strip_breadcrumbs(t)
    t = CLICK_RE.sub("", t)
    t = COPYRIGHT_RE.sub("", t)
    t = NAV_RE.sub("", t)
    t = PAGENUM_RE.sub("", t)
    # MARKER_ONLY lines are NOT dropped here: an own-line marker is a heading
    # whose title wrapped onto the previous line (see split_sections, CDN-0195).
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t


def chapter_title_from_breadcrumb(raw: str, fallback: str) -> str:
    m = re.search(r'Master Tax Guide\s*>\s*Master Tax Guide\s*>\s*(?P<ch>[^>\n]+)>', raw)
    if not m:
        return fallback
    ch = m.group("ch").strip()
    if ch.isupper():
        ch = ch.title()
    return ch.replace("&", "and")


def chapter_number(raw: str) -> str | None:
    counts: dict[str, int] = {}
    for m in PARA_RE.finditer(raw):
        n = m.group("para").split("-")[0]
        counts[n] = counts.get(n, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def first_para_number(raw: str) -> int:
    m = PARA_RE.search(raw)
    return int(m.group("para").split("-")[1]) if m else 10 ** 6


def split_sections(text: str):
    """Yield (heading_title, para_number, body_lines) for each ¶ heading."""
    lines = text.split("\n")
    cur_title, cur_para, buf = None, None, []
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line.strip())
        if m:
            # rescue a wrapped title: previous non-empty line that is not itself content
            title = m.group("title").strip()
            if len(title) < 45 and buf:
                prev = buf[-1].strip()
                if prev and not prev.endswith(('.', ':', ')')) and len(prev) < 60:
                    title = f"{prev} {title}"
                    buf.pop()
            if cur_title is not None:
                yield cur_title, cur_para, buf
            cur_title, cur_para, buf = title, m.group("para"), []
            continue
        mo = MARKER_ONLY_RE.match(line.strip())
        if mo:
            # Own-line marker: the CCH export wraps some headings so the ¶ number
            # is on its own line, and the Precedents chapter does this for every
            # topic. The line above the marker is then the heading TITLE, not body
            # text of the preceding section (CDN-0195: 196 topics were being
            # swallowed into the previous section, incl. all 13 of ch 90).
            prev = buf[-1].strip() if buf else ""
            if prev and len(prev) < 80 and not prev.endswith(('.', ':', ';', ',')):
                buf.pop()
                if cur_title is not None:
                    yield cur_title, cur_para, buf
                cur_title, cur_para, buf = prev, mo.group("para"), []
            # else: the duplicate marker that follows a normal 'Title [¶N-N]' heading
            continue
        if cur_title is None:
            # Before the first heading: keep a short window of preamble lines only.
            # The own-line-marker heading form puts its title on the line directly
            # above the marker, which is all this buffer is needed for.
            buf.append(line)
            del buf[:-3]
        else:
            buf.append(line)
    if cur_title is not None:
        yield cur_title, cur_para, buf


def looks_like_subheading(line: str, nxt: str) -> bool:
    s = line.strip()
    if not s or len(s) > 80 or not nxt:
        return False
    if s.endswith(('.', ':', ';', ',')) or s.startswith(('•', '-', '–')) or MARKER_ONLY_RE.match(s):
        return False
    if re.search(r'\d{4}\s*$', s) and len(s) > 40:
        return False
    words = s.split()
    if len(words) > 12 or len(words) < 1:
        return False
    # allow Title Case or sentence case labels; reject all-lowercase prose fragments
    if s.islower():
        return False
    return True


def build_blocks(lines):
    """Return (content_blocks, sub_headings)."""
    blocks, subs = [], []
    para: list[str] = []

    def flush(target):
        txt = "\n".join(para).strip()
        if not txt:
            return
        txt = re.sub(r'[ \t]+', ' ', txt)
        txt = re.sub(r'\n{2,}', '\n', txt)
        refs = []
        for m in SECREF_RE.finditer(txt):
            v = re.sub(r'\s+', ' ', m.group(0)).strip()
            if v.lower() not in [r.lower() for r in refs]:
                refs.append(v)
        for m in ACTNAME_RE.finditer(txt):
            if m.group(0) not in refs:
                refs.append(m.group(0))
        target.append({
            "text": txt,
            "section_refs": refs[:20],
            "cross_refs": sorted({f"¶{m.group('para')}" for m in PARA_RE.finditer(txt)}),
        })

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < n else ""
        if not line.strip():
            flush(blocks if not subs else subs[-1]["content_blocks"])
            para = []
        elif looks_like_subheading(line, nxt) and not para:
            flush(blocks)
            subs.append({"title": line.strip(), "content_blocks": []})
        else:
            para.append(line.rstrip())
        i += 1
    flush(blocks if not subs else subs[-1]["content_blocks"])
    return blocks, subs


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    pdfs = sorted(SRC.glob("*.pdf"))
    if not pdfs:
        print("no source PDFs found in", SRC)
        return 1

    # pass 1: read + group by chapter number
    groups: dict[str, dict] = {}
    ungrouped = []
    for pdf in pdfs:
        raw = pdftotext(pdf)
        num = chapter_number(raw)
        if not num:
            ungrouped.append(pdf)
            continue
        g = groups.setdefault(num, {"title": chapter_title_from_breadcrumb(raw, pdf.stem),
                                    "parts": []})
        g["parts"].append((first_para_number(raw), pdf.name, pdf, raw))
        print(f"  {pdf.name}: chapter {num} (first ¶..{first_para_number(raw)})")

    chapters = []
    for num in sorted(groups, key=lambda x: int(x)):
        g = groups[num]
        g["parts"].sort(key=lambda t: t[0])
        merged = "\n".join(p[3] for p in g["parts"])
        text = clean(merged)
        headings = []
        for title, para, body in split_sections(text):
            blocks, subs = build_blocks(body)
            if not blocks and not subs:
                continue
            headings.append({
                "title": re.sub(r'\s+', ' ', title).strip(),
                "paragraph_number": f"¶{para}",
                "content_blocks": blocks,
                "sub_headings": subs,
            })
        chapters.append({"number": num, "title": g["title"], "major_headings": headings})
        print(f"chapter {num} — {g['title']}: {len(headings)} topics from {len(g['parts'])} file(s)")

    data = {"publication": "New Zealand Master Tax Guide", "chapters": chapters}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    topics = sum(len(c["major_headings"]) for c in chapters)
    print(f"\nwrote {OUT} — {len(chapters)} chapters, {topics} topics, {OUT.stat().st_size:,} bytes")
    if ungrouped:
        print("ungrouped (no ¶ markers):", [p.name for p in ungrouped])
    return 0


if __name__ == "__main__":
    sys.exit(main())
