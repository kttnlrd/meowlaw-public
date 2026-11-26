"""Markdown processing utilities."""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.config import DATA_DIR
from backend.services.data_loader import load_definitions

def link_legislation_refs(markdown: str, context_act: str) -> str:
    """Convert legislation section references in markdown to internal links."""
    split_re = re.compile(r"(```[\s\S]*?```|`[^`]+`|\[[^\]]+\]\([^)]+\))")

    def cross_act_replacer(m: re.Match) -> str:
        act_name = re.sub(r'[^a-z0-9]', '', m.group(1).lower())
        target_act = CROSS_ACT_NAME_MAP.get(act_name, context_act)
        section = m.group(2)
        return f'[{m.group(0)}](/{target_act}/s{section})'

    def section_replacer(m: re.Match) -> str:
        section = m.group(2)
        target_act = context_act
        if context_act.startswith("master-"):
            target_act = "itaa-1997"
        return f'[{m.group(0)}](/{target_act}/s{section})'

    tokens = []
    last = 0
    for m in split_re.finditer(markdown):
        if m.start() > last:
            text = markdown[last:m.start()]
            text = CROSS_ACT_RE.sub(cross_act_replacer, text)
            text = LEG_SECTION_RE.sub(section_replacer, text)
            tokens.append(text)
        tokens.append(m.group(0))
        last = m.end()
    if last < len(markdown):
        text = markdown[last:]
        text = CROSS_ACT_RE.sub(cross_act_replacer, text)
        text = LEG_SECTION_RE.sub(section_replacer, text)
        tokens.append(text)

    return "".join(tokens)


# ---------------------------------------------------------------------------
# Markdown processors
# ---------------------------------------------------------------------------

# Predicates that introduce definitions
DEF_PREDICATE_RE = re.compile(
    r"^(\*\*\(\d+\)\*\*\s+)?"  # optional subsection marker at line start
    r"([A-Za-z0-9\-'%*()][A-Za-z0-9\-'%*() ]{0,80}?)\s+"
    r"(has the meaning given by|means|has the same meaning as(?: in)?|"
    r"has (?:a|the) meaning affected by|includes)",
    re.IGNORECASE | re.MULTILINE,
)

# Colon definitions: term at start of line followed by colon
# Only in dictionary sections (995-1 / s6)
DEF_COLON_RE = re.compile(
    r"^(\*\*\(\d+\)\*\*\s+)?"  # optional subsection marker
    r"([A-Za-z0-9\-'%*][A-Za-z0-9\-'%* ]{0,80}?):\s+"
    r"(?:>|[A-Z]|see\s|a\s|an\s|the\s|this\s|that\s|it\s)",  # '>' = definition body starts as a blockquote
    re.IGNORECASE | re.MULTILINE,
)

# Terms that should NOT be bold+italicized (false positives)
FALSE_STARTS = {
    "The ", "This ", "Note:", "Section ", "Division ", "Part ",
    "For ", "If ", "It ", "There ", "Subject ", "Without ",
    "Under ", "Over ", "From ", "With ", "By ", "At ", "In ", "On ",
    "To ", "Of ", "And ", "Or ", "But ",
}

STOP_WORDS = {
    "a", "an", "and", "or", "the", "to", "of", "in", "for", "on", "at", "by",
    "with", "from", "as", "is", "it", "its", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "must", "shall", "can", "need", "dare", "ought", "used",
    "if", "then", "than", "that", "this", "these", "those", "such", "so",
    "not", "no", "nor", "but", "yet", "however", "therefore", "thus", "hence",
    "when", "where", "why", "how", "what", "which", "who", "whom", "whose",
    "all", "any", "both", "each", "every", "few", "more", "most", "other",
    "some", "only", "own", "same", "too", "very",
    "just", "also", "now", "here", "there", "up", "out", "down", "off",
    "over", "under", "again", "further", "once", "about", "into", "through",
    "during", "before", "after", "above", "below", "between", "among",
    "within", "without", "against",
}


def _is_false_positive(term: str) -> bool:
    t = term.strip()
    if not t or len(t) < 2:
        return True
    for prefix in FALSE_STARTS:
        if t.startswith(prefix):
            return True
    if t.lower() in STOP_WORDS:
        return True
    lowered = t.lower()
    if "subsection" in lowered or " section" in lowered:
        words = lowered.split()
        if "section" in words or "subsection" in words:
            return True
    return False


