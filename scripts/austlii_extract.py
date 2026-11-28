#!/usr/bin/env python3
"""Extract judgment text from saved AustLII pages into clean text, one file per case.

    python austlii_extract.py SRC_DIR OUT_DIR [--limit N]

SRC_DIR holds <year>_<COURT>_<n>.html (AustLII judgment pages) and .txt (page text dumps).
For HTML the judgment is the <article class="the-document"> element; everything outside it
(menus, search panels, side panels, footer) is dropped. Ordered-list paragraphs keep their
numbers ("[12] ..."), tables become pipe-separated rows. For the text dumps, the judgment runs
from the first title line carrying the citation to the "PRINT" side panel.

OUT_DIR/<stem>.txt   the judgment, with a short header block
OUT_DIR/index.jsonl  one row per case: stem, citation, title, court, date, chars, paragraphs, status
Never modifies SRC_DIR.
"""
from __future__ import annotations

import argparse
import html as htmlmod
import json
import os
import re
import sys
from multiprocessing import Pool
from pathlib import Path

BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol",
         "section", "article", "center", "dl", "dt", "dd", "hr", "address", "form"}
STEM = re.compile(r"^(\d{4})_([A-Za-z]+)_(\d+)$")
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


def iso_date(s: str) -> str | None:
    m = re.search(r"\((\d{1,2}) ([A-Za-z]+) (\d{4})\)\s*$", s or "")
    if not m or m.group(2).lower() not in MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


WS = re.compile(r"\s+")
FN_NOTE = re.compile(r"fn(\d+)$")
FN_BACK = re.compile(r"fnB(\d+)$")
FN_REF = re.compile(r"#fn(\d+)$")
FN_MARK = "\x00FN\x00"  # replaced in tidy(): the first footnote opens a "## Footnotes" section


def _t(s: str | None, pre: bool) -> str:
    """Text node as a browser shows it: runs of whitespace (incl. source line wraps) are one space."""
    if not s:
        return ""
    return s if pre else WS.sub(" ", s)


def render(el, out: list[str], pre: bool = False) -> None:
    """Depth-first text of an lxml element with block, list and table structure kept."""
    if not isinstance(el.tag, str):  # comments and processing instructions (AustLII build notes)
        out.append(_t(el.tail, pre))
        return
    tag = el.tag.lower()
    if tag in ("script", "style", "noscript", "head", "nav", "button", "select"):
        out.append(_t(el.tail, pre))
        return
    pre = pre or tag == "pre"
    if tag == "tr":
        # one line per row: cells rendered on their own, inner breaks folded, joined by " | "
        cells = []
        for td in el:
            if isinstance(td.tag, str) and td.tag.lower() in ("td", "th"):
                buf: list[str] = []
                buf.append(_t(td.text, pre))
                for ch in td:
                    render(ch, buf, pre)
                c = WS.sub(" ", "".join(buf)).strip()
                if c:
                    cells.append(c)
        if cells:
            out.append("\n" + " | ".join(cells) + "\n")
        out.append(_t(el.tail, pre))
        return
    if tag == "a" and FN_NOTE.match(el.get("name") or ""):
        # AustLII footnote body: <a name="fn12" href="#fnB12">[12]</a> at the start of its <p>
        out.append(FN_MARK + f"[fn{FN_NOTE.match(el.get('name')).group(1)}]")
        out.append(_t(el.tail, pre))
        return
    if tag == "a" and FN_REF.match(el.get("href") or "") and FN_BACK.match(el.get("name") or ""):
        # the reference in the text: <a name="fnB12" href="#fn12">[12]</a>
        out.append(f"[fn{FN_REF.match(el.get('href')).group(1)}]")
        out.append(_t(el.tail, pre))
        return
    if tag == "span" and "listBullet" in (el.get("class") or ""):
        # Federal Court site: the paragraph number is a span at the start of a ParaNumbering <p>
        n = (el.text_content() or "").strip()
        out.append(f"[{n}] " if n.isdigit() else n + " ")
        out.append(_t(el.tail, pre))
        return
    if tag == "br":
        out.append("\n")
    elif tag == "li":
        v = el.get("value")
        out.append("\n\n" + (f"[{v}] " if v and v.isdigit() else "- "))
    elif tag in BLOCK:
        out.append("\n\n")
    out.append(_t(el.text, pre))
    for ch in el:
        render(ch, out, pre)
    if tag in BLOCK or tag == "li":
        out.append("\n\n")
    out.append(_t(el.tail, pre))


def tidy(text: str) -> str:
    # Footnotes go under their own heading as [fnN], so a pinpoint [71] never matches footnote 71.
    first = text.find(FN_MARK)
    if first >= 0:
        text = text[:first] + "\n\n## Footnotes\n\n" + text[first:].replace(FN_MARK, "")
    text = htmlmod.unescape(text).replace("\xa0", " ").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    # AustLII markers that survive as text in some pages
    lines = [l for l in lines if not re.match(r"^(make_database\b|sino (date|noindex)\b|section (break|end) \(\))", l)]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def from_html(raw: str) -> tuple[str, str]:
    import lxml.html
    doc = lxml.html.fromstring(raw.encode("utf-8") if isinstance(raw, str) else raw)
    title = (doc.findtext(".//title") or "").strip()
    node = None
    for xp in ("//article[contains(concat(' ', normalize-space(@class), ' '), ' the-document ')]",  # AustLII
               "//div[contains(@class, 'docx_judgment_content')]",                              # Federal Court site
               "//*[@id='page-main']", "//*[@id='content']"):
        hit = doc.xpath(xp)
        if hit:
            node = hit[0]
            break
    if node is None:
        return title, ""
    out: list[str] = []
    render(node, out)
    return title, tidy("".join(out))


