"""Normalise the id forms an MCP client may send (string work plus one tree.json stat for acts).

Two vocabularies exist: document ids (``leg:itaa-1997:8-1``, ``case:2025-fcafc-116``,
``ruling:tr-2024-1``, ``pbr:123``) used by fetch/related, and graph keys
(``section:itaa-1997:8-1``, ``case:[2025] FCAFC 116``, ``public_ruling:TR 2024/1``,
``nz_ruling:BRPRD_2004_1``, ``private_ruling:EV/123``) used by graph_neighbourhood/graph_path.
These helpers map any likely form to what each tool needs. Gating must run on the returned values.

Graph keys follow pipeline/graph_etl.py: ``section_key`` + ``_canon_section_id`` for sections,
``ruling_label`` for AU public rulings, ``nz_ruling:<STEM>`` for NZ rulings (load_nz_rulings) and
``regulatory_guide:<n>:<para>`` for RG paragraphs.
"""
from __future__ import annotations

import re

from backend.doc_ids import ACT_SLUGS, parse_id

_ACTS = frozenset(ACT_SLUGS)
# data/ directories with a tree.json that are not legislation (mirrors graph_etl._NON_LEGISLATION).
_NON_ACT_DIRS = frozenset({"db-snapshots", "map_reviews", "nz_rulings", "nz_rulings_pdf",
                           "nz_rulings_list", "proposed-law", "spec"})
_SEC = r"[0-9a-z][0-9a-z.\-]*"
_LEG = re.compile(rf"^leg:([a-z0-9-]+):({_SEC})(\(\d+\))?$", re.I)
_BARE = re.compile(rf"^([a-z0-9-]+):({_SEC})(?:\(\d+\))?$", re.I)
_SECTION_KEY = re.compile(rf"^section:([a-z0-9-]+):({_SEC})(?:\(\d+\))?$", re.I)
_CASE_ID = re.compile(r"^case:(\d{4})-([a-z]+)-(\d+)$", re.I)
_CASE_KEY = re.compile(r"^case:\[(\d{4})\]\s*([a-z]+)\s*(\d+)$", re.I)
_RULING_ID = re.compile(r"^ruling:([a-z0-9]+)-(\d{4})-(d?\d+)$", re.I)
_PBR_ID = re.compile(r"^pbr:(\d+)$", re.I)
_RG_ID = re.compile(r"^rg:(\d+)(?::(\d+(?:\.\d+)?[a-z]?))?$", re.I)
_PUB_KEY = re.compile(r"^public_ruling:([A-Za-z ]+?)\s*(\d{4})/(\d+)$")
_PRIV_KEY = re.compile(r"^private_ruling:EV/(\d+)$", re.I)
_GRAPH_PREFIXES = (
    "section:", "part:", "definition:", "public_ruling:", "ruling:", "nz_ruling:", "private_ruling:",
    "case:", "regulatory_guide:", "afsa_guide:", "commentary:", "treaty:", "insolvency:", "nz_mtg:",
)
# Plain ruling citations. Multi-word series come first so "PS LA 2005/24" is not read as
# "LA 2005/24" and "ATO ID 2001/1" not as "ID 2001/1". Also matches stems (TR_2024_1, BRPRD_2004_1),
# NZ display forms (BR Prd 04/01) and drafts (TD 2026/D1).
_RULING_CITATION = re.compile(
    r"(?<![A-Za-z0-9])(PS(?:\s*|_)LA|ATO(?:\s*|_)ID|BR(?:\s*|_)P(?:ub|rd)|[A-Z]{2,5})(?:\s*|_)"
    r"(\d{4}|\d{2})(?:\s*/\s*|_)(D?\d+)(?![A-Za-z0-9/])", re.I)
_IT_CITATION = re.compile(r"^IT(?:\s*|_)(\d{1,4})$", re.I)  # "IT 2621": the one series with no year
# NZ IRD series (data/nz_ruling_section_index.json, nz_rulings_list.json `type`). None is an AU series.
_NZ_SERIES = frozenset({"BRPUB", "BRPRD", "QB", "IS", "TDS", "SPS", "OS", "CS", "OP", "IG", "OG", "DET"})
_NZ_LETTERED = re.compile(r"([A-Za-z]{2})[\s_-]*(\d+[A-Za-z]*)")
# No id, graph key or citation is this long. Longer input is returned unchanged before any regex
# runs: some citation regexes backtrack quadratically and the tools run on the event loop.
_MAX_LEN = 256


def _is_act(slug: str) -> bool:
    """A legislation slug: a doc_ids act, or any data/<slug>/tree.json the graph ETL picks up."""
    s = slug.lower()
    if s in _ACTS:
        return True
    if s in _NON_ACT_DIRS or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", s):
        return False
    from backend.services.data_loader import DATA_DIR
    return (DATA_DIR / s / "tree.json").is_file()


def _canon_section(act: str, section: str) -> str:
    """Section id as the graph stores it: NZ lettered sections hyphenated (CB1 -> CB-1), letter
    suffixes upper-cased (6f -> 6F, graph_etl._canon_section_id)."""
    sec = section.strip()
    if act.lower().startswith("nz-"):
        m = _NZ_LETTERED.fullmatch(sec)
        if m:
            return f"{m[1].upper()}-{m[2].upper()}"
    return re.sub(r"[a-z]+$", lambda m: m.group(0).upper(), sec)


def _section_key(act: str, section: str) -> str:
    return f"section:{act.lower()}:{_canon_section(act, section)}"


