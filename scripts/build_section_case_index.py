"""Build bidirectional index: case citations <-> legislation sections from catchwords."""

import json
import re
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data")

# ---------------------------------------------------------------------------
# Load available sections per act
# ---------------------------------------------------------------------------

def load_section_ids(act: str) -> set[str]:
    tree = json.loads((DATA_DIR / act / 'tree.json').read_text())
    ids = set()
    for part in tree.get('parts', []):
        for sec in part.get('sections', []):
            ids.add(sec['id'])
        for div in part.get('divisions', []):
            for sec in div.get('sections', []):
                ids.add(sec['id'])
            for sub in div.get('subdivisions', []):
                for sec in sub.get('sections', []):
                    ids.add(sec['id'])
    return ids

ACT_SECTIONS = {
    'itaa-1997': load_section_ids('itaa-1997'),
    'gst-1999': load_section_ids('gst-1999'),
    'itaa-1936': load_section_ids('itaa-1936'),
    'taa-1953': load_section_ids('taa-1953'),
    'fbt-1986': load_section_ids('fbt-1986'),
    'sis-1993': load_section_ids('sis-1993'),
}

BASE_RE = r'([0-9]+\s*-\s*[0-9]+(?:\s*-\s*[0-9]+)?|[A-Z]*[0-9]+[A-Z]*|[0-9]+)'

BASE_LOOKUP: dict[str, list[tuple[str, str]]] = defaultdict(list)
for act, ids in ACT_SECTIONS.items():
    for sid in ids:
        m = re.match(BASE_RE, sid)
        base = m.group(1) if m else sid
        BASE_LOOKUP[base].append((act, sid))

# Bases that exist in multiple acts — require explicit context
AMBIGUOUS_BASES = {
    b for b, acts in BASE_LOOKUP.items()
    if len({a for a, _ in acts}) > 1
}

# Well-known ITAA 1936 sections safe to infer without context
ITAA1936_KNOWN = {
    '6', '26', '46', '47', '48', '50', '51', '63', '79', '80', '82',
    '97', '99', '100', '102', '109', '121', '128', '160', '165', '166',
    '177', '178', '179', '180', '190', '193', '222', '254', '255',
}

# Generic plain numbers that should NOT be inferred to TAA without context
TAA_GENERIC_BLOCK = {str(i) for i in range(1, 100)}

# ---------------------------------------------------------------------------
# Act detection
# ---------------------------------------------------------------------------

ACT_PATTERNS = [
    (r'Income Tax Assessment Act\s+1997|ITAA\s*1997', 'itaa-1997'),
    (r'Income Tax Assessment Act\s+1936|ITAA\s*1936', 'itaa-1936'),
    (r'A New Tax System \(Goods and Services Tax\) Act\s+1999|GST Act', 'gst-1999'),
    (r'Taxation Administration Act\s+1953|TAA\s*1953', 'taa-1953'),
    (r'Fringe Benefits Tax Assessment Act\s+1986|FBTAA|FBT Act', 'fbt-1986'),
    (r'Superannuation Industry \(Supervision\) Act\s+1993|SIS Act', 'sis-1993'),
    (r'Bankruptcy Act\s+1966', 'bankruptcy-1966'),
]


def detect_act(text: str) -> str | None:
    text = text.replace('\n', ' ')
    for pattern, act_id in ACT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return act_id
    return None


def act_mentions(text: str) -> list[tuple[int, int, str]]:
    """Sorted (start, end, act_id) of every act-name mention in text."""
    out = []
    for pattern, act_id in ACT_PATTERNS:
        for mm in re.finditer(pattern, text, re.IGNORECASE):
            out.append((mm.start(), mm.end(), act_id))
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Section reference extraction
# ---------------------------------------------------------------------------

# Section numbers: one optional letter per number part, optional dashed parts,
# optional (sub) paragraph suffixes. Hyphenated numbers (105-50, 284-75) are
# matched whole — never truncated to the leading integer (CDN-0185). Final
# no-lowercase lookahead stops glued act names ('s 9-5Tax Laws...') from
# being eaten as letter suffixes.
SEC_NUM = r'\d+[A-Z]?(?:-\d+[A-Z]?)*(?:\(\d+[A-Z]?\))*(?:\([a-z]\))*(?![a-z])'

REF_START_RE = re.compile(
    r'\b(?:s{1,2}\.?\s+|sections?\s+)(' + SEC_NUM + r')',
    re.IGNORECASE,
)

FOLLOW_ACT_WINDOW = 120  # prose: 's 105-50 of Schedule 1 to the TAA' — act after ref
PRECEDE_ACT_WINDOW = 200  # list/prose: 'the GST Act ... s 38-325' — act before ref


