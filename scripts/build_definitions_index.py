#!/usr/bin/env python3
"""
Comprehensive definitions scan — searches ALL section files in ALL acts for
any definition-like text pattern, not relying on the existing index.

Uses the production backend's text-matching logic to find:
  - "term means/includes/has the meaning given by"
  - "which ... is in this section called the term"
  - "In this section/Division/Part: term means ..."
  - Colon-style definitions: "term: definition text"

Output: data/definitions_comprehensive.json
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
OUT_PATH = DATA_DIR / "definitions_comprehensive.json"
REPORT_PATH = DATA_DIR / "definitions_scan_report.json"

ACTS = {
    "itaa-1997": "ITAA 1997",
    "itaa-1936": "ITAA 1936",
    "gst-1999": "GST Act 1999",
    "taa-1953": "TAA 1953",
    "fbt-1986": "FBTAA 1986",
    "sis-1993": "SIS Act 1993",
    "corporations-act-2001": "Corporations Act 2001",
    "aml-ctf-2006": "AML/CTF Act 2006",
    "nz-it-2007": "NZ IT Act 2007",
}

# ── Shared helpers for term boundaries (CDN-0209 / CDN-0210) ───────────
# Corpus dictionaries run terms together in long paragraphs, e.g.
#   "R&D activities has the meaning given by section 355-20. R&D entity ..."
# so a term is only a term when it begins at a real sentence/paragraph
# boundary.  Character classes must also allow '&' and normalise curly
# apostrophes (U+2019) to straight ones, otherwise acronym / possessive
# terms are dropped or captured mid-word.

# Words that cannot end a defined term (dangling run-on tails).
# CDN-0210 defect (a): "a" and "an" are NOT connector tails — they are real
# term words ("Zone A", itaa-1936 s 79A; "Zone B").  Including them dropped
# every defined term ending in a bare article.
_CONNECTOR_TAILS = frozenset({
    "the", "and", "or", "of", "to", "in", "for", "with", "by",
    "on", "at", "as", "that", "which", "is", "are", "was", "were", "be",
    "been", "being", "from", "if", "when", "where", "while", "but", "not",
    "no", "nor", "under", "over", "per", "has", "have", "had", "means",
    "includes", "its", "their", "this", "these", "those", "any", "all",
    "each", "every", "such", "other", "another", "more", "than", "into",
    "upon", "within", "without", "about", "through", "during", "before",
    "after", "so", "then", "also", "only", "whose", "who", "whom", "his",
    "her", "our", "your", "them", "they", "there", "here",
})

# A capture that begins at a bullet / numbered list marker ("a) ...", "ii) ...").
_FRAGMENT_LEAD_RE = re.compile(r"^\(?[0-9a-z]{1,4}\)[ \t]")
# A capture that starts mid-word (a lone lowercase letter then a space: the
# tail of an acronym such as "R&D activities" or "arm's length ...").
_MIDWORD_START_RE = re.compile(r"^[a-z][ \t]")
# A run-on continuation clause ("and (b) ...").
_RUNON_LEAD_RE = re.compile(r"^(?:and|or)[ \t]+\(?[0-9a-z]{1,4}\)[ \t]", re.IGNORECASE)
# An inner sentence break captured inside a term.
_INNER_BREAK_RE = re.compile(r"\.\s+[A-Z]")
# A single ASCII letter or digit (used to detect mid-word capture starts).
_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
# A paragraph / sub-clause list marker immediately before a capture, e.g.
# "> **(a)** in a scheme ... has the meaning given by" or "b) for a trust,".
# Such a capture is a run-on clause, never a defined term.
_LIST_MARKER_BEFORE_RE = re.compile(
    r"(?:^|[\s.;:,>(])(?:\([a-z]\)|\([ivx]{2,4}\)|[a-z]\))[ \t]*$",
    re.IGNORECASE,
)
# A trailing scope qualifier: "adjusted Division 6 percentage, in relation to
# a trust estate," -> the actual defined term is "adjusted Division 6 percentage".
_QUALIFIER_RE = re.compile(
    r",[ \t]+(?:in relation to|in respect of|in connection with|in the case of|"
    r"when used|by|for|of|to|at|under|on|with|within|in)\b.*$",
    re.DOTALL,
)


def normalise_quotes(text: str) -> str:
    """Map curly quotes/apostrophes to straight ones (as clean_dict_body does)."""
    return (text.replace("\u2018", "'").replace("\u2019", "'")
                .replace("\u201c", '"').replace("\u201d", '"'))


def clean_scan_body(body: str) -> str:
    """Normalise quotes + strip markdown emphasis before scanning a section."""
    return normalise_quotes(body).replace("*", "")


def repair_term(term: str) -> str | None:
    """Reduce a raw regex capture to the actual defined-term string.

    Drops blank captures and a trailing ", <scope qualifier>" clause; returns
    None when nothing usable is left.
    """
    t = re.sub(r"\s+", " ", term).strip()
    t = t.strip(";:").strip().strip(",").strip()
    if _QUALIFIER_RE.search(t):
        core = _QUALIFIER_RE.sub("", t).strip().rstrip(",").strip()
        if core:
            t = core
    return t or None


def starts_after_list_marker(body: str, pos: int) -> bool:
    """True when a capture at ``pos`` sits just after a list marker ("(a) ", "b) ")."""
    return bool(_LIST_MARKER_BEFORE_RE.search(body[max(0, pos - 48):pos]))


def starts_at_left_boundary(body: str, pos: int) -> bool:
    """True when a capture at ``pos`` begins at a real word boundary (CDN-0209).

    A capture that begins immediately after an ASCII letter or digit is the
    tail of a longer word, never a defined term: with "&" outside the term
    class, "R&D entity" was captured as "d entity" and "arm's length profits"
    as "s length profits".  Non-ASCII letters (the "ā" in "Māori") are left
    alone — the capture regex cannot span them, so a term legitimately starts
    there.
    """
    return pos == 0 or not _ASCII_ALNUM_RE.match(body[pos - 1])


def had_scope_qualifier(raw: str) -> bool:
    """True when the raw capture carried a trailing ", <scope qualifier>" clause.

    CDN-0210 defect (b): repair_term() strips such a clause, so several
    definitions in different sections reduce to the same core term (e.g.
    "exempt income, in relation to a partnership", itaa-1936 s 90).  Records
    for those must be deduplicated on term+act+section, not term+act.
    """
    return bool(_QUALIFIER_RE.search(re.sub(r"\s+", " ", raw).strip()))


# ── Pattern 1: Standard "term means/includes/has meaning" ──────────────
# CDN-0209: the character class allows '&' and the curly apostrophe U+2019 so
# acronym/possessive terms ("R&D entity", "arm's length conditions") are
# captured whole; curly quotes are normalised to straight ones in
# clean_scan_body/repair_term.
# The raw capture is still deliberately permissive — mis-segmented captures
# (run-on clauses, scope qualifiers) are removed by repair_term() and
# is_valid_def_term() rather than by narrowing the match itself.
STD_DEF_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9\s&'\u2019/(),%\-]{1,80}?)\s+"
    r"(has (?:the|a) meaning given by|has (?:the|a) meaning affected by|"
    r"has the same meaning as(?: in)?|means|includes)\b",
    re.IGNORECASE,
)

# A capture that begins with a section number ("57a meaning of corporation",
# "90 in relation to a partnership ...") is a heading/cross-reference, never a
# defined term.  CDN-0210 defect (c): 87 corps heading captures leaked in.
_SECTION_NUMBER_LEAD_RE = re.compile(r"^\d+[a-z]?\s")

# ── Pattern 2: "which ... is in this section/Division/Part called the <term>" ──
CALLED_RE = re.compile(
    r"(?:which|that)\s"
    r"(?:\([^)]*\)\s)*"  # optional parenthetical like "(in this section)"
    r"(?:is|are|is to be)\s"
    r"(?:in\s+this\s+(?:section|Division|Part|Subdivision|Act)\s)?"
    r"called\s+"
    r"(?:the\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9\s&'\u2019(),/-]{1,60}?)"
    r"(?:[.])",
    re.IGNORECASE,
)

# ── Pattern 3: "In this section/Division/Part: term means ..." ─────────
IN_THIS_RE = re.compile(
    r"(?:In\s+this\s+(?:section|Division|Part|Subdivision|Act)\s*[:.]\s*)?"
    r"([A-Za-z0-9][A-Za-z0-9\s&'\u2019(/,-]{1,80}?)\s+"
    r"(means|includes|has (?:the|a) meaning)",
    re.IGNORECASE,
)


# ── Pattern 4: "term: definition text" ────────────────────────────────
COLON_DEF_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9\s&'\u2019{(\[(\/,)-]{1,60}?):\s",
    re.MULTILINE,
)

SECTION_HEADING_RE = re.compile(r"^#{1,4}\s", re.MULTILINE)

# ── Dictionary-section scanner (curated phase for acts with no pointers) ──
# These acts have a single Interpretation/Definitions section whose terms are
# listed alphabetically in run-on paragraphs (the itaa-1936 s 6 pattern).
DICTIONARY_SECTIONS = {
    "fbt-1986": ("136", "sections/part-xid/division-unknown/136.md"),
    "taa-1953": ("2", "sections/part-i/division-unknown/2.md"),
    "sis-1993": ("10", "sections/part-1/division-2/10.md"),
    "aml-ctf-2006": ("5", "sections/part-1/division-1/5.md"),
    "nz-it-2007": ("YA-1", "sections/part-Y/division-YA/YA-1.md"),
}

DEFS_ALL_PATH = DATA_DIR / "definitions_all.json"

# "term means / includes / has the meaning ...", with an optional
# ", in relation to X," qualifier and the NZ "term— (a) means" dash style.
DICT_VERB_RE = re.compile(
    r"(?:^|\.\s+|;\s+|:\s+|\n|\)\s+|\]\s+)\s*"
    r"([A-Za-z0-9][^.;:\n—()\[\]]{0,80}?(?:\([^)]{1,60}\))?[^.;:\n—()\[\]]{0,40}?)"
    r"(,\s(?:in relation to|in respect of|when used|in connection with|for|of|to)\s[^.;\n]{0,200}?,)?"
    r"\s*(?:—\s*)?(?:\((?:[a-z]|[ivx]+|\d+)\)\s*)?"
    r"(?:has\s+(?:the|a)\s+(?:same\s+)?meanings?\b|is defined in\b|means\b|includes\b)",
    re.MULTILINE,
)

# Colon-style: "term: definition text" (AML/CTF s 5 uses this heavily)
DICT_COLON_RE = re.compile(
    r"(?:^|\.\s+|\n)\s*([A-Za-z0-9][^.;:\n—]{1,60}?):\s",
    re.MULTILINE,
)


def _loose_alpha_key(term: str) -> str:
    return re.sub(r"[^a-z0-9]", "", term.lower())


def clean_dict_body(content: str) -> str:
    body = get_body(content)
    body = re.sub(r'<a id="[^"]*"></a>\s*', "", body)
    body = re.sub(r"^#+\s.*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"^>\s?", "", body, flags=re.MULTILINE)
    body = body.replace("‘", "'").replace("’", "'")
    body = body.replace("“", '"').replace("”", '"')
    return body.replace("*", "")


def scan_dictionary_section(act: str) -> list[str]:
    """Extract term names from an act's dictionary section, in file order."""
    section, rel_path = DICTIONARY_SECTIONS[act]
    md_path = DATA_DIR / act / rel_path
    body = clean_dict_body(md_path.read_text(encoding="utf-8", errors="replace"))

    candidates = []  # (position, term)
    verb_spans = []
    for m in DICT_VERB_RE.finditer(body):
        term = m.group(1).strip().rstrip(",")
        candidates.append((m.start(1), term))
        verb_spans.append((m.start(1), m.end()))
    for m in DICT_COLON_RE.finditer(body):
        # Skip colon matches inside a verb-style match ("term means:")
        if any(s <= m.start(1) < e for s, e in verb_spans):
            continue
        candidates.append((m.start(1), m.group(1).strip()))
    candidates.sort()

    # Terms appear alphabetically in dictionary sections, so the real terms
    # form the longest non-decreasing chain of sort keys through the file;
    # mid-definition false positives ("the recipient means...") fall off it.
    valid = [t for _, t in candidates if is_valid_def_term(t) and _loose_alpha_key(t)]
    keys = [_loose_alpha_key(t) for t in valid]
    n = len(keys)
    # ponytail: O(n^2) LIS, n is at most a few thousand in a one-off script
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if keys[j] <= keys[i] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
                prev[i] = j
    if not n:
        return []
    i = max(range(n), key=lambda k: best[k])
    chain = []
    while i != -1:
        chain.append(valid[i])
        i = prev[i]
    chain.reverse()

    kept, seen = [], set()
    for term in chain:
        key = _loose_alpha_key(term)
        if key not in seen:
            seen.add(key)
            kept.append(term)
    return kept


