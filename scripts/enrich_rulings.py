"""Enrich all procedural maps with ATO rulings from the citation index.

For each node's statute refs, look up the citation index and attach
high-value rulings. Priority: TR > TD > PCG > PS LA > IT > LCG > GSTR > AID > MT > SMSF.
Clean titles parsed from the rulings tree. Dedupe across each map so the
same ruling does not appear on many nodes.

Fallback: when the citation index has no entry for a section, scan the raw
ruling text files for the section reference and attach the best matches.
The citation index is incomplete for alphanumeric sections (e.g. 26AH,
109C-109ZB have zero index entries while the rulings themselves reference
them), so the text scan is the ground truth.
"""
import json
import os
import re
import urllib.request

CITATION = json.load(open('data/citation_index.json'))
MAPS_DIR = 'data/maps'
RULINGS_DIR = 'data/rulings'
SUMMARIES_DIR = os.path.join(RULINGS_DIR, 'summaries')

PRIORITY = [('TR_', 8), ('TD_', 7), ('PCG_', 7), ('PS_LA_', 6), ('IT_', 5),
            ('LCG_', 4), ('GSTR_', 4), ('AID_', 3), ('MT_', 3), ('SMSF_', 3)]
SKIP = ('CR_', 'ATOID_', 'PR_', 'SBO_', 'DFT_', 'ID_')

def priority_of(cit):
    for prefix, prio in PRIORITY:
        if cit.startswith(prefix):
            return prio
    return 0

def fmt_citation(cit):
    """TR_1992_3 -> TR 1992/3"""
    m = re.match(r'([A-Z]+)_(\d{4})_(\d+)', cit)
    if not m:
        return cit
    prefix, year, num = m.groups()
    if prefix in ('PS', 'LCG', 'SMSF'):
        return f"{prefix} {year}/{num}"
    return f"{prefix} {year}/{num}"

def clean_title(cit, raw):
    """Parse a clean short title from the noisy tree title."""
    if not raw:
        return ''
    # strip leading citation prefix (TR 2010/3 —, AID 2002/249 —, IT 2346 —)
    t = re.sub(r'^[A-Z]+ \d{4}/\d+(?:[A-Za-z0-9]*)?\s*[—-]\s*', '', raw.strip())
    t = re.sub(r'^IT \d+\s*[—-]\s*', '', t.strip())
    # cut at common noise markers
    for marker in ('Date of effect', 'Subject References', 'Business Line',
                   'Legislative References', 'Related Rulings', 'History',
                   'Issued', 'Preamble', 'Introductory'):
        i = t.find(marker)
        if i > 40:
            t = t[:i]
    t = re.sub(r'\s{2,}', ' ', t).strip(' -—')
    if len(t) > 90:
        t = t[:90].rsplit(' ', 1)[0] + '…'
    if len(t) < 15:
        return ''
    return t

def load_ruling_titles():
    try:
        req = urllib.request.Request('http://localhost:8765/api/tree/rulings')
        d = json.load(urllib.request.urlopen(req, timeout=15))
        titles = {}
        for part in d.get('parts', []):
            for div in part.get('divisions', []):
                for s in div.get('sections', []):
                    titles[s['id']] = s.get('title', '')
        return titles
    except Exception as e:
        print('  [warning] ruling titles unavailable:', e)
        return {}

TITLES = load_ruling_titles()
print('ruling titles loaded:', len(TITLES))

def load_summary_titles():
    """Clean titles for rulings whose tree id differs from the file id
    (AIDs: tree uses ATOID_*, files use AID_*)."""
    titles = {}
    if not os.path.isdir(SUMMARIES_DIR):
        return titles
    for fn in os.listdir(SUMMARIES_DIR):
        if not fn.endswith('.json'):
            continue
        try:
            d = json.load(open(os.path.join(SUMMARIES_DIR, fn)))
            cit = d.get('citation', '')
            title = d.get('title', '')
            if cit and title:
                titles[fn[:-5]] = title
        except Exception:
            continue
    return titles

