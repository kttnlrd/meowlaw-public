#!/usr/bin/env python3
"""
Repair pass for the NZ scrape: recover the real year for documents that landed as
<TYPE>_0_<N>.txt, then renumber and rename into <TYPE>_<YEAR>_<N>.txt.

Why this exists
---------------
The first scrape derived the year from (a) a citation on the landing page or (b) a
/year/ segment in the URL. Determinations, standard practice statements, operational
statements, positions and older interpretation statements have NEITHER: their URLs are
slug-only (/determinations/accounting-income-method/det-a4-...) and their landing pages
carry no citation. 627 of 1,534 documents fell through to year 0.

Year 0 is not cosmetic. data_loader.load_rulings() matches ^([A-Za-z]+)_(\\d{2,4})_(\\d+),
so a 1-digit year fails the match entirely and the document silently becomes type "LCG",
year 0 - the exact failure mode the PS LA comments in that loader call LK-11.

Recovery order (first hit wins):
  1. The document's own citation in the body      "OS 09/01", "CS 12/01", "DET A10"
  2. The Issued date captured from the landing page
  3. A date in the first 4 KB of the document body
  4. A /year/ segment in the source URL
Anything still unresolved is reported, never guessed.

Numbering within a (TYPE, YEAR) group: the document's own citation number where we have
one, otherwise sequential in stable order of the original number.

Usage:
  python3 scripts/nz_repair_years.py --dry-run
  python3 scripts/nz_repair_years.py
"""
from __future__ import annotations
import os

import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "nz_rulings"))
MANIFEST = OUT / "nz_rulings_list.json"

MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
          "November|December")
DATE_RE = re.compile(rf"(\d{{1,2}})\s+({MONTHS})\s+((?:19|20)\d{{2}})", re.I)
MY_RE = re.compile(rf"\b({MONTHS})\s+((?:19|20)\d{{2}})\b", re.I)
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

# Citation in the document body. Groups: (year2, number) or a bare DET-style letter+number.
CITE_RE = {
    "IS":  re.compile(r"\bIS\s+(\d{2})\s*/\s*(\d+)", re.I),
    "OS":  re.compile(r"\bOS\s+(\d{2})\s*/\s*(\d+)", re.I),
    "OP":  re.compile(r"\bOP\s+(\d{2})\s*/\s*(\d+)", re.I),
    "SPS": re.compile(r"\bSPS\s+(\d{2})\s*/\s*(\d+)", re.I),
    "CS":  re.compile(r"\bCS\s+(\d{2})\s*/\s*(\d+)", re.I),
    "IG":  re.compile(r"\bIG\s+(\d{2})\s*/\s*(\d+)", re.I),
    "OG":  re.compile(r"\bOG\s+(\d{2})\s*/\s*(\d+)", re.I),
    "QB":  re.compile(r"\bQB\s+(\d{2})\s*/\s*(\d+)", re.I),
    "TDS": re.compile(r"\bTDS\s+(\d{2})\s*/\s*(\d+)", re.I),
}
DET_RE = re.compile(r"\bDET\s+([A-Z]?\d+[A-Z]?)\b", re.I)

TYPE_LABEL = {
    "BRPUB": "Public ruling", "PRPUB": "Public ruling", "BRPRD": "Product ruling",
    "TDS": "Technical decision summary", "IS": "Interpretation statement",
    "QB": "Questions we've been asked", "SPS": "Standard practice statement",
    "OS": "Operational statement", "OP": "Operational position",
    "DET": "Determination", "CS": "Commissioner's statement",
    "IG": "Interpretation guideline", "OG": "Operational guideline",
}


def two_digit_year(yy: int) -> int:
    return 2000 + yy if yy < 90 else 1900 + yy