def run_dictionary_phase() -> int:
    import datetime
    import shutil

    data = json.loads(DEFS_ALL_PATH.read_text(encoding="utf-8"))
    backup = DEFS_ALL_PATH.with_name(
        f"definitions_all.json.bak-{datetime.date.today().isoformat()}")
    if not backup.exists():
        shutil.copy2(DEFS_ALL_PATH, backup)
        print(f"Backup: {backup}")

    for act, (section, rel_path) in DICTIONARY_SECTIONS.items():
        raw = (DATA_DIR / act / rel_path).read_text(encoding="utf-8", errors="replace")
        anchor_m = re.search(r'<a id="(s[^"]+)"', raw)
        anchor = anchor_m.group(1) if anchor_m else ""

        terms = scan_dictionary_section(act)
        existing = data.get(act, {}).get("terms", {})
        merged = {**existing}
        for term in terms:
            if term not in merged:
                merged[term] = {"anchor": anchor, "section": section}
        data[act] = {"section": section, "terms": merged}
        print(f"{act}: {len(existing)} -> {len(merged)} terms (s {section})")

    DEFS_ALL_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {DEFS_ALL_PATH}")
    return 0


def get_body(content: str) -> str:
    if content.startswith("---"):
        m = re.search(r"\n---\s*\n", content)
        return content[m.end():] if m else content
    return content


