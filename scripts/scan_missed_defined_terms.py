#!/usr/bin/env python3
"""
Scan dictionary/interpretation sections for defined terms and cross-check
against the definitions library (data/definitions_all.json).

Goal: find terms that ARE defined in the source legislation but MISSING
from the library. Uses multiple extraction patterns and reports candidates
that don't resolve to a library entry, so they can be manually triaged.

Usage: python3 scripts/scan_missed_defined_terms.py
Output: data/definitions_missed_scan.json (grouped by act) + console summary
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
OUT_PATH = DATA_DIR / "definitions_missed_scan.json"

# Dictionary section per act: (section id, relative md path)
DICTIONARY_SECTIONS = {
    "itaa-1936": ("6", "sections/part-i/division-unknown/6.md"),
    "itaa-1936-s317": ("317", "sections/part-x/division-1/317.md"),
    "itaa-1997": ("995-1", "sections/part-6-5/division-995/995-1.md"),
    "gst-1999": ("195-1", "sections/part-6-3/division-195/195-1.md"),
    "fbt-1986": ("136", "sections/part-xid/division-unknown/136.md"),
    "taa-1953": ("2", "sections/part-i/division-unknown/2.md"),
    "sis-1993": ("10", "sections/part-1/division-2/10.md"),
    "aml-ctf-2006": ("5", "sections/part-1/division-1/5.md"),
    "nz-it-2007": ("YA-1", "sections/part-Y/division-YA/YA-1.md"),
    "corporations-act-2001": ("9", "sections/part-1/division-1.2/9.md"),
}

# Patterns from build_definitions_index.py (kept aligned with the library's)
STD_DEF_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9\s'/(),%-]{1,80}?)\s+"
    r"(has (?:the|a) meaning given by|has (?:the|a) meaning affected by|"
    r"has the same meaning as(?: in)?|means|includes)\b",
    re.IGNORECASE,
)
CALLED_RE = re.compile(
    r"(?:which|that)\s"
    r"(?:\([^)]*\)\s)*"
    r"(?:is|are|is to be)\s"
    r"(?:in\s+this\s+(?:section|Division|Part|Subdivision|Act)\s)?"
    r"called\s+"
    r"(?:the\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9\s'(),/-]{1,60}?)"
    r"(?:[.])",
    re.IGNORECASE,
)
DICT_VERB_RE = re.compile(
    r"(?:^|\.\s+|;\s+|:\s+|\n|\)\s+|\]\s+)\s*"
    r"([A-Za-z0-9][^.;:\n\u2014()\[\]]{0,80}?(?:\([^)]{1,60}\))?[^.;:\n\u2014()\[\]]{0,40}?)"
    r"(,\s(in relation to|in respect of|when used|in connection with|for|of|to)\s[^.;\n]{0,200}?,)?"
    r"\s*(?:\u2014\s*)?(?:\([a-z]|[ivx]+|\d+\)\s*)?"
    r"(?:has\s+(?:the|a)\s+(?:same\s+)?meanings?\b|is defined in\b|means\b|includes\b)",
    re.MULTILINE,
)
COLON_DEF_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9\s'{(\[(\/,)-]{1,60}?):\s",
    re.MULTILINE,
)

# Known-non-term captions (list lettering, notes, headers, etc.)
STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "subsection", "section",
    "subsections", "sections", "paragraph", "paragraphs", "subparagraph",
    "subparagraphs", "item", "items", "subitem", "note", "note:", "for", "of",
    "to", "in", "on", "by", "with", "under", "unless", "however", "if", "and",
    "or", "but", "subject", "except", "means", "includes", "include", "meaning",
    "expressions", "expression", "word", "words", "term", "terms", "also",
    "has", "have", "having", "as", "at", "from", "into", "over", "up", "down",
    "out", "so", "then", "there", "their", "its", "it", "he", "she", "they",
    "will", "may", "must", "shall", "where", "when", "any", "all", "each",
    "both", "either", "neither", "part", "division", "subdivision", "act",
    "period", "time", "day", "days", "year", "years", "amount", "person",
    "taxpayer", "company", "income", "expenditure", "expenses", "deduction",
    "allowance", "credit", "liability", "asset", "property", "business",
    "carried", "carrying", "referred", "reference", "purpose", "purposes",
    "general", "particular", "application", "operations", "operation",
}


def clean_term(t: str) -> str:
    t = t.strip().strip("()[],.\\/*:;\u2014").strip()
    # drop trailing qualifiers that are part of the sentence, not the term
    for pat in (r",\s+(?:in|of|for|to|when|that|which|where|if|unless|subject).*$",
                r"\s+(?:means|includes|has|is|are).*$",
                r"\((?:\d+|[a-z])\)\s*$"):
        t = re.sub(pat, "", t, flags=re.IGNORECASE).strip()
    return t


def is_real_term(t: str) -> bool:
    if not t or len(t) < 2 or len(t) > 90:
        return False
    low = t.lower().strip()
    if low in STOPWORDS:
        return False
    # must start with a letter or digit
    if not re.match(r"^[A-Za-z0-9]", t):
        return False
    # list lettering / numbering like "(a)", "(i)", "12)", "5."
    if re.match(r"^\([a-z]\)$", t) or re.match(r"^\([ivx]+\)$", t, re.I):
        return False
    if re.match(r"^\d+[.)]?$", t):
        return False
    # pure heading fragments
    if re.match(r"^(note|example|guide|definition|interpretation)s?$", low):
        return False
    # Fragment rejection: terms must not contain mid-sentence continuations
    # (the regex captures too far when the "means" is far away). A real
    # defined term is a noun phrase - it does not contain these:
    frag = [
        r"\b(?:and|or|but)\s+(?:without|the|a|any|if|subject|when|where|for)\b",
        r"\b(?:and|but)\s+without\b",
        r"\b(?:who|which|that|whose|whom)\b",
        r"\b(?:during|before|after|between|until|within)\b",
        r"\b(?:including|excludes?|excluding|comprising|containing)\b",
        r"\b(?:carried|carrying|pays?|paid|received|receiving|incurred|incurring)\s",
        r"\b(?:does|doesn't|is|are|was|were|be|been|being)\b",
        r"\b(?:with|without|under|upon|against|towards?)\b",
        r"\b(?:in\s+relation\s+to|in\s+respect\s+of|for\s+the\s+purpose)\b",
        r"\b(?:section|subsection|paragraph|subparagraph|division|part|schedule)\s+\d",
        r"\b(?:this|that|these|those)\b",
        r"^(?:the|a|an|any|each|every|such)\s+",
        r"\b(?:total|number|amount|value|period|year|time|day)\s+of\b",
    ]
    for pat in frag:
        if re.search(pat, low):
            return False
    # 1936 s 6 run-on style: "term means" entries are short; long captures
    # are almost always sentence fragments. Real multiword terms stay short.
    if len(t.split()) > 6:
        return False
    # "in this definition called X" - the term X follows; the capture before
    # "means" is the description, not the term. Skip if it ends mid-phrase.
    if re.search(r"\b(?:referred to as|known as|called)\b", low):
        return False
    return True


def normalize_key(t: str) -> str:
    """Library keys are lowercase-snake; match on a loose normalized form."""
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def extract_terms(text: str) -> list[str]:
    terms = []
    for pat in (STD_DEF_RE, CALLED_RE, DICT_VERB_RE, COLON_DEF_RE):
        for m in pat.finditer(text):
            terms.append(clean_term(m.group(1)))
    return terms


def load_library():
    with open(DATA_DIR / "definitions_all.json") as f:
        lib = json.load(f)
    keys_by_act = {}
    for act, data in lib.items():
        keys_by_act[act] = {
            normalize_key(t): t for t in data.get("terms", {}).keys()
        }
    return keys_by_act


def recover_left(text: str, term: str) -> str:
    """Try to extend a truncated term leftwards in the source text.

    When the regex starts mid-word (e.g. 'ssive commodity gain' from
    'passive commodity gain'), find the term in the text and walk back
    over the incomplete leading word to recover the full term.
    """
    idx = text.find(term)
    if idx <= 0:
        return term
    # walk left over the partial word (letters, apostrophes, hyphens, digits)
    j = idx
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] in "'-"):
        j -= 1
    if j == idx:
        return term  # no partial word before - clean capture
    prefix = text[j:idx]
    if len(prefix) > 12:
        return term  # too long to be a word start
    extended = prefix + term
    # sanity: the extended form shouldn't itself contain a fragment keyword
    if re.search(r"\b(?:and|or|but)\b", extended.lower()):
        return term
    if len(prefix) <= 4:
        return extended
    # Longer prefixes: only accept if the recovered leading word exists
    # elsewhere in the text as a standalone token (proves it's a real word
    # being truncated, not a false start).
    first_m = re.match(r"[A-Za-z0-9'\-]+", extended)
    if not first_m:
        return term
    first_word = first_m.group(0)
    if re.search(r"(?<![A-Za-z0-9'-])" + re.escape(first_word) + r"(?![A-Za-z0-9'-])", text):
        return extended
    return term


def main():
    lib = load_library()
    report = {}
    total_missed = 0
    for label, (sec, rel_path) in DICTIONARY_SECTIONS.items():
        act = label.split("-s")[0] if "-s" in label else label
        md = DATA_DIR / act / rel_path
        if not md.exists():
            print(f"[skip] {label} - {rel_path} missing")
            continue
        text = md.read_text()
        terms = [t for t in extract_terms(text) if is_real_term(t)]
        # dedupe preserving order
        seen = set()
        uniq = []
        for t in terms:
            k = normalize_key(t)
            if k and k not in seen:
                seen.add(k)
                uniq.append((t, k))
        act_keys = lib.get(act, {})
        missed = []
        for raw, k in uniq:
            if k not in act_keys:
                missed.append((raw, k))
        # cross-act fallback: term defined in another act's dictionary
        cross_found = []
        for raw, k in missed:
            if any(k in other for other in lib.values()):
                continue
            cross_found.append((raw, k))
        # Recover truncated terms
        recovered = []
        final_missed = []
        for raw, k in cross_found:
            ext = recover_left(text, raw)
            if ext and ext != raw:
                ek = normalize_key(ext)
                if ek in act_keys:
                    continue  # already in library under the full form
                if any(ek in other for other in lib.values()):
                    continue
                recovered.append((ext, ek, raw))
            else:
                final_missed.append((raw, k))
        # Confidence filter: a REAL defined term in a dictionary section sits
        # directly before a definition verb. Verify each missed candidate
        # against the source text.
        def verify(t: str) -> bool:
            return bool(
                re.search(
                    re.escape(t) + r"(?:\s*[,:;)\]]?\s*(?:\u2014\s*)?(?:has\s+(?:the|a)\s+(?:same\s+)?meaning|means|includes|is defined in)\b)",
                    text, flags=re.IGNORECASE,
                )
            )
        high_conf = [(r, k) for r, k in final_missed if verify(r)]
        low_conf = [(r, k) for r, k in final_missed if not verify(r)]
        report[label] = {
            "section": sec,
            "candidates_extracted": len(uniq),
            "high_confidence_misses": [r for r, _ in high_conf],
            "possible_misses": [r for r, _ in low_conf],
            "recovered_fragments": [
                {"full": r, "as_captured": f} for r, _, f in recovered
            ],
            "missed_count": len(high_conf) + len(recovered),
            "possible_count": len(low_conf),
        }
        total_missed += len(high_conf) + len(recovered)
        status = "OK" if not (high_conf or recovered) else f"{len(high_conf) + len(recovered)} MISS"
        print(f"{label:32s} {len(uniq):5d} candidates  ->  {status}" + (f" (+{len(low_conf)} possible)" if low_conf else ""))
        for r, _ in high_conf[:15]:
            print(f"    MISS: {r[:90]}")
        for ext, _, frag in recovered[:8]:
            print(f"    MISS: {ext[:90]}   (captured as '{frag[:40]}')")

    with open(OUT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nTotal high-confidence missed: {total_missed}  (report: {OUT_PATH})")


if __name__ == "__main__":
    main()