def _ruling_candidates(series: str, year: str, n: str) -> list[str]:
    s = re.sub(r"[\s_]", "", series).upper()
    if len(year) == 2:  # 97 -> 1997, 04 -> 2004 (matches data_loader.load_rulings)
        year = f"19{year}" if int(year) >= 90 else f"20{year}"
    n = n.upper()
    n = f"D{int(n[1:])}" if n.startswith("D") else str(int(n))
    if s in _NZ_SERIES:  # graph_etl.load_nz_rulings keys NZ rulings by stem
        return [f"nz_ruling:{s}_{year}_{n}"]
    out = [f"public_ruling:{s} {year}/{n}"]
    if s.startswith("PS") and len(s) > 2:  # PSLA 2011/12 is stored as "PS LA 2011/12"
        out.append(f"public_ruling:PS {s[2:]} {year}/{n}")
    if s in ("ATOID", "AID"):  # corpus stems are AID_, ruling_label keeps ATOID_ as ATOID
        out.append(f"public_ruling:{'AID' if s == 'ATOID' else 'ATOID'} {year}/{n}")
    return out


def _plain_ruling(s: str) -> list[str] | None:
    m = _IT_CITATION.match(s)
    if m:
        return [f"public_ruling:IT {int(m[1])}"]
    m = _RULING_CITATION.search(s)
    if m:
        return _ruling_candidates(m[1], m[2], m[3])
    return None


def graph_key_candidates(raw: str) -> list[str]:
    """Graph keys to try, in order, for any accepted id form. Unrecognised input -> [raw]."""
    s = (raw or "").strip()
    if not s or len(s) > _MAX_LEN:
        return [raw]
    m = _CASE_ID.match(s)
    if m:
        return [f"case:[{m[1]}] {m[2].upper()} {m[3]}"]
    m = _CASE_KEY.match(s)
    if m:
        canon = f"case:[{m[1]}] {m[2].upper()} {m[3]}"
        return list(dict.fromkeys([s, canon, f"case:{m[1]}-{m[2].lower()}-{m[3]}"]))
    m = _LEG.match(s)
    if m:
        return [_section_key(m[1], m[2])]
    m = _RULING_ID.match(s)
    if m:
        return _ruling_candidates(m[1], m[2], m[3])
    m = _PBR_ID.match(s)
    if m:
        return [f"private_ruling:EV/{m[1]}"]
    m = _RG_ID.match(s)
    if m:  # rg:<n> -> hub; rg:<n>:<para> -> paragraph node, which the graph keys "<n>.<para>"
        if not m[2]:
            return [f"regulatory_guide:{m[1]}"]
        out = [f"regulatory_guide:{m[1]}:{m[2]}"]
        if "." not in m[2]:
            out.append(f"regulatory_guide:{m[1]}:{m[1]}.{m[2]}")
        return out
    m = _SECTION_KEY.match(s)
    if m:
        canon = _section_key(m[1], m[2])
        return list(dict.fromkeys([s, canon]))
    if s.lower().startswith(_GRAPH_PREFIXES):
        return [s]
    m = _BARE.match(s)
    if m and _is_act(m[1]):
        return [_section_key(m[1], m[2])]
    # Plain citations: "[2015] HCA 48", "TR 2024/1", "PS LA 2005/24", "s 8-1 ITAA 1997",
    # "ITAA 1997 s 8-1". The bracketless case form ("2015 HCA 48") is tried last: it also
    # matches "1997 s 8" inside an act-first section citation.
    from backend.resolve_ref import (
        _CASE_BRACKETLESS_RE, _CASE_RE, _SECTION_CITATION_RE, _normalise_act_hint,
    )
    m = _CASE_RE.search(s)
    if m:
        return [f"case:[{m[1]}] {m[2].upper()} {m[3]}"]
    r = _plain_ruling(s)
    if r:
        return r
    m = _SECTION_CITATION_RE.search(s)
    if m:
        hint = re.sub(r"^\s*(?:of\s+)?(?:the\s+)?", "", _SECTION_CITATION_RE.sub("", s, count=1), flags=re.I).strip()
        act = _normalise_act_hint(hint)
        if act:
            return [_section_key(act, m[1])]
    m = _CASE_BRACKETLESS_RE.search(s)
    if m:
        return [f"case:[{m[1]}] {m[2].upper()} {m[3]}"]
    return [s]


def related_doc_id(raw: str) -> str:
    """Map bare ``<act>:<section>`` and graph-key forms to the document id `related` parses.

    Valid document ids pass through (an NZ lettered section is hyphenated: CB1 -> CB-1);
    unrecognised input is returned unchanged.
    """
    s = (raw or "").strip()
    if len(s) > _MAX_LEN:
        return s
    m = _LEG.match(s)
    if m:
        return f"leg:{m[1].lower()}:{_canon_nz(m[1], m[2])}{m[3] or ''}"
    if parse_id(s) is not None:
        return s
    m = _SECTION_KEY.match(s)
    if m:
        return f"leg:{m[1].lower()}:{_canon_nz(m[1], m[2])}"
    m = _BARE.match(s)
    if m and _is_act(m[1]):
        return f"leg:{m[1].lower()}:{_canon_nz(m[1], m[2])}"
    m = _CASE_KEY.match(s)
    if m:
        return f"case:{m[1]}-{m[2].lower()}-{m[3]}"
    m = _PRIV_KEY.match(s)
    if m:
        return f"pbr:{m[1]}"
    m = _PUB_KEY.match(s)
    if m:
        return f"ruling:{m[1].lower().replace(' ', '')}-{m[2]}-{m[3]}"
    return s


def _canon_nz(act: str, section: str) -> str:
    """Only the NZ hyphenation: `related` lower-cases ids itself, so case is left alone."""
    if act.lower().startswith("nz-"):
        m = _NZ_LETTERED.fullmatch(section.strip())
        if m:
            return f"{m[1].upper()}-{m[2].upper()}"
    return section