def normalize(term: str) -> str:
    return normalise_quotes(re.sub(r"\s+", " ", term.strip().lower()))


def is_valid_def_term(term: str) -> bool:
    t = normalize(term)
    if not t or len(t) < 2 or len(t) > 80:
        return False
    if t.startswith("(") or t.endswith("("):
        return False
    if re.match(r"^[\d\s]+$", t):  # numbers only
        return False
    # Subsection markers like "a)", "(b)", "ii)", "(ii)"
    if re.match(r"^\(?[a-z]\)?$", t) or re.match(r"^\(?[ivx]+\)?$", t):
        return False
    if re.match(r"^\d+\)", t):  # "2)", "3)"
        return False
    # ── CDN-0210 term-boundary quality gates ───────────────────────────
    # A real defined term never begins at a list marker, mid-word, or carries
    # an inner sentence break / dangling connective tail.
    if _FRAGMENT_LEAD_RE.match(t) or _RUNON_LEAD_RE.match(t):
        return False
    # CDN-0210 defect (c): a capture that begins with a section number
    # ("57a meaning of corporation (1)") is a heading/cross-reference.
    if _SECTION_NUMBER_LEAD_RE.match(t):
        return False
    if _MIDWORD_START_RE.match(t):
        return False
    if _INNER_BREAK_RE.search(t):
        return False
    if t.split() and t.split()[-1] in _CONNECTOR_TAILS:
        return False
    if t.endswith((",", ":", ";", "-", "&")):
        return False
    # False starts
    first_word = t.split()[0] if t.split() else ""
    if first_word in ("the", "a", "an", "this", "that", "these", "those", "if", "for", "to", "in", "of", "on", "or", "and", "but", "not", "by", "with", "from", "at", "as", "any", "all", "each", "every", "its", "his", "her", "their", "whether", "where", "when", "while", "after", "before", "during", "under", "over", "without", "no", "nor"):
        return False
    # Remove leading "In this section/Division" prefix that got captured
    if re.match(r"^(?:in this|for the purposes of this)", t):
        return False
    # Must contain at least one alphabetic character
    if not re.search(r"[a-z]", t):
        return False
    return True


