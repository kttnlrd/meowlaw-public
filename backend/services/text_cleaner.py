"""Text cleaning utilities for case paragraphs and ruling bodies."""

from __future__ import annotations

import re

# ── AustLII navigation noise ────────────────────────────────────────────────

_AUSTLII_NAV_LINES: list[re.Pattern] = [
    re.compile(r'^(Home|Databases|WorldLII|Search|Feedback)\s*$', re.IGNORECASE),
    re.compile(r'^(Database Search|Name Search|Recent Decisions)\s*$', re.IGNORECASE),
    re.compile(r'^\[(Noteup|Download|Help)\]\s*$'),
    re.compile(r'^\[?(You are here|Last Updated)\s*:.*$', re.IGNORECASE),
    re.compile(r'^AustLII\s*:?\s*$', re.IGNORECASE),
    re.compile(r'^\*\s+AustLII:\s*$'),
    re.compile(r'^\*{3,}\s*$'),  # horizontal rule-like
    re.compile(r'^-{3,}\s*$'),   # horizontal rule-like
    re.compile(r'^={3,}\s*$'),   # horizontal rule-like
    re.compile(r'^[|+\-*]{8,}\s*$'),  # decorative borders
]

_AUSTLII_SUB_PATTERNS = [
    (re.compile(r'\s*\[Noteup\]\s*'), ' '),
    (re.compile(r'\s*\[Download\]\s*'), ' '),
    (re.compile(r'\s*\[Help\]\s*'), ' '),
    (re.compile(r'\s*\[Home\]\s*'), ' '),
    (re.compile(r'\s*\[Databases\]\s*'), ' '),
    (re.compile(r'\s*\[WorldLII\]\s*'), ' '),
    (re.compile(r'\s*\[Search\]\s*'), ' '),
    (re.compile(r'\s*\[Feedback\]\s*'), ' '),
    (re.compile(r'\s*\[Database Search\]\s*'), ' '),
    (re.compile(r'\s*\[Name Search\]\s*'), ' '),
    (re.compile(r'\s*\[Recent Decisions\]\s*'), ' '),
    (re.compile(r'\s*\[Index\]\s*'), ' '),
    (re.compile(r'\s*Last Updated:\s*[^\]]*\]?', re.IGNORECASE), ' '),
    (re.compile(r'\s*You are here:\s*[^\]]*\]?', re.IGNORECASE), ' '),
]


# Patterns for footnote-only lines (short citation references, not judgment text)
_FOOTNOTE_PATTERNS = [
    re.compile(r'^\(\d{4}\)\s+\d+\s+\w+\s+\d+'),  # (2003) 212 CLR 511.
    re.compile(r'^\[\d{4}\]\s+\w+\s+\d+'),  # [1984] HCA 61
    re.compile(r'^\d+\s+IR\s+\d+'),  # 334 IR 70
    re.compile(r'^\d+\s+NSWLR\s+\d+'),  # 117 NSWLR 253
    re.compile(r'^\d+\s+ALR\s+\d+'),  # 212 ALR 1
    re.compile(r'^\d+\s+ALJR\s+\d+'),  # 90 ALJR 1
    re.compile(r'^\d+\s+FCR\s+\d+'),  # 150 FCR 1
    re.compile(r'^\d+\s+A\s+W+N+\s+\(NSW\)\s+\d+'),  # 92 WN (NSW) 1070
    re.compile(r'^See\s+(also\s+)?our\s+reasons'),  # "See our reasons at [13], [16]"
    re.compile(r'^See\s+[A-Z]'),  # "See Santayana..."
]

_AUSTLII_SHORT_LINE_RE = re.compile(
    r'^(AustLII:|Copyright Policy|Disclaimers|Privacy Policy|Feedback)',
    re.IGNORECASE
)


# ── Centralised scraped-markup stripper for CDN-0095 ──────────────────────

