#!/usr/bin/env python3.12
"""Build a definitions index for an NZ Act from the official legislation.govt.nz markup.

The corpus markdown is flattened, which is why parsing s YA 1 for defined terms by hand produced
rubbish. The official HTML keeps the structure: each definition is a
``<dfn class="def-term" id="...">term</dfn>`` and every use of a defined term is an
``<a class="term" href="#that-id">``. Section boundaries come from the table of contents, which
maps each provision label to its anchor.

Output: data/<act>/definitions_index.json
    {"definitions": {term: defining section}, "usages": {section: [term, ...]}, "source": ...}

Usage: build_nz_definitions_index.py <act-slug> <whole.html> [--out data/<act>/definitions_index.json]
"""

from __future__ import annotations

import bisect
import json
import pathlib
import re
import sys
from html import unescape

BASE = pathlib.Path(__file__).resolve().parent.parent

_TOC_LABEL = re.compile(
    r'<span class="tocProvLabel">\s*<a class="toc" href="#(DLM\d+)">([^<]{1,40})</a>', re.S)
_SECTION_ANCHOR = re.compile(r'id="(DLM\d+)"')
_DFN = re.compile(r'<dfn class="def-term" id="(DLM\d+)"[^>]*>(.*?)</dfn>', re.S)
_TERM_USE = re.compile(r'<a class="term"[^>]*href="#(DLM\d+)"[^>]*>(.*?)</a>', re.S)
_INTREF = re.compile(r'<a class="intref"[^>]*href="#(DLM\d+)"[^>]*>(.*?)</a>', re.S)
# a provision's own block, used for the text fallback when the Act has no <a class="term"> markup
_SECTION_BODY = re.compile(r'<div class="prov"[^>]*id="(DLM\d+)"[^>]*>(.*?)</div>\s*</div>', re.S)


def clean(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text)).replace("\u00a0", " ").strip()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    act, html_path = sys.argv[1], pathlib.Path(sys.argv[2])
    out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv \
        else BASE / "data" / act / "definitions_index.json"

    html = html_path.read_text(errors="ignore")

    # TOC label -> anchor, then body offsets for those anchors (the TOC also uses id=, so the body
    # occurrence is the one that follows the TOC).
    labels = {anchor: clean(label) for anchor, label in _TOC_LABEL.findall(html)}
    if not labels:
        print("  no TOC provision labels found - this is not an NZ whole-act page")
        return 1

    toc_end = html.rfind('<div id="body"') if '<div id="body"' in html else 0
    offsets: list[tuple[int, str]] = []
    for m in _SECTION_ANCHOR.finditer(html):
        if m.start() < toc_end:
            continue
        label = labels.get(m.group(1))
        if label:
            offsets.append((m.start(), label))
    offsets.sort()
    starts = [o for o, _ in offsets]
    if not starts:
        print("  no section anchors in the body")
        return 1

    def section_at(pos: int) -> str:
        i = bisect.bisect_right(starts, pos) - 1
        return offsets[i][1] if i >= 0 else ""

    # Definitions: term -> the section that defines it. The dfn id is what usages point at, so map
    # id -> (term, defining section) for the second pass.
    defined: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for m in _DFN.finditer(html):
        term = clean(m.group(2))
        if not term or len(term) > 80:
            continue
        where = section_at(m.start())
        if not where:
            continue
        by_id[m.group(1)] = term
        defined.setdefault(term.lower(), where)

    usages: dict[str, set[str]] = {}
    for m in _TERM_USE.finditer(html):
        term = by_id.get(m.group(1)) or clean(m.group(2))
        if not term:
            continue
        where = section_at(m.start())
        if where:
            usages.setdefault(where, set()).add(term.lower())

    # Older Acts (TAA 1994, GST 1985, ...) do not wrap term usages in <a class="term">, so the
    # anchor pass finds none. Fall back to scanning provision text for the terms we know are
    # defined, but only for terms specific enough to be unambiguous: single short words such as
    # "amount" appear in every provision and would link everything to everything.
    derived = 0
    if not usages:
        specific = {t: w for t, w in defined.items()
                    if len(t) >= 8 or " " in t}
        used: dict[str, set[str]] = {}
        for m in _DFN.finditer(html):
            pass
        for m in _SECTION_BODY.finditer(html):
            label = labels.get(m.group(1))
            if not label:
                continue
            body = " ".join(clean(m.group(2)).lower().split())
            for term in specific:
                if f" {term} " in f" {body} ":
                    used.setdefault(label, set()).add(term)
                    derived += 1
        usages = used

    # Explicit internal cross-references ("section HD 18") are legislation-to-legislation links too.
    refs: dict[str, set[str]] = {}
    for m in _INTREF.finditer(html):
        target = clean(m.group(2))
        where = section_at(m.start())
        if where and target and target != where:
            refs.setdefault(where, set()).add(target)

    payload = {
        "act": act,
        "source": str(html_path.name),
        "definitions": defined,
        "usages": {k: sorted(v) for k, v in usages.items()},
        "references": {k: sorted(v) for k, v in refs.items()},
        "counts": {"terms": len(defined), "using_sections": len(usages),
                   "referencing_sections": len(refs), "sections": len(labels)},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    print(f"  {act}: {len(defined)} terms, {len(usages)} sections using terms, "
          f"{len(refs)} sections with internal refs -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
