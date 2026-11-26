#!/usr/bin/env python3
"""Split ATO Interpretative Decisions (ATOID / AID) into one whole-document chunk each.

ATOIDs are short (~17 lines) and have no numeric paragraph markers, so the
paragraph-marker approach in split_ruling_paragraphs.py produces nothing
usable for them (944 paragraphs across 5,931 docs, 5,462 empty files). This
script instead ingests one chunk per document: the whole cleaned body.

Cleaning removes only the fixed boilerplate blocks (verified to be exact,
repeated strings across nearly the entire corpus):
  - "ATO Interpretative Decision" header line
  - "ATO ID YYYY/N" line
  - the "====...====" title rule (the title text itself is kept)
  - the "This ATOID provides you with the following level of protection: ..."
    paragraph
  - the "CAUTION: This is an edited and summarised record ..." paragraph
Everything else (FOI status line, withdrawal/current statement, Issue,
Decision, Facts, Reasons For Decision, legislative references, etc.) is kept
verbatim, since it is not listed as boilerplate.

The ATO cites these documents as "ATO ID YYYY/N"; the corpus/file ref is
"AID YYYY/N". Line 2 (the "ATO ID ..." line) is dropped as boilerplate, so
without extra care neither citation form would appear as running text in the
chunk. Metadata fields are not reliably indexed by the search layer, so the
chunk's first line is set to carry both forms explicitly
("ATO ID YYYY/N (AID YYYY/N)") -- this is the only way to guarantee the
citation is findable by full-text/vector search over the chunk text itself.

Output: data/rulings/paragraphs/AID_<year>_<num>.json
  {"ruling": "AID 2001/1", "title": "...", "citation_alt": "ATO ID 2001/1",
   "status": "withdrawn"|"current",
   "paragraphs": [{"num": "1", "label": "Document", "text": "..."}]}

Usage:
    python3 scripts/split_ato_documents.py [--all] [--file data/rulings/AID_2001_1.txt]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEXTS_DIR = REPO / "data" / "rulings"
OUT_DIR = REPO / "data" / "rulings" / "paragraphs"

FILENAME_RE = re.compile(r"^AID_(\d{4})_(\d+)\.txt$")

HEADER_RE = re.compile(
    r"^\s*ATO Interpretative Decision\s*"
    r"ATO\s*ID\s*\d{4}/\d+\s*(\(Withdrawn\)\s*)?",
    re.IGNORECASE,
)
RULE_RE = re.compile(r"^=+\s*")

PROTECTION_RE = re.compile(
    r"This ATOID provides you with the following level of protection:\s*"
    r"If you reasonably apply this decision in good faith to your own circumstances\s*"
    r"\(which are not materially different from those described in the decision\),\s*"
    r"and the decision is later found to be incorrect you will not be liable to pay any\s*"
    r"penalty or interest\.\s*However, you will be required to pay any underpaid tax\s*"
    r"\(or repay any over-claimed credit, grant or benefit\), provided the time limits\s*"
    r"under the law allow it\.\s*If you do intend to apply this decision to your own\s*"
    r"circumstances, you will need to ensure that the relevant provisions referred to in\s*"
    r"the decision have not been amended or repealed\.\s*You may wish to obtain further\s*"
    r"advice from the Tax Office or from a professional adviser\.\s*",
    re.IGNORECASE,
)

CAUTION_RE = re.compile(
    r"CAUTION:\s*This is an edited and summarised record of a Tax Office decision\.\s*"
    r"This record is not published as a form of advice\.\s*"
    r"It is being made available for your inspection to meet FOI requirements,\s*"
    r"because it may be used by an officer in making another decision\.\s*",
    re.IGNORECASE,
)

# Earliest-match wins: boundary between the descriptive title and the
# FOI-status / withdrawal-status text that follows it.
TITLE_CUT_RE = re.compile(
    r"FOI status:"
    r"|This document incorporates revisions made since original publication"
    r"|This ATOID provides you with the following level of protection"
    r"|ATO\s*ID (?:is|has been|was) withdrawn"
    r"|ATO Interpretative Decision is withdrawn"
    r"|This decision has been withdrawn"
    r"|Decision Withdrawn"
    r"|Withdrawn pending"
    r"|\bWithdrawn\."
    r"|CAUTION:",
    re.IGNORECASE,
)

# Status region ends at the "Issue" heading (or a fixed fallback window for
# the rare stub documents that have no Issue/Decision/Facts body at all), so
# a withdrawn-related-ATOID citation in the Reasons/footer metadata (e.g.
# "Related ATO Interpretative Decisions ATO ID 2004/750 (Withdrawn)") isn't
# mistaken for this document's own withdrawal status. "Decision"/"Facts" are
# NOT used as end markers: both words occur too often inside the opening
# status sentence itself (e.g. "This Interpretative Decision is ... withdrawn").
STATUS_END_RE = re.compile(r"\bIssue\b")
STATUS_FALLBACK_CHARS = 600
WITHDRAWN_RE = re.compile(r"withdrawn", re.IGNORECASE)

MAX_PARA_CHARS = 12000  # must match scripts/embed_rulings.py


def clean_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    out_lines: list[str] = []
    blank = False
    for ln in lines:
        if ln == "":
            if not blank and out_lines:
                out_lines.append("")
            blank = True
        else:
            out_lines.append(ln)
            blank = False
    return "\n".join(out_lines).strip()


def process_file(path: Path) -> dict:
    m = FILENAME_RE.match(path.name)
    if not m:
        raise ValueError(f"unexpected filename: {path.name}")
    year, num = m.group(1), m.group(2)
    ruling = f"AID {year}/{num}"
    citation_alt = f"ATO ID {year}/{num}"

    raw = path.read_text(encoding="utf-8", errors="replace").replace("\xa0", " ")

    # Anchored to this document's OWN "ATO ID ..." header line only -- a bare
    # search anywhere in the text would also match "(Withdrawn)" markers on
    # unrelated rulings/ATOIDs cited later (e.g. in a Related ATOIDs list).
    had_withdrawn_marker = bool(HEADER_RE.match(raw) and "(Withdrawn)" in HEADER_RE.match(raw).group(0))

    body = HEADER_RE.sub("", raw, count=1)
    body = RULE_RE.sub("", body, count=1)

    cut_m = TITLE_CUT_RE.search(body)
    title = clean_whitespace(body[: cut_m.start()] if cut_m else body)

    end_m = STATUS_END_RE.search(body)
    status_scope = body[: end_m.start()] if end_m else body[:STATUS_FALLBACK_CHARS]
    status = "withdrawn" if (had_withdrawn_marker or WITHDRAWN_RE.search(status_scope)) else "current"

    body = PROTECTION_RE.sub(" ", body)
    body = CAUTION_RE.sub(" ", body)
    body = clean_whitespace(body)

    text = f"ATO ID {year}/{num} (AID {year}/{num})\n\n{body}".strip()

    return {
        "ruling": ruling,
        "title": title,
        "citation_alt": citation_alt,
        "status": status,
        "paragraphs": [{"num": "1", "label": "Document", "text": text}],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="process all AID_*.txt files")
    ap.add_argument("--file", type=Path, help="process a single file")
    args = ap.parse_args()

    if args.file:
        files = [args.file]
    else:
        files = sorted(TEXTS_DIR.glob("AID_*.txt"))

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    for f in files:
        doc = process_file(f)
        m = FILENAME_RE.match(f.name)
        out_path = OUT_DIR / f"AID_{m.group(1)}_{m.group(2)}.json"
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    print(f"Processed {len(files)} files, wrote {written} JSON docs to {OUT_DIR}")


if __name__ == "__main__":
    main()