_SCRAPED_PATTERNS: list[re.Pattern] = [
    # HTML tags (any) — but preserve <a id="..."> paragraph anchors and </a> (CDN-0094)
    re.compile(r'<(?!a\s+id="[^"]*"\s*>|/a\s*>)[^>]+>'),
    # Google Analytics / JS injection artifacts
    re.compile(r'//\s*\(function\s*\([^)]*\).*?;?\s*$', re.DOTALL),
    re.compile(r'bazadebezolkohpepadr="[^"]*"'),
    # jQuery / inline JS
    re.compile(r'\$\([^)]+\)\.\w+\([^)]*\);?'),
    re.compile(r'window\.dataLayer.*?;'),
    re.compile(r'gtag\([^)]*\);?'),
    # ATO navigation chrome (inline single-line)
    re.compile(
        r'(?i)Legal\s+database\s*(/\s*(Contents|Download|Email|Print|Back\s+to\s+browse)\s*)+',
    ),
    # ATO navigation chrome (multi-line block)
    re.compile(r'(?i)^(Legal\s+database\s*\n){1,2}'
               r'(Contents\s*\n)?'
               r'(Download\s*\n)?'
               r'(Email\s*\n)?'
               r'(Print\s*\n)?'
               r'(Back\s+to\s+browse\s*\n)?'
               r'(\d+\s+related\s+documents\s*\n)?',
               re.MULTILINE),
    # Generated on timestamps
    re.compile(r'(?i)Generated\s+on:\s*\d+\s+\w+\s+\d{4},\s*\d+:\d+:\d+\s*(AM|PM)'),
    # Page X of Y markers
    re.compile(r'(?i)Page\s+\d+\s+of\s+\d+'),
    # You are here navigation
    re.compile(r'(?i)You\s+are\s+here\s*:?\s*[^\n]*'),
    # Back to top
    re.compile(r'(?i)^Back\s+to\s+top\s*$', re.MULTILINE),
    # Standalone ato.gov.au
    re.compile(r'(?i)^ato\.gov\.au\s*$', re.MULTILINE),
    # Copyright / disclaimer blocks
    re.compile(r'(?i)^©\s*AUSTRALIAN\s+TAXATION\s+OFFICE.*$', re.MULTILINE),
    re.compile(r'(?i)^You\s+are\s+free\s+to\s+copy.*$', re.MULTILINE),
    re.compile(r'(?i)^Copyright\s+(notice|policy).*$', re.MULTILINE),
    re.compile(r'(?i)^Disclaimers?\s*$', re.MULTILINE),
    re.compile(r'(?i)^Privacy\s+Policy\s*$', re.MULTILINE),
    re.compile(r'(?i)^Feedback\s*$', re.MULTILINE),
    re.compile(r'(?i)^AustLII\s*:?\s*$', re.MULTILINE),
    re.compile(r'(?i)^Home\s*$', re.MULTILINE),
    re.compile(r'(?i)^Databases\s*$', re.MULTILINE),
    re.compile(r'(?i)^WorldLII\s*$', re.MULTILINE),
    re.compile(r'(?i)^Search\s*$', re.MULTILINE),
    # Standalone citation number lines that are just TOC noise
    re.compile(r'^[A-Z]{2,6}\s+\d{4}/\d+\s*$', re.MULTILINE),
    # Copyright symbol + trailing stuff
    re.compile(r'&#169;.*$', re.MULTILINE),
    # Inline [Noteup] [Download] [Help] brackets
    re.compile(r'\s*\[(Noteup|Download|Help|Home|Databases|WorldLII|Search|Feedback|Database\s+Search|Name\s+Search|Recent\s+Decisions|Index)\]\s*'),
]


def strip_scraped_markup(text: str) -> str:
    """Strip HTML tags, navigation chrome, scraped artifacts from any text output.

    Removes all known scraped-markup artifacts including:
    - HTML tags (<header>, <div>, <a id=\"...\">, etc.)
    - Google Analytics / JS injection artifacts
    - ATO navigation chrome (Legal database / Contents / Download / …)
    - Generated on: timestamps
    - Page X of Y markers
    - You are here: / Back to top / ato.gov.au
    - Copyright / disclaimer / privacy policy lines
    - [Noteup] [Download] [Help] inline brackets
    - Trailing metadata sections (Keywords, Legislative References, etc.)
    """
    if not text:
        return ""

    result = text

    # Apply all pattern substitutions
    for pattern in _SCRAPED_PATTERNS:
        result = pattern.sub('', result)

    # Also strip trailing metadata (Keywords, Legislative References, etc.)
    result = _strip_trailing_metadata(result)

    # Collapse blank lines and trim
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = re.sub(r'^\s*\n', '\n', result, flags=re.MULTILINE)

    return result.strip()


# ── Legislation markdown → clean prose ─────────────────────────────────

