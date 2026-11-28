#!/usr/bin/env python3
"""
NZ Inland Revenue — Tax Technical bulk scraper.

Mirrors the ATO pattern in scripts/bulk_scrape_rulings.py: one .txt per document,
named <TYPE>_<YEAR>_<N>.txt so data_loader.load_rulings() picks it up unchanged.

Source: https://www.taxtechnical.ird.govt.nz (server-rendered, robots.txt allows all
except /temp, /sitecore, /app_data). Every document URL is enumerable from sitemap.xml.

Per doc: fetch landing page -> metadata (citation, title, issued date, TIB ref) ->
download the PDF -> extract text. Text comes from the PDF because the landing page is
a PDF stub (~1.2 KB of chrome), not the document.

Ingestion ruleset compliance:
  * 7.4  Title is taken from the document heading, NEVER from the URL slug.
  * 7.5  nfkc_ligatures() is applied to all served text (U+FB00-06 only).
  * 4.2  Page furniture (nav, footer, running headers) stripped before writing.

Usage:
  python3 scripts/nz_scrape_rulings.py --limit 5            # smoke test
  python3 scripts/nz_scrape_rulings.py --families rulings    # one family
  python3 scripts/nz_scrape_rulings.py                       # everything
"""
from __future__ import annotations
import os

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl
import fitz  # pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.text_normalize import nfkc_ligatures  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("nz_scrape")

BASE = "https://www.taxtechnical.ird.govt.nz"
REPO = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = REPO / "data" / "nz_rulings"
PDF_DIR = REPO / "data" / "nz_rulings_pdf"
STATE = REPO / "data" / "nz_scrape_state.json"
URLS = Path(os.path.join(os.path.expanduser(os.environ.get("LAWKITTY_HOME", "~/.lawkitty")), "cache", "scratch", "nz", "urls.json"))

DELAY = 0.8          # polite gap between requests
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-NZ,en;q=0.9"}

# family -> (filename TYPE token, human label)
FAMILY_TYPE = {
    "rulings":                        ("BRPUB", "Public ruling"),
    "technical-decision-summaries":   ("TDS",   "Technical decision summary"),
    "interpretation-statements":      ("IS",    "Interpretation statement"),
    "questions-we-ve-been-asked":     ("QB",    "Questions we've been asked"),
    "standard-practice-statements":   ("SPS",   "Standard practice statement"),
    "operational-statements":         ("OS",    "Operational statement"),
    "operational-positions":          ("OP",    "Operational position"),
    "determinations":                 ("DET",   "Determination"),
    "commissioner-s-statements":      ("CS",    "Commissioner's statement"),
    "interpretation-guidelines":      ("IG",    "Interpretation guideline"),
    "operational-guidelines":         ("OG",    "Operational guideline"),
}

# CITATION_PATTERNS run against the page heading text, most specific first.
CITATION_PATTERNS = [
    (re.compile(r"\b(BR\s*Pub)\s+(\d{2})\s*[/-]\s*(\d+)", re.I), "BRPUB"),
    (re.compile(r"\b(PR\s*Pub)\s+(\d{2})\s*[/-]\s*(\d+)", re.I), "PRPUB"),
    (re.compile(r"\b(BR\s*Prd)\s+(\d{2})\s*[/-]\s*(\d+)", re.I), "BRPRD"),
    (re.compile(r"\b(TDS)\s+(\d{2})\s*[/-]\s*(\d+)", re.I), "TDS"),
    (re.compile(r"\b(IS)\s*(\d{2})\s*[/-]\s*(\d+)", re.I), "IS"),
    (re.compile(r"\b(QB)\s*(\d{2})\s*[/-]\s*(\d+)", re.I), "QB"),
    (re.compile(r"\b(IG)\s*(\d{2})\s*[/-]\s*(\d+)", re.I), "IG"),
]