def format_definition_terms(markdown: str, section: str, act: str = "") -> str:
    """
    In dictionary sections (995-1 / s6 / 195-1), bold+italicize the term being defined
    and inject its per-term anchor so definition links can scroll to it.
    Only processes the subsection (1) block to avoid false positives elsewhere.
    """
    if section not in ("995-1", "6", "195-1"):
        return markdown

    # Find subsection (1) bounds
    m = re.search(r'(<a id="[^"]+"></a>\n)?\*\*\(1\)\*\*', markdown)
    if m:
        next_sub = re.search(r'\n\*\*\(\d+\)\*\*', markdown[m.end():])
        sub1_end = m.end() + next_sub.start() if next_sub else len(markdown)
        sub1_start = m.start()
    elif section == "195-1":
        # GST dictionary has no numbered subsections — one continuous block after the heading
        heading = re.search(r'^# .*\n', markdown, re.MULTILINE)
        sub1_start = heading.end() if heading else 0
        sub1_end = len(markdown)
    else:
        return markdown
    sub1_text = markdown[sub1_start:sub1_end]

    # Anchor injection: the parser only emits subsection-level <a id> tags, so the
    # per-term anchors that definition links target must be added at serve time.
    defs = load_definitions(act) if act else {}
    existing_ids = set(re.findall(r'<a id="([^"]+)"', markdown))

    def _anchor_for(term: str) -> str:
        # Definition keys are stored star-free; terms in the markdown may carry
        # embedded *cross-ref markers.
        info = defs.get(term.replace("*", "").lower())
        if not info:
            return ""
        anchor = info.get("anchor", "")
        if not anchor or info.get("section") != section or anchor in existing_ids:
            return ""
        existing_ids.add(anchor)
        return f'<a id="{anchor}"></a>'

    def predicate_replacer(match: re.Match) -> str:
        prefix = match.group(1) or ""
        term = match.group(2).strip()
        predicate = match.group(3)
        if _is_false_positive(term.replace("*", "")):
            return match.group(0)
        anchor = _anchor_for(term)
        if "*" in term:
            return f"{prefix}{anchor}{term} {predicate}"
        return f"{prefix}{anchor}***{term}*** {predicate}"

    def colon_replacer(match: re.Match) -> str:
        prefix = match.group(1) or ""
        term = match.group(2).strip()
        if _is_false_positive(term.replace("*", "")):
            return match.group(0)
        anchor = _anchor_for(term)
        original = match.group(0)
        if "*" in term:
            return original.replace(term, f"{anchor}{term}", 1)
        return original.replace(term, f"{anchor}***{term}***", 1)

    result = DEF_PREDICATE_RE.sub(predicate_replacer, sub1_text)
    result = DEF_COLON_RE.sub(colon_replacer, result)

    # Fallback: if no predicate matches found, try a more general regex
    # (needed for ITAA 1936 s6 where definitions are on one continuous line)
    if section in ("6", "195-1") and "***" not in result:
        GENERAL_PREDICATE_RE = re.compile(
            r"([A-Za-z0-9\-'%*][A-Za-z0-9\-'%* ]{0,80}?)\s+"
            r"(has the meaning given by|means|has the same meaning as(?: in)?|"
            r"has (?:a|the) meaning affected by|includes)",
            re.IGNORECASE,
        )

        def general_predicate_replacer(match: re.Match) -> str:
            term = match.group(1).strip()
            predicate = match.group(2)
            if _is_false_positive(term.replace("*", "")):
                return match.group(0)
            anchor = _anchor_for(term)
            if "*" in term:
                return f"{anchor}{term} {predicate}"
            return f"{anchor}***{term}*** {predicate}"

        result = GENERAL_PREDICATE_RE.sub(general_predicate_replacer, sub1_text)

    return markdown[:sub1_start] + result + markdown[sub1_end:]