def find_definition_end(body: str, start: int, term: str) -> int:
    """Find where this definition ends using multiple strategies."""
    after = body[start:]
    ends = []

    # Next definition anchor
    m = re.search(r'<a id="s\d', after)
    if m:
        ends.append(start + m.start())

    # Next section heading
    m = SECTION_HEADING_RE.search(after)
    if m:
        ends.append(start + m.start())

    # Next definition pattern. CDN-0213: searching from position 1 re-matched
    # the CURRENT term mid-word ("R&D entity" -> "d entity ... means"), and the
    # old `> 10` guard then discarded that only real boundary, so a run-on
    # dictionary paragraph bled to the end of the section (mean 8,936 / max
    # 431,276 chars). Locate the current definition's own anchor (term + verb)
    # first, then search for the NEXT one after it.
    own_end = None
    m_own = STD_DEF_RE.match(after)
    if m_own:
        own_end = m_own.end()
    else:
        m_own_called = CALLED_RE.match(after)
        if m_own_called:
            own_end = m_own_called.end()
    search_from = own_end if own_end else 1
    m_std = STD_DEF_RE.search(after, search_from)
    if m_std:
        ends.append(start + m_std.start())

    m_called = CALLED_RE.search(after, search_from)
    if m_called:
        ends.append(start + m_called.start())

    # Try to use the next sentence break after a reasonable length
    # Find the next ". \n" pattern that follows a definition-length segment
    long_enough = max(50, len(term) * 3)
    if len(after) > long_enough:
        # Find next double-newline or paragraph break
        m = re.search(r"\n\n(?=[A-Z\"(])", after[long_enough:])
        if m:
            ends.append(start + long_enough + m.start())

    ends = [e for e in ends if e > start]
    return min(ends) if ends else len(body)