# Landing-page furniture: nav labels present on every page. Dropped from the
# extracted text so they never reach the corpus.
NOISE = {
    "Skip to main content", "Home", "Publications", "Ngā putanga", "Consultation",
    "Whai Tohutohu", "Work Programmes", "Hōtaka mahi", "Apply for ruling",
    "Te tono whakataunga", "Modify legislation", "Whakarerekē Ture",
    "Toggle mobile nav", "Tax Technical", "About", "Keep up to date",
    "IR main site", "IR Tax Policy", "Contact us", "Website feedback",
    "Jump back to the top of the page", "top", "pdf", "Report a Problem with this Page or Publication",
}


# Citation-derived type -> the label written into the document header.
TYPE_LABEL = {
    "BRPUB": "Public ruling", "PRPUB": "Public ruling", "BRPRD": "Product ruling",
    "TDS": "Technical decision summary", "IS": "Interpretation statement",
    "QB": "Questions we've been asked", "SPS": "Standard practice statement",
    "OS": "Operational statement", "OP": "Operational position",
    "DET": "Determination", "CS": "Commissioner's statement",
    "IG": "Interpretation guideline", "OG": "Operational guideline",
}


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"done": {}, "failed": {}}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=1))


def strip_html(html: str) -> str:
    """Cheap, dependency-light HTML to text. Keeps block boundaries as newlines."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    main = soup.find(class_=re.compile(r"tt-item__content")) or soup.find(id="content") or soup
    text = main.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    out, seen = [], set()
    for ln in lines:
        if not ln or ln in NOISE or ln in seen:
            continue
        if re.fullmatch(r"[\d\s./:-]{0,12}", ln):   # bare dates/page numbers of no value
            continue
        seen.add(ln)
        out.append(ln)
    return "\n".join(out)


def parse_landing(html: str, url: str, family: str) -> dict | None:
    """Pull citation, title, issued date and the PDF href off a landing page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    # Page title (the document heading) - rule 7.4: from the document, not the slug.
    h1 = soup.find("h1")
    page_title = h1.get_text(" ", strip=True) if h1 else ""
    if not page_title:
        t = soup.find("title")
        page_title = t.get_text(" ", strip=True) if t else ""
    page_title = re.sub(r"\s*\|\s*Tax Technical.*$", "", page_title).strip()

    body_text = strip_html(html)

    # Citation: prefer the strongest pattern in the first 2000 chars of content.
    head = body_text[:2000]
    citation, ctype, cyear, cnum = None, None, None, None
    for pat, tcode in CITATION_PATTERNS:
        m = pat.search(head)
        if m:
            citation = f"{m.group(1)} {m.group(2)}/{m.group(3)}"
            citation = re.sub(r"\s+", " ", citation)
            ctype = tcode
            yy = int(m.group(2))
            cyear = 2000 + yy if yy < 90 else 1900 + yy
            cnum = int(m.group(3))
            break

    # Type from the URL path when the landing page carries no citation. The /rulings/
    # family splits in the URL itself (/rulings/product/ vs /rulings/public/) and those
    # pages frequently show no citation in their first 3 KB.
    if not ctype:
        if "/rulings/product/" in url:
            ctype = "BRPRD"
        elif "/rulings/public/" in url:
            ctype = "PRPUB" if re.search(r"\bpr[-\s]?pub\b", url, re.I) else "BRPUB"

    # Year fallback: a /19xx/ or /20xx/ segment in the URL.
    url_year = None
    my = re.search(r"/((?:19|20)\d{2})/", url)
    if my:
        url_year = int(my.group(1))

    # Issued date. IRD uses two shapes: "Issued 18 Oct 2021" and the year-only
    # "Issued 2017" (year-only was missed by the first pass and cost 40 documents
    # their year). Capture both.
    issued = None
    md = re.search(r"Issued\s+(\d{1,2}\s+\w{3,9}\s+\d{4})", body_text)
    if md:
        issued = md.group(1)
    else:
        my2 = re.search(r"Issued\s+((?:19|20)\d{2})\b", body_text)
        if my2:
            issued = my2.group(1)

    # TIB reference
    tib = None
    mt = re.search(r"(Tax Information Bulletin\s+Vol\s+\d+\s+No\s+\d+[^\n]*)", body_text)
    if mt:
        tib = mt.group(1).strip()

    # PDF href
    pdfs = [a.get("href") for a in soup.find_all("a", href=True)
            if a.get("href", "").lower().endswith(".pdf")]
    # Some hrefs carry ?modified=... so endswith fails; retry on the path.
    if not pdfs:
        pdfs = [a.get("href") for a in soup.find_all("a", href=True)
                if ".pdf" in a.get("href", "").lower()]
    pdf = pdfs[0] if pdfs else None
    if pdf and pdf.startswith("/"):
        pdf = BASE + pdf

    return {
        "url": url, "family": family, "citation": citation, "ctype": ctype,
        "cyear": cyear, "cnum": cnum, "url_year": url_year,
        "title": page_title, "issued": issued, "tib": tib, "pdf": pdf,
        "landing_text": body_text,
    }