TXT_START = re.compile(r"\[\d{4}\]\s+[A-Za-z]+\s+\d+\s+\(\d{1,2} [A-Za-z]+ \d{4}\)")


def from_txt(raw: str) -> tuple[str, str]:
    lines = raw.replace("\r", "").split("\n")
    start = next((i for i, l in enumerate(lines) if TXT_START.search(l)), None)
    if start is None:
        return "", ""
    end = next((i for i in range(len(lines) - 1, start, -1)
                if lines[i].strip() == "PRINT" and i + 1 < len(lines) and lines[i + 1].startswith("Print")), len(lines))
    return lines[start].strip(), tidy(txt_footnotes(lines[start:end]))


PARA = re.compile(r"^\[(\d+)\]\s")


def txt_footnotes(lines: list[str]) -> str:
    """A page text dump loses the footnote markup: the footnotes are where the [n] numbering
    starts again at [1] after the paragraphs. From there, "[n] " becomes "[fnn] " under a heading."""
    top, cut = 0, None
    for i, line in enumerate(lines):
        m = PARA.match(line.strip())
        if not m:
            continue
        n = int(m.group(1))
        if n == 1 and top >= 2:
            cut = i
            break
        top = max(top, n)
    if cut is None:
        return "\n".join(lines)
    notes = [PARA.sub(lambda m: f"[fn{m.group(1)}] ", line.strip()) for line in lines[cut:]]
    return "\n".join(lines[:cut] + ["", "## Footnotes", ""] + notes)


def work(args):
    path, out_dir = args
    p = Path(path)
    m = STEM.match(p.stem)
    row = {"stem": p.stem, "source": p.suffix.lstrip(".")}
    if m:
        row.update(year=int(m.group(1)), court=m.group(2).upper(), num=int(m.group(3)),
                   citation=f"[{m.group(1)}] {m.group(2).upper()} {int(m.group(3))}")
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        title, body = from_html(raw) if p.suffix.lower() == ".html" else from_txt(raw)
    except Exception as e:
        row.update(status="error", reason=repr(e)[:200])
        return row
    title = re.sub(r"\s+", " ", title).strip()
    date = iso_date(title)
    if not date:  # Federal Court site titles carry no date: take it from the cover sheet
        m2 = re.search(r"Date of (?:judgment|decision)\s*:?\s*\|?\s*(\d{1,2} [A-Za-z]+ \d{4})", body)
        date = iso_date(f"({m2.group(1)})") if m2 else None
    row.update(title=title, date=date, source_site="fedcourt" if "docx_judgment_content" in raw else "austlii",
               chars=len(body),
               paragraphs=len(re.findall(r"(?m)^\[\d+\]\s|^\d{1,4}\s{1,3}[A-Z]", body.split("\n## Footnotes\n")[0])),
               footnotes=len(re.findall(r"(?m)^\[fn\d+\]", body)))
    if len(body) < 400:
        row.update(status="empty" if len(body) < 50 else "short")
    else:
        row["status"] = "ok"
    if body:
        head = f"{title}\n\nCitation: {row.get('citation', '')}\nCourt: {row.get('court', '')}\n"
        if row.get("date"):
            head += f"Date: {row['date']}\n"
        head += "Source: " + ("Federal Court of Australia" if row["source_site"] == "fedcourt" else "AustLII") + "\n\n---\n\n"
        (Path(out_dir) / f"{p.stem}.txt").write_text(head + body, encoding="utf-8")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--retry-failed", action="store_true", help="redo only cases not 'ok' in OUT/index.jsonl")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    if out.resolve() == src.resolve() or str(out.resolve()).startswith(str(src.resolve()) + os.sep):
        raise SystemExit("OUT must be outside SRC")
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(str(f) for f in src.iterdir() if f.suffix.lower() in (".html", ".txt") and STEM.match(f.stem))
    # a case saved as both .html and .txt: the HTML (structured) wins
    stems = {}
    for f in files:
        s = Path(f).stem
        if s not in stems or f.endswith(".html"):
            stems[s] = f
    todo = list(stems.values())[: a.limit] if a.limit else list(stems.values())
    keep = {}
    if a.retry_failed and (out / "index.jsonl").is_file():
        prev = [json.loads(l) for l in (out / "index.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        keep = {r["stem"]: r for r in prev if r.get("status") == "ok"}
        todo = [f for f in todo if Path(f).stem not in keep]
        print(f"retrying {len(todo)} of {len(prev)}", flush=True)
    rows = list(keep.values())
    with Pool(a.workers) as pool:
        for i, r in enumerate(pool.imap_unordered(work, ((f, str(out)) for f in todo), chunksize=64)):
            rows.append(r)
            if i % 5000 == 0:
                print(f"{i}/{len(todo)}", flush=True)
    rows.sort(key=lambda r: r["stem"])
    with open(out / "index.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print("status:", dict(Counter(r["status"] for r in rows)))
    print("by court:", dict(Counter(r.get("court") for r in rows)))
    print("chars total:", sum(r.get("chars", 0) for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