def link_definitions(markdown: str, act: str) -> str:
    defs = load_definitions(act)
    if not defs:
        return markdown

    # Fast lookup: group terms by first word so we only check relevant candidates
    terms_by_first_word: dict[str, list[str]] = {}
    for term in sorted(defs.keys(), key=len, reverse=True):
        first = term.split()[0].lower() if " " in term else term.lower()
        terms_by_first_word.setdefault(first, []).append(term)

    # Match * followed by candidate text (word chars, spaces, %, parentheses, hyphens)
    # Skip * that is immediately preceded by ** (i.e. inside *** bold+italic wrappers)
    star_re = re.compile(r"(?<!\*\*)\*(?!\s)([\w%][\w\s%()-]*)")

    def replacer(m: re.Match) -> str:
        candidate = m.group(1)
        words = candidate.split()
        for i in range(len(words), 0, -1):
            prefix = " ".join(words[:i])
            key = prefix.lower()
            info = defs.get(key)
            if info:
                remainder = candidate[len(prefix) :]
                # Check if next char is a bare * (italic close) — consume it into the link text
                next_char = m.string[m.end()] if m.end() < len(m.string) else ""
                if next_char == "*" and not m.string[m.end() + 1 : m.end() + 2] == "*":
                    return f'[*{prefix}*](/{act}/s{info["section"]}#{info["anchor"]}){remainder}'
                return f'[*{prefix}](/{act}/s{info["section"]}#{info["anchor"]}){remainder}'
        return m.group(0)

    tokens = []
    split_re = re.compile(r"(```[\s\S]*?```|`[^`]+`|\[[^\]]+\]\([^)]+\))")
    last = 0
    for m in split_re.finditer(markdown):
        if m.start() > last:
            tokens.append(("text", star_re.sub(replacer, markdown[last : m.start()])))
        tokens.append(("code", m.group(0)))
        last = m.end()
    if last < len(markdown):
        tokens.append(("text", star_re.sub(replacer, markdown[last:])))

    return "".join(t[1] for t in tokens)


def link_section_references(markdown: str, act: str) -> str:
    """Auto-link internal section references (e.g. 'section 50-1'). Stub — returns unchanged."""
    return markdown


def link_cross_act_references(markdown: str, act: str) -> str:
    """Auto-link cross-act references (e.g. 'ITAA 1936 section 6'). Stub — returns unchanged."""
    return markdown


def auto_link_definitions(markdown: str, act: str, section: str) -> str:
    """In non-dictionary sections, auto-link first occurrence of defined terms."""
    if section in ("995-1", "6", "195-1"):
        return markdown

    defs = load_definitions(act)
    if not defs:
        return markdown

    # Sort terms by length descending so longer terms match first
    terms = sorted(defs.keys(), key=len, reverse=True)
    patterns = {term: re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)', re.IGNORECASE) for term in terms}

    # Split markdown into protected chunks (code blocks, existing links) and text
    split_re = re.compile(r"(```[\s\S]*?```|`[^`]+`|\[[^\]]+\]\([^)]+\))")

    segments = []
    last = 0
    for m in split_re.finditer(markdown):
        if m.start() > last:
            segments.append(("text", markdown[last:m.start()]))
        segments.append(("code", m.group(0)))
        last = m.end()
    if last < len(markdown):
        segments.append(("text", markdown[last:]))

    result = []
    linked_terms_overall = set() # Track terms linked globally across all segments

    for seg_type, seg_text in segments:
        if seg_type == "code":
            result.append(seg_text)
            continue

        current_segment_output = seg_text # This will be modified with placeholders
        placeholders_to_restore_in_segment = []
        placeholder_counter = 0

        for term in terms:
            if term in linked_terms_overall:
                continue # This term has already been linked in a previous segment or earlier in this segment

            pat = patterns[term]
            # Find the first occurrence of the term in the current_segment_output
            # which will have placeholders for terms already processed in this segment
            m = pat.search(current_segment_output)

            if m:
                info = defs[term]
                section_val = info.get("section", "")
                anchor_val = info.get("anchor", "")

                link_format = ""
                if section_val and anchor_val:
                    link_format = f'[*{m.group(0)}*](/{act}/s{section_val}#{anchor_val})'
                else:
                    # Definition exists but has no section/anchor mapping — render without link
                    link_format = f'[*{m.group(0)}*]'

                # Replace the term with a unique placeholder within this segment processing
                placeholder = f"__LINK_PH_{placeholder_counter}__"
                placeholders_to_restore_in_segment.append((placeholder, link_format))

                current_segment_output = (
                    current_segment_output[:m.start()] + \
                    placeholder + \
                    current_segment_output[m.end():]
                )
                linked_terms_overall.add(term) # Mark this term as linked globally
                placeholder_counter += 1

        # After processing all terms for the current segment, restore the actual links
        # from their placeholders.
        for ph, real_link in placeholders_to_restore_in_segment:
            current_segment_output = current_segment_output.replace(ph, real_link)

        result.append(current_segment_output)

    return "".join(result)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