def dedupe_sig(record: dict) -> str:
    """Deduplication key for one definition record (CDN-0210 defect b).

    A record whose raw capture carried a ", <scope qualifier>" clause keeps its
    section in the key: several sections define the same core term in different
    scopes ("exempt income, in relation to a partnership", itaa-1936 s 90), and
    keying them on term+act alone folds the scoped definitions into whichever
    section happens to be scanned first.  Unscoped records keep term+act, so a
    term defined once is still emitted once.
    """
    if record.get("scoped"):
        return f"{record['term']}|{record['act']}|{record['section']}"
    return f"{record['term']}|{record['act']}"


def dedupe_records(records: list[dict]) -> list[dict]:
    """First occurrence wins, on the key from dedupe_sig()."""
    seen = set()
    unique = []
    for d in records:
        sig = dedupe_sig(d)
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(d)
    return unique


def scan_section_body(body: str, act: str, act_display: str,
                      section_id: str) -> list[dict]:
    """Extract definition records from one section body (raw markdown text)."""
    clean_body = clean_scan_body(body)
    results = []

    # 1. Standard definitions: "term means/includes"
    for m in STD_DEF_RE.finditer(clean_body):
        pos = m.start(1)
        # CDN-0209: the capture must begin at a real word boundary, never
        # immediately after an ASCII letter/digit (the tail of a longer word,
        # e.g. "d entity" from "R&D entity", "s length" from "arm's length").
        if not starts_at_left_boundary(clean_body, pos):
            continue
        if starts_after_list_marker(clean_body, pos):
            continue
        raw = m.group(1)
        repaired = repair_term(raw)
        if repaired is None:
            continue
        term = normalize(repaired)
        if not is_valid_def_term(term):
            continue

        # Check it doesn't start with a false start word
        first_word = term.split()[0] if term.split() else ""
        if first_word in ("the", "a", "an", "this", "that", "these", "those", "if", "for", "to", "in", "of", "on"):
            continue

        start = pos
        end = find_definition_end(clean_body, start, term)
        def_text = clean_body[start:end].strip()
        def_text = re.sub(r"\n{3,}", "\n\n", def_text)

        if len(def_text) >= 15:
            results.append({
                "term": term,
                "act": act, "act_display": act_display,
                "section": section_id,
                "definition": def_text,
                "source": "std",
                "scoped": had_scope_qualifier(raw),
            })

    # 2. "which ... is called the <term>" pattern
    for m in CALLED_RE.finditer(clean_body):
        raw = m.group(1)
        repaired = repair_term(raw)
        if repaired is None:
            continue
        term = normalize(repaired)
        if not is_valid_def_term(term):
            continue

        start = m.start()
        end = find_definition_end(clean_body, start, term)
        def_text = clean_body[start:end].strip()
        def_text = re.sub(r"\n{3,}", "\n\n", def_text)

        if len(def_text) >= 15:
            results.append({
                "term": term,
                "act": act, "act_display": act_display,
                "section": section_id,
                "definition": def_text,
                "source": "called",
                "scoped": had_scope_qualifier(raw),
            })

    # Per-section dedup (same key rules as the whole-act pass).
    return dedupe_records(results)


