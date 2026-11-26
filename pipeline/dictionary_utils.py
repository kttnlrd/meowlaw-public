"""
dictionary_utils.py — shared helpers for detecting where a new dictionary
definition begins in PDF-derived text.

Used by the parse_*.py structural parsers so that the big "Dictionary" sections
(ITAA 1997 s 995-1, GST Act s 195-1, ITAA 1936 s 6) split each defined term onto
its own paragraph instead of merging dozens of definitions into one run-on line.
"""

from __future__ import annotations

import re

# A line begins a new definition if it looks like:
#   "<term> means ...", "<term> includes ...", "<term> has the meaning given by ...",
#   "<term> has the same meaning as ...", "<term> has ...", or "<term>: ..." (colon form).
# The leading term may be asterisked (e.g. "*entity means ...").
#
# The colon alternative is only safe in combination with the stop-word guard in
# starts_new_definition() — on its own it would false-split lines like "However:"
# or "as follows:".
# Include Unicode curly quotes (U+2018/U+2019) commonly found in PDF-extracted text.
# CDN-0209: "&" belongs in the term character class — without it a run-on
# "R&D entity has the meaning given by ..." is not recognised as a new
# definition and the R&D family never reaches the dictionary.
DEF_START_RE = re.compile(
    r"^"                                  # start of line
    r"\*?"                                # optional leading asterisk
    r"[\w%*-]"                            # first char of term
    r"[\w\s%*()'&\u2018\u2019-]{0,80}?"   # rest of term (non-greedy, bounded)
    r"(?:"
    r"\s+(?:"
    r"has the meaning given(?: by)?|"
    r"has the meaning affected by|"
    r"has the same meaning as|"
    r"means|includes|has"
    r")\b"
    r"|:\s"                               # colon-style definition
    r")"
)

# First words that signal a continuation/sentence rather than a new defined term.
CONTINUATION_STARTERS = {
    "it", "they", "you", "he", "she", "we", "i",
    "however", "but", "and", "or", "if", "when", "where", "then",
    "note", "example", "item", "paragraph", "subsection", "section",
    "also", "furthermore", "moreover", "nevertheless", "otherwise",
}


def starts_new_definition(text: str) -> bool:
    """
    True iff ``text`` begins a new dictionary definition.

    Requires DEF_START_RE to match AND the first word (lowercased, punctuation
    stripped) to not be a known continuation starter AND the line to not begin
    with a paragraph/subparagraph marker like "(a)" / "(i)" / "(1)".
    """
    if not DEF_START_RE.match(text):
        return False

    # Paragraph markers "(a)", "(ii)", "(1)" are always continuations.
    if re.match(r"^\([a-z0-9]+\)", text):
        return False

    words = text.split()
    if not words:
        return False
    first_word = re.sub(r"[,;:.]$", "", words[0].lstrip("*").lower())
    if first_word in CONTINUATION_STARTERS:
        return False

    return True