def extract_refs(text: str) -> list[tuple[str, str, str]]:
    """Extract (base, section_id, act) triples with act-scoped binding.

    CDN-0184/CDN-0185 fix: a section ref is bound to the act that governs it,
    NOT to every act named anywhere in a wide context window. Resolution:
      1. nearest PRECEDING act mention (within 200 chars) — the 'Legislation'
         list format (ActName, ss X, Y, Z ActName2, ss W) and prose where the
         act is named before the ref;
      2. else nearest FOLLOWING act mention (within 120 chars) — prose where
         the ref precedes the act ('s 105-50 of Schedule 1 to the TAA');
      3. else no act: refs with no act context are skipped (never defaulted
         wholesale to ITAA 1997 — that misattribution is the old bug).
    Comma-continuation lists ('ss 105-5(1), 284-75, 284-90') bind to the same
    act as their parent ref.
    """
    mentions = act_mentions(text)
    refs = []

    def act_for_ref(pos: int) -> str | None:
        preceding = [mn for mn in mentions if mn[1] <= pos and pos - mn[1] <= PRECEDE_ACT_WINDOW]
        following = [mn for mn in mentions if mn[0] >= pos and mn[0] - pos <= FOLLOW_ACT_WINDOW]
        if following:
            # Prose binding: 'ss X ... of [Schedule N of] the <Act>' — an act
            # named shortly AFTER the ref with 'of'-connective governs it
            # ('s 105-50 of Schedule 1 to the TAA'). In list mode
            # ('GST Act, ss 31-5, 31-8 TAA, ss 105-5') the following act is the
            # NEXT list item — the 'of' connective is absent, so preceding wins.
            gap = text[pos:following[0][0]]
            if len(gap) <= 90 and re.search(r'\bof\b', gap, re.IGNORECASE) and not re.search(r'[.;–]|consideration|whether|where|if\b', gap, re.IGNORECASE):
                return following[0][2]
        if preceding:
            return preceding[-1][2]
        return None

    for m in REF_START_RE.finditer(text):
        sec = m.group(1)
        act = act_for_ref(m.start())
        if not act:
            continue
        refs.append((sec, act, m.start()))

    # Comma continuations: after each captured ref, scan forward until the
    # next act mention for ', SEC_NUM' list items (same governing act).
    more = []
    for sec, act, start in refs:
        end_ctx = next((mn[0] for mn in mentions if mn[0] >= start), len(text))
        ctx = text[start:end_ctx]
        for cm in re.finditer(r',\s*(' + SEC_NUM + r')', ctx):
            more.append((cm.group(1), act))

    all_refs = [(s, a) for s, a, _ in refs] + more
    seen = set()
    deduped = []
    for base, act in all_refs:
        # false positives: 4-digit years captured as 'sections'
        if re.fullmatch(r'\d{4}', base):
            continue
        # resolve to a corpus section id under that act
        if act and base in BASE_LOOKUP:
            candidates = BASE_LOOKUP[base]
            for a, sid in candidates:
                if a == act:
                    key = (base, sid, act)
                    if key not in seen:
                        seen.add(key)
                        deduped.append((base, sid, act))
                    break
    return deduped


def infer_act(base: str) -> str | None:
    """Infer act for unambiguous bases when no explicit context exists."""
    if base not in BASE_LOOKUP:
        return None
    candidates = BASE_LOOKUP[base]
    acts = {a for a, _ in candidates}
    if len(acts) == 1:
        act = candidates[0][0]
        # Block generic TAA numbers without context
        if act == 'taa-1953' and base in TAA_GENERIC_BLOCK:
            return None
        return act

    # ITAA 1936 anti-avoidance / well-known sections
    if re.match(r'^[A-Z]*[0-9]+[A-Z]+$', base) or base in ITAA1936_KNOWN:
        for a, _ in candidates:
            if a == 'itaa-1936':
                return 'itaa-1936'

    # TAA specific sections
    if base in ('14ZZO', '14ZZ', '14ZZK', '14ZZL', '14ZZN', '298-20', '255-1', '350-10'):
        for a, _ in candidates:
            if a == 'taa-1953':
                return 'taa-1953'

    # Dashed sections: prefer GST for known prefixes, otherwise ITAA 1997
    if re.match(r'^[0-9]+-[0-9]+$', base):
        gst_prefixes = (
            '9-', '15-', '17-', '23-', '29-', '33-', '37-', '38-', '39-', '40-',
            '48-', '54-', '57-', '58-', '75-', '78-', '79-', '81-', '82-', '83-',
            '84-', '87-', '90-', '93-', '96-', '99-', '105-', '110-', '111-',
            '117-', '123-', '129-', '132-', '135-', '141-', '144-', '150-',
            '153-', '156-', '162-', '165-', '168-', '171-', '177-', '183-',
            '189-', '195-'
        )
        if base.startswith(gst_prefixes):
            for a, _ in candidates:
                if a == 'gst-1999':
                    return 'gst-1999'
        for a, _ in candidates:
            if a == 'itaa-1997':
                return 'itaa-1997'
        for a, _ in candidates:
            if a == 'taa-1953':
                return 'taa-1953'

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with open(DATA_DIR / 'case_catchwords.json') as f:
        catchwords = json.load(f)

    section_to_cases = defaultdict(list)
    case_to_sections = defaultdict(list)

    for citation, text in catchwords.items():
        refs = extract_refs(text)
        for base, sid, act in refs:
            key = f"{act}:{sid}"
            if not any(c['citation'] == citation for c in section_to_cases[key]):
                section_to_cases[key].append({'citation': citation, 'catchwords': text})
            case_to_sections[citation].append({'act': act, 'section': sid, 'base': base})

    with open(DATA_DIR / 'section_case_index.json', 'w') as f:
        json.dump(dict(section_to_cases), f, indent=2)
    with open(DATA_DIR / 'case_section_refs.json', 'w') as f:
        json.dump(dict(case_to_sections), f, indent=2)

    print(f"Section->Cases: {len(section_to_cases)} entries")
    print(f"Case->Sections: {len(case_to_sections)} entries")
    act_counts = defaultdict(int)
    for key, cases in section_to_cases.items():
        act_counts[key.split(':')[0]] += len(cases)
    for act, count in sorted(act_counts.items()):
        print(f"  {act}: {count} links")


if __name__ == '__main__':
    main()