def clean_legislation_body(text: str) -> str:
    """Render a legislation markdown body as clean prose.

    Legislation section files carry rendering scaffolding that is noise to a
    reader:

    * internal paragraph anchors (``<a id="s8-1-1"></a>``),
    * bold subsection/paragraph markers (``**(1)**``, ``**(a)**``),
    * ``*`` defined-term markers (e.g. ``*business``), and
    * ``>`` blockquote markers delimiting provision paragraphs.

    Strip these, then apply :func:`strip_scraped_markup` for generic HTML /
    navigation artifacts, so ``fetch`` returns readable prose instead of raw
    markup.
    """
    if not text:
        return ""

    # Internal paragraph / subsection anchors (standalone or inline). Only
    # consume trailing spaces/tabs — never the newline, which would merge the
    # following line's '>' blockquote marker into this one.
    text = re.sub(r'<a\s+id="[^"]*"\s*>\s*</a>[ \t]*', '', text)
    text = re.sub(r'<a\s+id="[^"]*"\s*>', '', text)
    text = re.sub(r'</a>[ \t]*', '', text)
    # Bold subsection/paragraph markers: **(1)** -> (1) ; **(a)** -> (a)
    text = re.sub(r'\*\*\s*\(([^)]+)\)\s*\*\*', r'(\1) ', text)
    # Remaining markdown bold: **text** -> text
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = text.replace('**', '')
    # '*' defined-term markers directly before a word (not list bullets).
    text = re.sub(r'(?<!\w)\*(?=[A-Za-z])', '', text)
    # Blockquote markers at line starts delimit provision paragraphs.
    text = re.sub(r'^[ \t]*>[ \t]?', '', text, flags=re.MULTILINE)
    # Generic scraped-markup / navigation noise.
    text = strip_scraped_markup(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Strip trailing metadata blocks ──────────────────────────────────────

_STRIP_TRAILING_AFTER = re.compile(
    r'\n\s*('
    r'Keywords|Related\s+Public\s+Rulings?|Other\s+References?|'
    r'Business\s+Line|Date\s+of\s+publication|ISSN|'
    r'history|Archived|Original\s+statement|'
    r'Legislative\s+References'
    r')\s*[:：]?\s*\n',
    re.IGNORECASE
)


def _strip_trailing_metadata(body: str) -> str:
    """Remove trailing metadata sections from ATO ID and ruling bodies.
    
    Truncates from the first metadata section header to end of string.
    This handles cases where the headers were already stripped by line-level
    filtering — the remaining keyword values and metadata text still get removed.
    """
    m = _STRIP_TRAILING_AFTER.search(body)
    if m:
        body = body[:m.start()]
    return body.strip()


def clean_case_paragraph(content: str) -> str:
    """Strip AustLII navigation noise and footnote-only lines from a paragraph.

    Removes lines that are purely navigation UI elements, and scrubs
    stray markers from within text. Returns empty string if the entire
    paragraph is noise.
    """
    lines = content.splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip pure navigation lines
        skip = any(p.match(stripped) for p in _AUSTLII_NAV_LINES)
        if skip:
            continue
        # Skip AustLII header boilerplate (Date of order, High Court of Australia HCA #, etc.)
        if any(p.search(stripped) for p in _AUSTLII_HEADER_LINES):
            continue
        # Skip short footnote-only lines
        stub = stripped.rstrip('.')
        if len(stub) < 80 and any(p.match(stub) for p in _FOOTNOTE_PATTERNS):
            continue
        if _AUSTLII_SHORT_LINE_RE.match(stripped):
            continue
        cleaned.append(line)
    result = '\n'.join(cleaned)

    # Remove inline markers
    for pattern, replacement in _AUSTLII_SUB_PATTERNS:
        result = pattern.sub(replacement, result)

    # Collapse multiple blank lines and trim
    result = re.sub(r'\n{3,}', '\n\n', result).strip()

    # Merge fragmented sentences: if a line starts lowercase / comma,
    # and prev line isn't empty and isn't a heading, join them
    merged_lines = []
    out_lines = result.splitlines()
    for line in out_lines:
        stripped = line.strip()
        if not stripped:
            merged_lines.append('')
            continue
        if merged_lines and merged_lines[-1]:
            prev = merged_lines[-1]
            if (stripped[0].islower() or stripped.startswith(',')) and not prev.startswith('#'):
                merged_lines[-1] = prev.rstrip() + ' ' + stripped
                continue
        merged_lines.append(stripped)
    result = '\n'.join(merged_lines)

    return result


# ── ATO ruling noise ────────────────────────────────────────────────────────

_ATO_NAV_LINES: list[re.Pattern] = [
    re.compile(r'^Legal database\s*$', re.IGNORECASE),
    re.compile(r'^(Contents|Download|Email|Print|Para)\s*$', re.IGNORECASE),
    re.compile(r'^Back to browse\s*$', re.IGNORECASE),
    re.compile(r'^\d+\s+related documents\s*$'),
    re.compile(r'^PDF version', re.IGNORECASE),
    re.compile(r'^is the authorised', re.IGNORECASE),
    re.compile(r'^There is a Compendium', re.IGNORECASE),
    re.compile(r'^Please note that the', re.IGNORECASE),
    re.compile(r'^This document incorporates revisions', re.IGNORECASE),
    re.compile(r'^View its history and amending notices', re.IGNORECASE),
    re.compile(r'^You are here\s*', re.IGNORECASE),
    re.compile(r'^Back to top\s*$', re.IGNORECASE),
    re.compile(r'^©\s*AUSTRALIAN TAXATION OFFICE', re.IGNORECASE),
    re.compile(r'^You are free to copy', re.IGNORECASE),
    re.compile(r'^(Taxation Ruling|Taxation Determination)\s*$', re.IGNORECASE),
    re.compile(r'^(Commissioner of Taxation)\s*$'),
    re.compile(r'^Previously released in draft form', re.IGNORECASE),
    re.compile(r'^[A-Z]{2,6}\s+\d{4}/\d+\s*$'),  # standalone citation like "TR 2020/1"
    re.compile(r'^[A-Z]{2,6}\s+\d{4}/\d+EC\s*$'),  # "TR 2020/1EC"
    re.compile(r'^ato\.gov\.au\s*$', re.IGNORECASE),
    re.compile(r'^history\s*$', re.IGNORECASE),
    re.compile(r'^and amending notices', re.IGNORECASE),
    re.compile(r'^\d+\s*$'),  # standalone digit(s) (TOC page number)
    re.compile(r'^\.\s*$'),  # standalone period
    re.compile(r'^\*{3,}\s*$'),
    re.compile(r'^-{3,}\s*$'),
    re.compile(r'^={3,}\s*$'),
    # ATO ID boilerplate
    re.compile(r'^ATO\s+Interpretative\s+Decision\s*$', re.IGNORECASE),
    re.compile(r'^ATO\s+ID\s+\d{4}/\d+\s*$', re.IGNORECASE),
    re.compile(r'^FOI\s+status\s*:', re.IGNORECASE),
]

_DESCRIPTIVE_TITLE_RE = re.compile(
    r'(?:TR\s+\d{4}/\d+|TD\s+\d{4}/\d+|SGR\s+\d{4}/\d+|GSTR\s+\d{4}/\d+'
    r'|PCG\s+\d{4}/\d+|LCG\s+\d{4}/\d+|MT\s+\d{4}/\d+|IT\s+\d+'
    r'|PS\s+LA\s+\d{4}/\d+|TA\s+\d{4}/\d+)\s*\n\s*(.+?)(?:\n\s*\n|$)',
    re.DOTALL
)

# Also match standalone descriptive title line (when citation heading was stripped)
_DESCRIPTIVE_TITLE_STANDALONE = re.compile(r'^[A-Z][a-z]+:\s+.+')

# AustLII case header boilerplate — text that's just the case title, citation, date, court
# These appear as the first 1-3 paragraphs and contain no judgment text
_AUSTLII_HEADER_LINES: list[re.Pattern] = [
    re.compile(r'Date of order:\s*\d+\s+\w+\s+\d+', re.IGNORECASE),
    re.compile(r'Date of pub', re.IGNORECASE),
    re.compile(r'High Court of Australia\s+HCA\s+\d+'),
    re.compile(r'Federal Court of Australia\s+FCA'),
    re.compile(r'Full Court of the Federal Court\s+FCAFC'),
]

_BOLD_CLEAN = re.compile(r'\*\*([^*]+)\*\*')


def clean_ruling_body(body: str) -> dict:
    """Strip ATO navigation noise from a ruling body.

    * Removes navigation lines (Legal database, Contents, Download, …)
    * Extracts the **descriptive title** (the multi-line text after the citation heading)
    * Converts section headers to ## markdown headings for ToC support
    * Returns ``{body: str, descriptive_title: str}``
    """
    lines = body.splitlines()
    cleaned: list[str] = []
    descriptive_title = ''

    # Extract descriptive title: lines after the citation heading
    # until a blank line or known ATO navigation pattern
    citation_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^(TR|TD|SGR|GSTR|PCG|LCG|MT|IT|PS LA|TA)\s+\d', stripped, re.IGNORECASE):
            citation_idx = i
            break

    if citation_idx is not None:
        title_lines = []
        for j in range(citation_idx + 1, min(citation_idx + 10, len(lines))):
            s = lines[j].strip()
            if not s:
                break
            # Stop at ATO navigation patterns
            if any(p.match(s) for p in [
                re.compile(r'^Please note that the', re.IGNORECASE),
                re.compile(r'^PDF version', re.IGNORECASE),
                re.compile(r'^There is a Compendium', re.IGNORECASE),
                re.compile(r'^Legal database', re.IGNORECASE),
                re.compile(r'^Contents', re.IGNORECASE),
                re.compile(r'^This document incorporates', re.IGNORECASE),
            ]):
                break
            title_lines.append(s)
        if title_lines:
            descriptive_title = ' '.join(title_lines)

    # Section header patterns — lines that should become ## headings
    # These are standalone lines that are NOT numbered paragraphs or bullet points
    _SECTION_HEADER_RE = re.compile(
        r'^(Summary|Ruling|Date of effect|Introduction|Background|'
        r'Purpose and context|Explanation|'
        r'Detailed analysis|Examples?|Application|'
        r'Commissioner of Taxation|'
        r'Relying on this Ruling|'
        r'Elements of|Importance of|Relevance of|'
        r'The negative tests|Capital or capital|Private or domestic|'
        r'Gaining or producing exempt|Substantiation|'
        r'Exceptions and relief|'
        r'Appendix\s+\d+|'
        r'Work-related|'
        r'Not legally binding|Legally binding)',
        re.IGNORECASE
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip pure navigation lines
        skip = any(p.match(stripped) for p in _ATO_NAV_LINES)
        if skip:
            continue
        # Convert section headers to ## headings (but not numbered paragraphs)
        if (_SECTION_HEADER_RE.match(stripped)
                and not re.match(r'^\d+[\.\)]', stripped)
                and not stripped.startswith('•')
                and len(stripped) < 80):
            cleaned.append(f'## {stripped}')
            continue
        cleaned.append(line)

    result = '\n'.join(cleaned)

    # Collapse multiple blank lines
    result = re.sub(r'\n{3,}', '\n\n', result)

    # Strip bold formatting from non-emphasis uses
    result = _BOLD_CLEAN.sub(r'\1', result)

    result = result.strip()
    result = _strip_citation_block(result)
    result = _strip_trailing_metadata(result)
    return {'body': result, 'descriptive_title': descriptive_title}


# ── Strip citation/cover-sheet block from body ──────────────────────────────


def _strip_citation_block(body: str) -> str:
    """Remove the initial citation line(s) and cover sheet from the body.

    The first few lines of a ruling typically are:
      TR 2024/1 - Income tax: composite items ...
      This cover sheet is provided for information only...
      There is a Compendium for this document...
      Generated on: ...

    These duplicate the page-level heading and should not render as body text.
    """
    lines = body.splitlines()
    if not lines or len(lines) < 3:
        return body

    # Find the first non-empty line
    first_content_idx = 0
    for i, line in enumerate(lines):
        if line.strip():
            first_content_idx = i
            break

    if first_content_idx >= len(lines):
        return body

    first_line = lines[first_content_idx].strip()

    # If the first line looks like a citation (e.g. "TR 2024/1 - ..." or "TR 2024/1 | ..."), strip it
    # and any following cover-sheet / generated-on lines
    if re.match(r'^[A-Z]{2,6}\s+\d{4}/\d+(\s*[-–—|]|\s*$)', first_line, re.IGNORECASE):
        # Find the first substantive line after the citation block
        start_idx = first_content_idx + 1
        for i in range(first_content_idx + 1, min(first_content_idx + 15, len(lines))):
            s = lines[i].strip()
            if not s:
                continue
            # Skip cover sheet, compendium, generated-on, and standalone "Taxation Ruling" lines
            if re.match(r'^(This cover sheet|There is a Compendium|Generated on|Taxation Ruling|Taxation Determination|Status:|[A-Z]{2,6}\s+\d{4}/\d+\s*$)', s, re.IGNORECASE):
                start_idx = i + 1
                continue
            # Stop at the first meaningful line
            break
        body = '\n'.join(lines[start_idx:])
    # ATO ID format: "ATO ID 2001/1" or "ATO Interpretative Decision"
    elif re.match(r'^ATO\s+(ID\s+\d{4}/\d+|Interpretative\s+Decision)', first_line, re.IGNORECASE):
        start_idx = first_content_idx + 1
        for i in range(first_content_idx + 1, min(first_content_idx + 10, len(lines))):
            s = lines[i].strip()
            if not s:
                continue
            # Skip the second header line, === separator, and FOI status
            if re.match(r'^(ATO\s+(ID\s+\d{4}/\d+|Interpretative\s+Decision)|===+$|FOI\s+status)', s, re.IGNORECASE):
                start_idx = i + 1
                continue
            break
        body = '\n'.join(lines[start_idx:])

    return body