SUMMARY_TITLES = load_summary_titles()
print('summary titles loaded:', len(SUMMARY_TITLES))

# ---------------------------------------------------------------------------
# Text-derived section index (fallback ground truth).
# Scans every raw ruling text file for section references and maps
# lowercase base section -> list of ruling ids that mention it.
# ---------------------------------------------------------------------------

# Full form: "section 26AH", "subsection 26AH(13)", "Section 160AAB"
_SEC_RE = re.compile(
    r'(?:sub[\s-]*)?sections?\.?\s+'
    r'(\d{1,4}[A-Z]{0,3}(?:-\d{1,3})?(?:\([0-9A-Za-z]+\))?)',
    re.IGNORECASE)
# Bare alphanumeric token: "26AH(6)", "109D(3)". Requires a letter so plain
# years / page numbers ("s 1983", "1983") are not captured.
_BARE_RE = re.compile(
    r'(?<![\w.])(\d{1,4}[A-Z]{1,3}(?:-\d{1,3})?(?:\([0-9A-Za-z]+\))?)(?![\w])')
# Minimum weighted mention score for a section to count. "section 26AH" form
# scores 2, bare "26AH(6)" scores 1, bare "26AH" scores 0 (references tables
# list sections without discussing them, e.g. GSTR 2004/4 lists the whole of
# Div 7A once without commentary).
_MIN_SCORE = 3

# Acts that share ITAA-style hyphenated numbering (e.g. GST Act 1999 s 132-1
# vs ITAA 1997 s 132-1). Sections attributed to these acts in a ruling's
# Legislative References block must not be indexed for ITAA maps.
_NON_ITAA_ACTS = re.compile(
    r'ANTS\((?:GST|ABN|FBT)\)A 1999|GST Act 1999|FBT Act 1986|'
    r'A New Tax System \(Goods and Services Tax\) Act|Fringe Benefits Tax Assessment Act')

def _non_itaa_sections(text):
    """Sections attributed to non-ITAA acts in the Legislative References block."""
    excluded = set()
    m = re.search(r'Legislative References:\s*(.{0,4000})', text, re.DOTALL)
    if not m:
        return excluded
    block = m.group(1)
    # current act being listed; sections following it belong to that act
    for am in re.finditer(
            r'([A-Z][A-Z0-9() ]*(?:Act|A) 1999|ANTS\([A-Z]+\)A 1999|'
            r'FBT Act 1986|Fringe Benefits Tax Assessment Act 1986)'
            r'([^A-Z]{0,300})', block):
        act = am.group(1)
        if _NON_ITAA_ACTS.search(act):
            for sm in re.finditer(r'(\d{1,4}[A-Z]{0,3}(?:-\d{1,3})?)', am.group(2)):
                excluded.add(sm.group(1).lower())
    return excluded

def _normalise_section(tok):
    """'26AH(6)' -> '26ah'; '104-10(3)' -> '104-10'; '109D' -> '109d'"""
    return re.sub(r'\(.*', '', tok).strip().lower()

def build_text_index():
    """Return {base_section_lower: [ruling_id, ...]} from raw ruling texts."""
    index = {}
    if not os.path.isdir(RULINGS_DIR):
        print('  [warning] no rulings dir, text fallback disabled')
        return index
    for fn in sorted(os.listdir(RULINGS_DIR)):
        if not fn.endswith('.txt'):
            continue
        rid = fn[:-4]
        if rid.startswith(SKIP):
            continue
        try:
            with open(os.path.join(RULINGS_DIR, fn), encoding='utf-8', errors='replace') as f:
                text = f.read()
        except Exception:
            continue
        non_itaa = _non_itaa_sections(text)
        scores = {}
        for m in _SEC_RE.finditer(text):
            base = _normalise_section(m.group(1))
            if base and re.search(r'\d', base) and base not in non_itaa:
                scores[base] = scores.get(base, 0) + 2
        for m in _BARE_RE.finditer(text):
            if '(' not in m.group(1):
                continue  # bare no-paren: references-table noise
            base = _normalise_section(m.group(1))
            if base and re.search(r'\d', base) and base not in non_itaa:
                scores[base] = scores.get(base, 0) + 1
        for base, score in scores.items():
            if score >= _MIN_SCORE:
                index.setdefault(base, []).append(rid)
    return index