def head_of(path: Path) -> tuple[str, str]:
    """Return (header_block, body) for one corpus file."""
    txt = path.read_text(errors="replace")
    parts = txt.split("=" * 60, 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def recover(meta: dict) -> tuple[int | None, str | None, str]:
    """Return (year, citation_string_or_None, how_it_was_found)."""
    path = Path(meta["source"])
    _, body = head_of(path)
    t = meta["type"]
    search = body[:6000]

    # 1. the document's own citation
    if t == "DET":
        m = DET_RE.search(search)
        if m:
            cite = f"DET {m.group(1).upper()}"
            return None, cite, "citation(DET)"   # year still needed separately
    else:
        m = CITE_RE.get(t, re.compile(r"(?!x)x")).search(search)
        if m:
            yy, num = int(m.group(1)), int(m.group(2))
            cite = f"{t} {m.group(1)}/{m.group(2)}"
            return two_digit_year(yy), cite, "citation"

    # 2. Issued date from the landing page
    if meta.get("issued"):
        d = YEAR_RE.search(meta["issued"])
        if d:
            return int(d.group(1)), None, "issued"

    # 3. A date in the body head
    d = DATE_RE.search(body[:4000]) or MY_RE.search(body[:4000])
    if d:
        g = d.groups()
        return int(g[-1]), None, "body-date"

    # 4. Year in the source URL
    u = re.search(r"/((?:19|20)\d{2})/", meta["url"])
    if u:
        return int(u.group(1)), None, "url"

    return None, None, "UNRESOLVED"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text())
    work = [m for m in manifest if m["year"] == 0]
    print(f"repairing {len(work)} of {len(manifest)} entries\n")

    # ---- pass 1: recover year + citation per document
    how = Counter()
    unresolved = []
    for m in work:
        year, cite, src = recover(m)
        m["_year"] = year
        m["_cite"] = cite
        m["_how"] = src
        if year is None and not (m["type"] == "DET" and cite):
            unresolved.append(m)
            how["UNRESOLVED"] += 1
        else:
            how[src] += 1

    print("=== year recovery method ===")
    for k, v in how.most_common():
        print(f"  {k:14s} {v}")

    # DET citations carry no year: take it from the issued date / body for those too.
    for m in work:
        if m.get("_year") is None and m.get("_cite"):
            for cand in (m.get("issued"),):
                if cand:
                    d = YEAR_RE.search(cand)
                    if d:
                        m["_year"] = int(d.group(1))
                        break
            if m["_year"] is None:
                _, body = head_of(Path(m["source"]))
                d = DATE_RE.search(body[:4000]) or MY_RE.search(body[:4000])
                if d:
                    m["_year"] = int(d.groups()[-1])

    still = [m for m in work if m.get("_year") is None]
    if still:
        print(f"\n=== {len(still)} STILL UNRESOLVED (not guessed) ===")
        for m in still:
            print(f"  {Path(m['source']).name:16s} {m['type']:5s} {m['url'][-56:]}")
    else:
        print("\nall years resolved")

    if args.dry_run:
        yrs = Counter(m["_year"] for m in work if m.get("_year"))
        print("\n=== resulting year distribution (top 10) ===")
        for y, n in sorted(yrs.items()):
            print(f"  {y}: {n}")
        return 0

    # ---- pass 2: work out the new numbers, then rename in two phases
    # Existing good files (year != 0) keep their names, but their numbers are part of
    # the per-(type,year) sequence, so seed the used-number map from them first.
    used: dict[tuple[str, int], set[int]] = defaultdict(set)
    for m in manifest:
        if m["year"]:
            mm = re.match(r"^([A-Za-z]+)_(\d{2,4})_(\d+)$", Path(m["source"]).stem)
            if mm:
                used[(mm.group(1).upper(), int(mm.group(2)))].add(int(mm.group(3)))

    plan = []
    for m in work:
        year = m.get("_year")
        if year is None:
            continue
        t = m["type"]
        mm = re.match(r"^([A-Za-z]+)_(\d{2,4})_(\d+)$", Path(m["source"]).stem)
        orig_n = int(mm.group(3)) if mm else 0
        # prefer the document's own number when its citation gave one
        n = None
        if m.get("_cite"):
            cm = re.search(r"/(\d+)$", m["_cite"])
            if cm:
                n = int(cm.group(1))
            else:
                dm = re.search(r"DET\s+[A-Z]?(\d+)", m["_cite"], re.I)
                if dm:
                    n = int(dm.group(1))
        if n is None or n in used[(t, year)]:
            n = (max(used[(t, year)]) + 1) if used[(t, year)] else orig_n
            while n in used[(t, year)]:
                n += 1
        used[(t, year)].add(n)
        plan.append((m, t, year, n))

    # phase A: move originals aside
    tmp = []
    for m, t, year, n in plan:
        src = Path(m["source"])
        holder = src.with_name(f"__tmp__{src.name}")
        src.rename(holder)
        tmp.append((holder, m, t, year, n))

    # phase B: write final name + rewritten header
    renamed = 0
    for holder, m, t, year, n in tmp:
        _, body = head_of(holder)
        new_name = f"{t}_{year}_{n}.txt"
        dest = OUT / new_name
        cite = m.get("_cite") or f"{t} {year}/{n}"
        header = (
            f"{m['full_title']}\n"
            f"{cite}\n"
            f"Type: {TYPE_LABEL.get(t, t)}\n"
            f"Jurisdiction: New Zealand\n"
            f"Issued: {m.get('issued') or 'n/a'}\n"
            f"TIB: {m.get('tib') or 'n/a'}\n"
            f"Source: {m['url']}\n"
            f"{'=' * 60}\n"
        )
        dest.write_text(header + body, encoding="utf-8")
        holder.unlink()

        m["title"] = f"{t}_{year}_{n}"
        m["citation"] = cite
        m["year"] = year
        m["source"] = str(dest)
        m["preview"] = (header + body)[:400]
        m.pop("_year", None); m.pop("_cite", None); m.pop("_how", None)
        renamed += 1

    for m in manifest:
        if m["year"] == 0 and m.get("_year"):
            pass
    MANIFEST.write_text(json.dumps(manifest, indent=1))
    print(f"\nrenamed and repaired {renamed} files")

    # ---- verification
    bad_left = [m for m in manifest if m["year"] == 0]
    unparsed = [f.name for f in OUT.glob("*.txt")
                if not re.match(r"^([A-Za-z]+)_(\d{2,4})_(\d+)$", f.stem)]
    print(f"year-0 remaining: {len(bad_left)}")
    print(f"filenames failing data_loader regex: {len(unparsed)}")
    if unparsed[:5]:
        print("  e.g.", unparsed[:5])
    collisions = [k for k, v in Counter(
        f.name for f in OUT.glob("*.txt")).items() if v > 1]
    print(f"filename collisions: {len(collisions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