def pdf_text(url: str) -> str | None:
    try:
        r = curl.get(url, headers=HEADERS, timeout=90, impersonate="chrome")
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            return None
        doc = fitz.open(stream=r.content, filetype="pdf")
        return "\n".join(p.get_text() for p in doc)
    except Exception as e:
        log.warning("pdf failed %s: %s", url, e)
        return None


def clean_pdf_text(text: str) -> str:
    """Ruleset 4.2: drop page furniture, collapse wrap, keep paragraph breaks."""
    text = nfkc_ligatures(text)
    text = text.replace("\x0c", "\n\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]

    out: list[str] = []
    drop_pats = [
        re.compile(r"^UNCLASSIFIED$", re.I),
        re.compile(r"^Inland Revenue\s*$", re.I),
        re.compile(r"^\s*\d{1,3}\s*$"),                       # bare page numbers
        re.compile(r"^Tax Information Bulletin Vol", re.I),   # TIB running footer
    ]
    for ln in lines:
        if any(p.match(ln) for p in drop_pats):
            continue
        out.append(ln)

    # Rebuild paragraphs: join a line to the previous when it is a wrap, not a break.
    paras: list[str] = []
    buf = ""
    for ln in out:
        if not ln:
            if buf:
                paras.append(buf); buf = ""
            continue
        if not buf:
            buf = ln
        elif buf.endswith(("-", "\u2010")) and not buf.endswith((" -", "\u2010 ")):
            buf = buf[:-1] + ln          # de-hyphenate a wrapped word
        elif re.match(r"^[a-z0-9(\"'\u2018\u201c]", ln) and not buf.endswith((".", ":", ";", "!", "?")):
            buf += " " + ln              # continuation of the same sentence
        else:
            paras.append(buf); buf = ln
    if buf:
        paras.append(buf)
    return "\n\n".join(paras)


def make_stem(meta: dict, seq: dict, existing: set[str] | None = None) -> tuple[str, str, int, int]:
    """<TYPE>_<YEAR>_<N>.txt - matches data_loader.load_rulings() regex.

    ``existing`` is the set of stems already on disk. Numbering must be collision-aware:
    a citation-derived number ("QB 22/08" -> n=8) and the sequential counter for slug-only
    URLs draw from the same pool, so without this check the two collided and 62 documents
    were silently dropped on the first pass.
    """
    existing = existing or set()
    fam_type, _ = FAMILY_TYPE[meta["family"]]
    tcode = meta.get("ctype") or fam_type
    # rulings family splits into BRPUB / PRPUB / BRPRD by citation
    if meta["family"] == "rulings" and not meta.get("ctype"):
        tcode = "BRPUB"
    year = meta.get("cyear") or meta.get("url_year") or 0
    if meta.get("cnum"):
        n = meta["cnum"]
    else:
        key = f"{tcode}_{year}"
        seq[key] = seq.get(key, 0) + 1
        n = seq[key]
    # never hand out a number already spoken for
    while f"{tcode}_{year}_{n}" in existing:
        n += 1
    key = f"{tcode}_{year}"
    seq[key] = max(seq.get(key, 0), n)
    return f"{tcode}_{year}_{n}", tcode, year, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", help="comma list; default all")
    ap.add_argument("--limit", type=int, default=0, help="stop after N docs (smoke test)")
    ap.add_argument("--delay", type=float, default=DELAY)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    buckets = json.loads(URLS.read_text())
    fams = args.families.split(",") if args.families else list(FAMILY_TYPE)
    state = load_state()

    # Seed the sequence counter from what is already on disk so reruns do not collide.
    seq: dict[str, int] = {}
    for f in OUT_DIR.glob("*.txt"):
        m = re.match(r"^([A-Za-z]+)_(\d{2,4})_(\d+)$", f.stem)
        if m:
            k = f"{m.group(1).upper()}_{m.group(2)}"
            seq[k] = max(seq.get(k, 0), int(m.group(3)))

    manifest = []
    mpath = OUT_DIR / "nz_rulings_list.json"
    if mpath.exists():
        try:
            manifest = json.loads(mpath.read_text())
        except Exception:
            manifest = []
    # Dedup key is the SOURCE URL, never the filename. Keying on the filename let two
    # different documents collide on one name and the second overwrote the first,
    # silently losing 62 documents on the first pass.
    by_url = {m["url"]: m for m in manifest if m.get("url")}
    existing_stems = {Path(m["source"]).stem for m in manifest if m.get("source")}
    existing_stems |= {f.stem for f in OUT_DIR.glob("*.txt")}

    todo = []
    for fam in fams:
        for u in buckets.get(fam, []):
            if u not in state["done"]:
                todo.append((fam, u))
    if args.limit:
        todo = todo[:args.limit]
    log.info("queued %d documents across %d families", len(todo), len(fams))

    ok = err = 0
    for i, (fam, url) in enumerate(todo, 1):
        try:
            r = curl.get(url, headers=HEADERS, timeout=60, impersonate="chrome")
            if r.status_code != 200:
                state["failed"][url] = f"http {r.status_code}"
                err += 1
                continue
            meta = parse_landing(r.text, url, fam)
            if meta is None:
                state["failed"][url] = "unparseable landing page"
                err += 1
                continue

            text = ""
            if meta["pdf"]:
                raw = pdf_text(meta["pdf"])
                if raw:
                    text = clean_pdf_text(raw)
            if not text:
                # Fall back to the landing page text so nothing is silently empty.
                text = clean_pdf_text(meta["landing_text"])

            stem, tcode, year, n = make_stem(meta, seq, existing_stems)
            fname = f"{stem}.txt"
            if url in by_url:
                state["done"][url] = Path(by_url[url]["source"]).name
                continue

            header = (
                f"{meta['title']}\n"
                f"{meta['citation'] or stem}\n"
                f"Type: {TYPE_LABEL.get(tcode, FAMILY_TYPE[fam][1])}\n"
                f"Jurisdiction: New Zealand\n"
                f"Issued: {meta['issued'] or 'n/a'}\n"
                f"TIB: {meta['tib'] or 'n/a'}\n"
                f"Source: {url}\n"
                f"{'=' * 60}\n\n"
            )
            (OUT_DIR / fname).write_text(header + text, encoding="utf-8")

            manifest.append({
                "citation": meta["citation"] or stem,
                "title": stem,
                "full_title": meta["title"],
                "type": tcode,
                "year": year,
                "jurisdiction": "NZ",
                "issued": meta["issued"],
                "withdrawn": False,
                "source": str(OUT_DIR / fname),
                "url": url,
                "has_text": bool(text),
                "chars": len(text),
                "preview": (header + text)[:400],
            })
            existing_stems.add(stem)
            state["done"][url] = fname
            ok += 1

            if i % 25 == 0:
                mpath.write_text(json.dumps(manifest, indent=1))
                save_state(state)
                log.info("%d/%d  ok=%d err=%d  latest=%s (%d chars)",
                         i, len(todo), ok, err, fname, len(text))
            time.sleep(args.delay)
        except Exception as e:
            log.warning("fail %s: %s", url, e)
            state["failed"][url] = str(e)
            err += 1

    mpath.write_text(json.dumps(manifest, indent=1))
    save_state(state)
    log.info("DONE ok=%d err=%d manifest=%d", ok, err, len(manifest))
    print(json.dumps({"ok": ok, "err": err, "manifest": len(manifest),
                      "out_dir": str(OUT_DIR)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