TEXT_INDEX = build_text_index()
print('text index sections:', len(TEXT_INDEX))

def _title_for(cit):
    """Title lookup: tree first, then summary files (AID_*)."""
    t = TITLES.get(cit, '')
    if t:
        return t
    return SUMMARY_TITLES.get(cit, '')

def enrich_map(fid):
    path = os.path.join(MAPS_DIR, fid + '.json')
    m = json.load(open(path))
    seen_global = set()  # dedupe across the map
    added = 0
    for n in m.get('nodes', []):
        if n.get('rulings'):
            continue  # already enriched (the new 8)
        # Collect section candidates per node from the citation index.
        refs_for_node = []
        text_cands = set()
        for st in n.get('statute', []):
            act = st.get('act', '')
            sec = st.get('section', '')
            act_refs = CITATION.get(act, {})
            base = re.sub(r'\(.*', '', sec).strip()
            # lowercase match against the index (keys are lowercase)
            base_l = base.lower()
            # do NOT strip trailing letters: 109C is a different section from
            # 109, and matching the numeric base was attaching junk rulings.
            cands_sec = set([sec, base, base_l])
            for cs in cands_sec:
                refs_for_node.extend(act_refs.get(cs, []))
            # text-index fallback candidates (same base, case-insensitive)
            if base_l in TEXT_INDEX:
                text_cands.update(TEXT_INDEX[base_l])
        # collect candidates, priority-ranked
        cands = {}
        for r in refs_for_node:
            if r.get('type') != 'ruling':
                continue
            cit = r.get('citation', '')
            if not cit or cit.startswith(SKIP):
                continue
            prio = priority_of(cit)
            if prio == 0:
                continue
            if cit in seen_global:
                continue
            cands.setdefault(prio, []).append(cit)
        # take up to 3, mixing priority buckets (best first)
        chosen = []
        for prio in sorted(cands, reverse=True):
            for cit in sorted(cands[prio]):
                if cit not in seen_global:
                    chosen.append({'id': cit, 'title': clean_title(cit, _title_for(cit))})
                    seen_global.add(cit)
                    if len(chosen) >= 3:
                        break
            if len(chosen) >= 3:
                break
        # Fallback: citation index came up empty for this node but the
        # rulings texts reference the section directly (e.g. 26AH, 109C+).
        if not chosen and text_cands:
            for cit in sorted(text_cands):
                prio = priority_of(cit)
                if prio == 0 or cit in seen_global:
                    continue
                chosen.append({'id': cit, 'title': clean_title(cit, _title_for(cit))})
                seen_global.add(cit)
                if len(chosen) >= 3:
                    break
        if chosen:
            n['rulings'] = chosen
            added += len(chosen)
    json.dump(m, open(path, 'w'), indent=1, ensure_ascii=False)
    return added

if __name__ == '__main__':
    new8 = {'itaa-1997-s6-5-ordinary-income', 'itaa-1997-s8-1-general-deductions',
            'itaa-1997-div-85-psi', 'itaa-1936-s44-dividends',
            'itaa-1936-s23ah-foreign-branch', 'itaa-1936-s47a-cfc-distribution-benefits',
            'itaa-1936-pe-domestic-treaty', 'itaa-1936-s25a-myer-profit-making-schemes'}
    total = 0
    for f in sorted(os.listdir(MAPS_DIR)):
        if not f.endswith('.json'):
            continue
        fid = f[:-5]
        if fid in new8:
            continue
        added = enrich_map(fid)
        total += added
        print(f"{fid}: +{added}")
    print('total rulings added:', total)