def scan_act(act: str, act_display: str) -> list[dict]:
    sections_dir = DATA_DIR / act / "sections"
    if not sections_dir.exists():
        return []

    results = []

    for md_path in sorted(sections_dir.rglob("*.md")):
        section_id = md_path.stem
        content = md_path.read_text(encoding="utf-8", errors="replace")
        body = get_body(content)
        if not body.strip():
            continue
        results.extend(scan_section_body(body, act, act_display, section_id))

    return dedupe_records(results)


def source_commit() -> str:
    """The git commit the index was generated from (provenance)."""
    try:
        return subprocess.run(
            ["git", "-C", str(BASE), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    all_defs = []
    for act, display in sorted(ACTS.items()):
        print(f"Scanning {act} ({display})...")
        defs = scan_act(act, display)
        all_defs.extend(defs)
        print(f"  -> {len(defs)} definitions found")

    # Dedup: first occurrence wins (term+act, or term+act+section when a scope
    # qualifier was stripped — see dedupe_sig).
    unique = dedupe_records(all_defs)

    print(f"\nTotal raw: {len(all_defs)}")
    print(f"Unique: {len(unique)}")

    # Per-act breakdown
    print("\nPer-act:")
    for act, display in sorted(ACTS.items()):
        n = sum(1 for d in unique if d['act'] == act)
        # Also show source breakdown
        std = sum(1 for d in unique if d['act'] == act and d['source'] == 'std')
        called = sum(1 for d in unique if d['act'] == act and d['source'] == 'called')
        print(f"  {display}: {n} ({std} std, {called} called)")

    # "scoped"/"dedupe_unscoped" are internal dedup markers, not part of the
    # published record shape.
    for d in unique:
        d.pop("scoped", None)

    # Write (with provenance: CDN-0209 asks for generator + source_commit).
    out = {
        "generator": "scripts/build_definitions_index.py",
        "source_commit": source_commit(),
        "count": len(unique),
        "definitions": unique,
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nWritten: {OUT_PATH} ({OUT_PATH.stat().st_size / 1024:.0f} KB)")

    return 0


if __name__ == "__main__":
    if "--dictionaries" in sys.argv:
        sys.exit(run_dictionary_phase())
    sys.exit(main())