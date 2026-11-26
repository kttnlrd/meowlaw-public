from __future__ import annotations

import functools
import json
import os
import re
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from fastapi import HTTPException
from backend.config import BASE, DATA_DIR, CASE_DIR, RULING_DIR, ATO_RULING_DIR, NZ_RULING_DIR

# Case-name corpus used as a fallback for citation→name lookup. CASE_DIR (the
# ASIC/tax scrape) only covers a few hundred cases; scripts/cleaned/summaries
# carries ~8.5k case summaries with citation + case_name. Name lookup prefers
# CASE_DIR because those entries also carry the full `content`.
CASE_SUMMARIES_DIR = Path(
    os.environ.get(
        "CASE_SUMMARIES_DIR",
        str(BASE / "scripts" / "cleaned" / "summaries"),
    )
)

logger = logging.getLogger(__name__)

# Ligature normalisation — some CCH content uses typographic ligatures
# (ﬃ, ﬁ, ﬀ) that don't render on all devices
_LIGATURE_TABLE = str.maketrans({
    '\ufb00': 'ff',   # ﬀ
    '\ufb01': 'fi',   # ﬁ
    '\ufb02': 'fl',   # ﬂ
    '\ufb03': 'ffi',  # ﬃ
    '\ufb04': 'ffl',  # ﬄ
    '\ufb05': 'st',   # ﬅ
    '\ufb06': 'st',   # ﬆ
})
def _normalise_text(s: str) -> str:
    return s.translate(_LIGATURE_TABLE)


_ATO_ID_HEADER = re.compile(
    r'^(ATO\s+Interpretative\s+Decision|ATO\s+ID\s+\d{4}/\d+|={3,}|'
    r'File\s+Number|FOI\s+status|This\s+ATO\s+ID|This\s+document)',
    re.IGNORECASE
)


def _check_withdrawn(content: str) -> bool:
    """Check content for withdrawal/supersession signals.
    
    Only searches within the first 2000 chars (versus full scan) to avoid
    false positives from version-history footers mentioning "Archived" or
    "superseded" in references to other documents.
    """
    head = content[:2000]
    patterns = [
        r'\b(withdrawn|Archived|superseded|no longer current)\b',
        r'has been replaced by',
    ]
    return any(re.search(p, head, re.IGNORECASE) for p in patterns)


def _strip_ato_chrome(text: str) -> str:
    """Strip ATO web chrome boilerplate from ruling text.
    
    Handles multi-line format:
      Legal database
      Legal database
      Contents
      Download
      Email
      Print
      Back to browse
      N related documents
    """
    # Strip the chrome block at the start of the text
    text = re.sub(
        r'(?i)^(Legal\s+database\s*\n){1,2}'
        r'(Contents\s*\n)?'
        r'(Download\s*\n)?'
        r'(Email\s*\n)?'
        r'(Print\s*\n)?'
        r'(Back\s+to\s+browse\s*\n)?'
        r'(\d+\s+related\s+documents\s*\n)?',
        '', text,
    )
    # Also handle single-line format
    text = re.sub(
        r'(?i)^Legal\s+database\s*/\s*Contents\s*/\s*Download\s*/\s*Email\s*/\s*Print\s*/\s*Back\s+to\s+browse.*?\n',
        '', text,
    )
    return text.strip()


# ---------------------------------------------------------------------------
# Acts / sections
# ---------------------------------------------------------------------------

def _corpus_stamp() -> tuple:
    """Fingerprint of the corpus the act list is built from.

    A plain lru_cache here outlives a re-ingest: /api/acts kept reporting the previous
    compilation of the AML/CTF Act and omitted new instruments entirely.
    """
    stamp = []
    for act_dir in sorted(DATA_DIR.iterdir()):
        tree = act_dir / "tree.json"
        lic = act_dir / "licence.json"
        if tree.exists():
            stamp.append((act_dir.name, tree.stat().st_mtime_ns,
                          lic.stat().st_mtime_ns if lic.is_file() else 0))
    return tuple(stamp)


def load_acts_meta() -> list[dict]:
    """Act metadata for /api/acts, invalidated whenever a tree or licence record changes."""
    return _load_acts_meta_cached(_corpus_stamp())


@functools.lru_cache(maxsize=1)
def _load_acts_meta_cached(stamp: tuple = ()) -> list[dict]:
    """Lightweight act metadata for /api/acts — top-level scalars from each
    tree.json, no title normalisation and no cached full-tree copies."""
    acts = []
    for act_dir in sorted(DATA_DIR.iterdir()):
        tree_path = act_dir / "tree.json"
        if act_dir.is_dir() and tree_path.exists():
            tree = json.loads(tree_path.read_text(encoding="utf-8"))
            comp_no = tree.get("compilation_no")
            # 0 is a placeholder for 'not recorded' — surface it as unknown.
            if comp_no in (0, "0", ""):
                comp_no = None
            lic_path = act_dir / "licence.json"
            jur = None
            if lic_path.is_file():
                try:
                    jur = json.loads(lic_path.read_text(encoding="utf-8")).get("jurisdiction")
                except Exception:
                    jur = None
            acts.append({
                "id": act_dir.name,
                "name": tree.get("act", act_dir.name),
                "compilation_no": comp_no,
                "compilation_date": tree.get("compilation_date"),
                # The licence record is authoritative. Where an act has none, the id
                # prefix is the corpus convention (nz-* is New Zealand).
                "jurisdiction": (jur or ("NZ" if act_dir.name.startswith("nz-") else "AU")).upper(),
            })
    return acts


def licence_notice(source: str) -> dict | None:
    """Display notice for one source, keyed on the licence file's mtime."""
    path = DATA_DIR / source / "licence.json"
    stamp = path.stat().st_mtime_ns if path.is_file() else 0
    return _licence_notice_cached(source, stamp)


@functools.lru_cache(maxsize=None)
def _licence_notice_cached(source: str, stamp: int = 0) -> dict | None:
    """What a view must display (attribution, disclaimer, unofficial-copy / no-endorsement notice), from
    data/<source>/licence.json. <source> is an act slug, or a source-level record shared by every document
    of a type (see document_licence). None where no record exists or the record is not cleared to render."""
    path = DATA_DIR / source / "licence.json"
    if not path.is_file():
        return None
    lic = json.loads(path.read_text(encoding="utf-8"))
    if lic.get("render") is False:  # e.g. data/treaties: provenance UNVERIFIED — never show that in the product
        return None
    out = {
        "jurisdiction": lic["jurisdiction"],
        "attribution": lic.get("attribution_display"),
        "notice_badge": lic.get("notice_badge") or lic.get("jurisdiction"),
        "notice_line": lic.get("notice_line"),
        "disclaimer": lic.get("disclaimer"),
        "unofficial_notice": lic.get("unofficial_notice"),
        "endorsement_notice": lic.get("endorsement_notice"),
        "licence": {"name": lic["licence"]["name"], "url": lic["licence"]["url"]},
        "source_url": lic["source_url"],
        "fetched": lic.get("fetched"),
        "terms_url": lic["terms_url"],
    }
    if "version" in lic:  # legislation only; rulings and cases carry no version line
        out["version_date"] = lic["version"]["date"]
    return out


_COURT_RE = re.compile(r"\[\d{4}\]\s*([A-Za-z]+)\s+\d+")


def document_licence(doc_type: str, citation: str = "", source_url: str | None = None) -> dict | None:
    """The notice for a non-legislation document: ONE record per source, resolved by document type,
    never stamped per row. ``source_url`` (the document's own page) replaces the record's fallback link."""
    if doc_type == "tax-cases":
        m = _COURT_RE.search(citation or "")
        # AATA / ARTA deliberately get no notice: the Tribunal permits its decisions to be reproduced for
        # non-commercial use only, and LawKitty's authorisation request is pending. Omission is intended.
        source = {"HCA": "hca", "FCA": "fca", "FCAFC": "fca"}.get(m.group(1).upper() if m else "")
    else:
        source = {"rulings": "ato", "private-rulings": "ato",
                  "regulatory-guides": "regulatory-guides"}.get(doc_type)
    lic = licence_notice(source) if source else None
    return {**lic, "source_url": source_url} if lic and source_url else lic


_PLAIN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*\Z")


def is_plain_id(name: str) -> bool:
    """True for a bare directory-safe id ('itaa-1997', 'usa'): no '/', '..' or leading dot.

    Act ids and treaty countries arrive from URLs, query strings and MCP arguments and are
    joined onto a data directory; anything that is not a plain id must not reach the disk.
    """
    return isinstance(name, str) and bool(_PLAIN_ID_RE.match(name))


@functools.lru_cache(maxsize=None)
def load_tree(act: str) -> dict:
    path = DATA_DIR / act / "tree.json"
    if not is_plain_id(act) or not path.exists():
        raise HTTPException(status_code=404, detail=f"Act {act} not found")
    tree = json.loads(path.read_text(encoding="utf-8"))
    # Normalise section titles
    for part in tree.get("parts", []):
        for sec in part.get("sections", []):
            sec["title"] = _normalise_text(sec["title"])
        for div in part.get("divisions", []):
            for sec in div.get("sections", []):
                sec["title"] = _normalise_text(sec["title"])
            for sub in div.get("subdivisions", []):
                for sec in sub.get("sections", []):
                    sec["title"] = _normalise_text(sec["title"])
                sub["title"] = _normalise_text(sub.get("title", ""))
            div["title"] = _normalise_text(div.get("title", ""))
        part["title"] = _normalise_text(part.get("title", ""))
    return tree


@functools.lru_cache(maxsize=None)
def _normalize_term_key(key: str) -> str:
    """Normalize a definition term key for lookup: lowercase + Unicode quote normalization.
    
    PDF-extracted text commonly uses curly apostrophes (U+2019) which must
    be normalized to ASCII apostrophes (U+0027) for index matching.
    """
    return key.lower().replace("\u2018", "'").replace("\u2019", "'")


def _singularise_term(term: str) -> str:
    """Best-effort singularisation for definition lookups.

    Index keys are singular ('capital gain', 'fringe benefit'), but LLMs
    naturally ask with plurals ('capital gains'). Strip a trailing 's'
    unless it looks like an -ss/-us/-is word ('business', 'bonus', 'basis').
    """
    t = term.strip()
    if len(t) <= 3:
        return t
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith(("ss", "us", "is", "sses")):
        return t
    if t.endswith("s"):
        return t[:-1]
    return t


# Shorthand act ids accepted by the definitions API. Unknown ids pass through.
ACT_ALIASES = {
    "1936": "itaa-1936", "itaa36": "itaa-1936",
    "1997": "itaa-1997", "itaa97": "itaa-1997",
    "gst": "gst-1999", "gst1999": "gst-1999",
    "fbt": "fbt-1986", "fbtaa": "fbt-1986",
    "taa": "taa-1953",
    "sis": "sis-1993",
    "corps": "corporations-act-2001",
    "aml": "aml-ctf-2006", "amlctf": "aml-ctf-2006",
    "nz": "nz-it-2007", "nzit": "nz-it-2007",
}


def resolve_act_id(act: str) -> str:
    return ACT_ALIASES.get(act.strip().lower(), act)


def _definitions_store_path() -> Path | None:
    # Prefer the combined definitions_all.json; fall back to the act-keyed
    # definitions.json when the combined file is absent (both share the
    # {act: {section, terms}} shape).
    for name in ("definitions_all.json", "definitions.json"):
        path = DATA_DIR / name
        if path.exists():
            return path
    return None


@functools.lru_cache(maxsize=4)
def _definitions_store(path_str: str, mtime: float) -> dict:
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def _load_definitions_store() -> dict:
    path = _definitions_store_path()
    if not path:
        return {}
    return _definitions_store(str(path), path.stat().st_mtime)


def _canon_section_id(section: str) -> str:
    """Canonicalise letter-suffixed section ids ('6f' -> '6F', '6ab' -> '6AB').

    Section files on disk use lowercase stems, but tree/meta and the graph
    use uppercase suffixes; normalise so lookups and graph edges resolve to
    the canonical node (CDN-0164).
    """
    return re.sub(r"[a-z]+$", lambda m: m.group(0).upper(), section)


def load_definitions(act: str) -> dict[str, dict]:
    act = resolve_act_id(act)
    act_data = _load_definitions_store().get(act, {})
    terms = act_data.get("terms", {})
    out = {}
    for term, info in terms.items():
        norm = {**info}
        if norm.get("section"):
            norm["section"] = _canon_section_id(norm["section"])
        out[_normalize_term_key(term)] = norm
    return out


def _all_definition_acts() -> list[str]:
    """Acts that carry a definitions index, derived from the definitions store."""
    try:
        store = _load_definitions_store()
        if store:
            return list(store.keys())
    except Exception:
        pass
    return ["itaa-1997", "itaa-1936", "gst-1999"]


# ---------------------------------------------------------------------------
# Citations (cases / rulings per section)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _load_citation_index() -> dict[str, dict[str, list[dict]]]:
    path = DATA_DIR / "citation_index.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _get_embedding_db():
    """Get a connection to the embeddings database."""
    db_path = DATA_DIR / "embeddings.db"
    if db_path.exists():
        return sqlite3.connect(str(db_path))
    return None


_COURT_MAP = {
    'high court of australia': 'HCA',
    'federal court of australia (full court)': 'FCAFC',
    'federal court of australia - full court': 'FCAFC',
    'full court of the federal court of australia': 'FCAFC',
    'federal court of australia': 'FCA',
    'administrative appeals tribunal': 'AATA',
    'administrative appeals tribunal of australia': 'AATA',
    'administrative review tribunal': 'ARTA',
    'administrative review tribunal (general division)': 'ARTA',
    'administrative review tribunal - general division': 'ARTA',
}

def _map_court(act_name: str, fallback_id: str = "") -> str:
    """Map a full court name to a short code.
    
    Args:
        act_name: The court name from the embeddings 'act' field.
        fallback_id: The source ID (e.g. austlii citation), used to infer
                     court when act_name is 'unknown'.
    """
    key = act_name.lower().strip()
    # Exact match first
    if key in _COURT_MAP:
        return _COURT_MAP[key]
    # Strip parentheticals and try again
    stripped = re.sub(r'[\(\)\-]', '', key).strip()
    if stripped in _COURT_MAP:
        return _COURT_MAP[stripped]
    # Fuzzy: match if all significant words appear
    for pattern, code in _COURT_MAP.items():
        pwords = set(pattern.split())
        kwords = set(key.split())
        if pwords and pwords.issubset(kwords):
            return code
        # Try stripped version
        stripped_pwords = {w.strip('()') for w in pwords}
        stripped_kwords = {w.strip('()') for w in kwords}
        if stripped_pwords and stripped_pwords.issubset(stripped_kwords):
            return code
    # Fallback: try to infer from the citation format
    if fallback_id:
        parts = fallback_id.split('_')
        if len(parts) >= 2:
            court_code = parts[1]
            valid_codes = {'HCA', 'FCAFC', 'FCA', 'AATA', 'ARTA'}
            if court_code in valid_codes:
                return court_code
    return "Other"


def _find_similar_via_embeddings(act: str, section: str, target_type: str, limit: int = 10) -> list[dict]:
    """Find related cases or rulings via embeddings similarity index."""
    db = _get_embedding_db()
    if db is None:
        return []
    
    try:
        # Find section embedding IDs
        sec_ids = db.execute(
            "SELECT id FROM embeddings WHERE act = ? AND section = ? AND source_type = 'section'",
            (act, section)
        ).fetchall()
        
        if not sec_ids:
            return []
        
        ids = [r[0] for r in sec_ids]
        placeholders = ",".join("?" * len(ids))
        
        # Query similarity_index for cross-type neighbors
        rows = db.execute(f"""
            SELECT DISTINCT e.act, e.section, e.section_title, MAX(s.similarity) as max_sim
            FROM similarity_index s
            JOIN embeddings e ON s.neighbor_id = e.id
            WHERE s.embedding_id IN ({placeholders})
            AND e.source_type = ?
            AND e.section IS NOT NULL
            GROUP BY e.section, e.act
            ORDER BY max_sim DESC
            LIMIT ?
        """, ids + [target_type, limit]).fetchall()
        
        results = []
        for r in rows:
            item = {"type": target_type}
            if target_type == "case":
                # For cases: section = austlii citation, section_title = case name
                item["citation"] = r[1]  # e.g., "2023_AATA_3074"
                item["title"] = r[2] or r[1]  # case name
                item["court"] = _map_court(r[0], r[1])  # court code from the act field
            else:
                # For rulings: section = ruling ID, section_title = ruling description
                item["citation"] = r[1]  # e.g., "AID_2011_104"
                item["title"] = r[2] or r[1]  # ruling title
                item["year"] = 0
                item["ato_url"] = ""
            results.append(item)
        return results
    finally:
        db.close()


def _apply_case_names(cases: list[dict]) -> list[dict]:
    """Replace citation-shaped titles with party names in place (CDN-0205).

    The embeddings/smartlink indexes key cases by citation, so both paths used
    to hand the frontend `title == citation`. The name index (CASE_DIR then
    scripts/cleaned/summaries, ~8.5k cases) is the same one the MCP path uses.
    """
    if not cases:
        return cases
    index = load_case_name_index()
    for c in cases:
        cit = c.get("citation") or ""
        entry = index.get(cit) or index.get(cit.replace("_", " "))
        if not entry or not entry.get("title"):
            continue
        c["title"] = entry["title"]
        if entry.get("short_name"):
            c["short_name"] = entry["short_name"]
        if entry.get("court") and not c.get("court"):
            c["court"] = entry["court"]
    return cases


def get_cases_for_section(act: str, section: str, limit: int = 50, offset: int = 0) -> list[dict]:
    # Primary: embeddings similarity index (vector-based, highest quality)
    cases = _find_similar_via_embeddings(act, section, "case", limit)
    
    # Fallback 1: citation index
    if not cases:
        act_data = _load_citation_index().get(act, {})
        entries = act_data.get(section, [])
        cases = [e for e in entries if e.get("type") == "case"]
        cases = [c for c in cases if classify_case(c.get("title", "")) == "tax"]
    
    # Fallback 2: smartlink index (lowest quality, generic placeholder entries)
    if not cases:
        smartlinks = get_smartlinks_for_item("section", f"{act}#{section}")
        case_links = [s for s in smartlinks if s.get("type") == "case"]
        for cl in case_links:
            case_id = cl.get("id", "")
            cases.append({"type": "case", "title": case_id, "citation": case_id})
    
    # CDN-0205: every source above yields citations, so name the rows from the
    # wide citation→name index before serving them.
    _apply_case_names(cases)
    
    end = offset + min(limit, 100)
    return cases[offset:end]


def get_rulings_for_section(act: str, section: str, limit: int = 50, offset: int = 0) -> list[dict]:
    # Primary: embeddings similarity index (vector-based, highest quality)
    rulings = _find_similar_via_embeddings(act, section, "ruling", limit)
    
    # Fallback 1: citation index
    if not rulings:
        act_data = _load_citation_index().get(act, {})
        entries = act_data.get(section, [])
        rulings = [e for e in entries if e.get("type") == "ruling"]
    
    # Fallback 2: smartlink index (lowest quality, generic placeholder entries)
    if not rulings:
        smartlinks = get_smartlinks_for_item("section", f"{act}#{section}")
        ruling_links = [s for s in smartlinks if s.get("type") == "ruling"]
        for rl in ruling_links:
            ruling_id = rl.get("id", "")
            rulings.append({"type": "ruling", "title": ruling_id, "citation": ruling_id})
    
    end = offset + min(limit, 100)
    return rulings[offset:end]


# ---------------------------------------------------------------------------
# Smart links
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _load_smartlink_index() -> dict[str, Any]:
    path = DATA_DIR / "smartlink_index.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    logger.warning("smartlink_index.json not found")
    return {}


def get_smartlinks_for_item(item_type: str, item_id: str) -> list[dict]:
    index = _load_smartlink_index()
    if item_type == "section":
        try:
            act_code, section_id = item_id.split("#")
            return index.get("sections", {}).get(act_code, {}).get(section_id, [])
        except ValueError:
            logger.error("Invalid section item_id format: %s", item_id)
            return []
    elif item_type == "case":
        return index.get("cases", {}).get(item_id, [])
    elif item_type == "ruling":
        return index.get("rulings", {}).get(item_id, [])
    elif item_type == "part":
        try:
            act_code, part_id = item_id.split("#")
            return index.get("parts", {}).get(act_code, {}).get(part_id, [])
        except ValueError:
            logger.error("Invalid part item_id format: %s", item_id)
            return []
    return []


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

TAX_KEYWORDS = ["commissioner of taxation", "federal commissioner of taxation",
                "deputy commissioner of taxation", r"\btax\b", "income tax", "gst"]
ASIC_KEYWORDS = ["australian securities and investments commission",
                 "australian securities commission", r"\basic\b"]


def classify_case(case_name: str) -> str:
    name = case_name.lower()
    is_tax = any(re.search(p, name) for p in TAX_KEYWORDS)
    is_asic = any(re.search(p, name) for p in ASIC_KEYWORDS)
    if is_tax and is_asic:
        return "asic"
    if is_tax:
        return "tax"
    if is_asic:
        return "asic"
    return "other"


_GOV_PARTY_KEYS = ("commissioner", "commission", "asic", "australian securities",
                   "director", "attorney-general", "minister", "administrator")
# 'Foo Pty Ltd and Commissioner of Taxation (Taxation)' — the AAT/ART conjoiner,
# as opposed to the ' v ' form the courts use.
_GOV_CONJOINER_RE = re.compile(
    r"\s+and\s+(?=(?:the\s+)?(?:Federal\s+|Deputy\s+)?Commissioner\b|Australian Securities\b)",
    re.IGNORECASE,
)


def _pick_case_party(left: str, right: str) -> str:
    """Return the non-government side of a case name (the taxpayer/company)."""
    left_gov = any(k in left.lower() for k in _GOV_PARTY_KEYS)
    right_gov = any(k in right.lower() for k in _GOV_PARTY_KEYS)
    if left_gov and not right_gov:
        return right
    if right_gov and not left_gov:
        return left
    return left if len(left) < len(right) else right


def short_case_name(case_name: str) -> str:
    name = case_name.removeprefix("Re ")
    parts = re.split(r'\s+[vV]\s+', name, maxsplit=1)
    if len(parts) == 2:
        candidate = _pick_case_party(parts[0], parts[1])
    else:
        # CDN-0205: 574 AAT/ART names read 'X and Commissioner of Taxation
        # (Taxation)'. Only splitting on ' v ' left the whole string, and the
        # tail rule below then returned the jurisdiction word 'Taxation' as the
        # case's short name. Split on ' and <government party>' and take the
        # taxpayer side, exactly as the ' v ' branch does.
        m = _GOV_CONJOINER_RE.split(name, maxsplit=1)
        candidate = _pick_case_party(m[0], m[1]) if len(m) == 2 else name
    candidate = re.split(r'[;,]\s+(In the Matter of|in the matter of|Receiver)', candidate)[0]
    candidate = re.split(r'\s+\(', candidate)[0]
    candidate = candidate.strip()
    company_indicators = ['pty ltd', 'ltd', 'limited', 'inc', 'corp', 'corporation',
                          'llc', 'plc', 'group', 'holdings', 'trustee', 'trust',
                          'superannuation', 'nominees']
    is_company = any(ind in candidate.lower() for ind in company_indicators)
    words = candidate.split()
    if not is_company and len(words) >= 2:
        return words[-1]
    return candidate


@functools.lru_cache(maxsize=None)
def load_cases() -> list[dict]:
    cases = []
    for f in sorted(CASE_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            case_name = data.get("case_name", "Unknown")
            cases.append({
                "citation": data.get("citation", f.stem),
                "title": case_name,
                "short_name": short_case_name(case_name),
                "category": classify_case(case_name),
                "court": data.get("court", ""),
                "year": data.get("year", 0),
                "date": data.get("decision_date", ""),
                "source_url": data.get("source_url", ""),
            })
        except Exception:
            logger.exception("Error loading case %s", f.name)
    return cases


@functools.lru_cache(maxsize=None)
def load_case_name_index() -> dict[str, dict]:
    """citation → case-name metadata, CASE_DIR first then cleaned summaries.

    CASE_DIR (the ASIC/tax scrape) carries only a few hundred cases and the
    full `content`; scripts/cleaned/summaries carries ~8.5k case summaries
    (citation, case_name/title, court, decision_date). CASE_DIR wins when a
    citation appears in both. This is a NAME-lookup index only — the
    /api/cases listing keeps using load_cases() (CASE_DIR only) so it does not
    silently grow from ~525 to ~8,500 cases.
    """
    index: dict[str, dict] = {}
    for c in load_cases():
        cit = c.get("citation")
        if cit:
            index[cit] = c
    if not CASE_SUMMARIES_DIR.is_dir():
        return index
    for f in sorted(CASE_SUMMARIES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        citation = data.get("citation") or _citation_from_case_stem(f.stem)
        if not citation or citation in index:
            continue
        name = data.get("case_name") or data.get("title") or ""
        if not name:
            continue
        index[citation] = {
            "citation": citation,
            "title": name,
            "short_name": short_case_name(name),
            "category": classify_case(name),
            "court": data.get("court", ""),
            "year": _year_from_citation(citation),
            "date": data.get("decision_date", "") or data.get("date", ""),
            "source_url": "",
        }
    return index


def _citation_from_case_stem(stem: str) -> str:
    """'1905_HCA_57' → '[1905] HCA 57' (summary filenames drop brackets/spaces)."""
    m = re.match(r'^(\d{4})_([A-Za-z]+)_(\d+)$', stem)
    if m:
        return f"[{m.group(1)}] {m.group(2).upper()} {m.group(3)}"
    return ""


def _year_from_citation(citation: str) -> int:
    m = re.search(r'(\d{4})', citation or "")
    return int(m.group(1)) if m else 0


_MONTH_NAME_RE = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)"
)


def _year_from_ruling_text(text: str) -> int:
    """Recover a ruling's issue year from its body for year-less citations.

    LK-11: the IT series (IT 100, IT 235 …) is sequential and carries no year in
    its citation, so `load_rulings()` left ~234 IT rulings with `year = 0`. Their
    issue year lives in the closing reference block ("Date original memo
    issued") or the Commissioner's signature. Returns 0 when nothing is found.
    """
    if not text:
        return 0

    def _norm(y: int) -> int:
        return y + 1900 if y < 100 else y

    # "Date original memo issued:\n29.05.75" (IT rulings' reference footer).
    m = re.search(
        r'Date original memo issued:\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})',
        text, re.IGNORECASE,
    )
    if m:
        return _norm(int(m.group(3)))
    # "Date original memo issued: Sept 82"
    m = re.search(
        r'Date original memo issued:\s*([A-Za-z]{3,9})\.?\s+(\d{2,4})',
        text, re.IGNORECASE,
    )
    if m:
        return _norm(int(m.group(2)))
    # Labelled month-name dates.
    for label in (r'Date of Issue', r'Date of Effect'):
        m = re.search(
            label + r':\s*\d{1,2}\s+' + _MONTH_NAME_RE + r'\s+(\d{4})',
            text, re.IGNORECASE,
        )
        if m:
            return int(m.group(1))
    # Signature line: "Commissioner of Taxation\n31 January 2024".
    m = re.search(
        r'Commissioner of Taxation\s*\n?\s*\d{1,2}\s+' + _MONTH_NAME_RE + r'\s+(\d{4})',
        text, re.IGNORECASE,
    )
    if m:
        return int(m.group(1))
    return 0


# ---------------------------------------------------------------------------
# Rulings
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _summary_title(stem: str) -> str | None:
    """Clean descriptive title from the ruling's summaries/<stem>.json if present."""
    import re as _re
    for cand in (stem, stem.replace("ATOID_", "AID_", 1)):
        try:
            p = RULING_DIR / "summaries" / f"{cand}.json"
            if not p.exists():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            t = d.get("title")
            if t and len(t) < 800:
                # Strip leading citation prefix if the summary title embeds it
                # e.g. "AID 2001/302 — Superannuation..." -> "Superannuation..."
                t = _re.sub(r"^[A-Z]{2,6} \d{4}/D?\d+\s*[—\-–]?\s*", "", t).strip()
                return t or None
        except Exception:
            pass
    return None


def _norm_citation(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def validate_ruling_meta(stem: str, meta: dict | None) -> None:
    """Fail-fast gate: a ruling's sidecar meta must agree with its filename.

    Draft files (TD_2026_D1.txt) carry sidecar meta with citation "TD 2026/D1",
    type "TD", draft true, status "exposure". Any disagreement (citation,
    status, type, year, draft flag) raises ValueError so a badly-fetched ruling
    is never silently ingested. Raises only when meta actually disagrees with
    the filename — files without sidecar meta are skipped.
    """
    if not meta:
        return
    m = re.match(r"^([A-Za-z]+)_(\d{2,4})_(D?\d+)$", stem)
    if not m:
        return
    file_type = m.group(1).upper()
    file_year = m.group(2)
    num = m.group(3)
    file_is_draft = num.startswith("D")
    meta_citation = str(meta.get("citation") or "").strip()
    meta_type = str(meta.get("type") or meta.get("ruling_type") or "").strip()
    meta_draft = bool(meta.get("draft")) or "/D" in meta_citation.upper()
    meta_status = str(meta.get("status") or "").lower()
    meta_year = meta.get("year")
    expect_citation = f"{file_type} {file_year}/{'D' if file_is_draft else ''}{num.lstrip('D')}"
    problems = []
    if meta_citation and _norm_citation(meta_citation) != _norm_citation(expect_citation):
        problems.append(f"citation {meta_citation!r} != {expect_citation!r}")
    if meta_type:
        mt, ft = _norm_citation(meta_type), _norm_citation(file_type)
        # AID/ATOID are equivalent display spellings of the same series
        if mt != ft and not (mt in {"AID", "ATOID"} and ft in {"AID", "ATOID"}):
            problems.append(f"type {meta_type!r} != {file_type!r}")
    if meta_draft != file_is_draft:
        problems.append(f"draft={meta_draft} but filename {'is' if file_is_draft else 'is NOT'} a draft")
    if file_is_draft and meta_status and meta_status not in ("exposure", "draft", "withdrawn"):
        problems.append(f"draft status {meta_status!r} not exposure/draft")
    if meta_year not in (None, "") and str(meta_year) != file_year:
        problems.append(f"year {meta_year!r} != {file_year!r}")
    if problems:
        raise ValueError(f"Ruling meta/filename mismatch for {stem}: " + "; ".join(problems))


# ── Ruling title extraction (CDN-0204) ───────────────────────────────────────
# ATO PDF text wraps long ruling titles over 2-4 lines. Lines matching the
# patterns below are page furniture, not title text.
_RULING_TITLE_SKIP_RE = re.compile(
    r'^(Keywords|Date of decision|SUBJECT|PURPOSE|Paragraph|FOI status|Issue|Decision|'
    r'Facts|CAUTION|Download|Email|Print|Back to browse|Contents|Please|PDF|'
    r'This ATO ID|This document|ATO Interpretative Decision|File Number|={3,})',
    re.IGNORECASE,
)
_RULING_TITLE_STOP_RE = re.compile(
    r'^(This cover sheet|There is a Compendium|Generated on|Status:|Page \d+ of|'
    r'Taxation Ruling|Taxation Determination|Relying on this|Table of Contents|'
    r'©|Australian Taxation Office|For the purposes|ISSN:)',
    re.IGNORECASE,
)
_RULING_CITE_LINE_RE = re.compile(
    r'^([A-Z]+ \d{2,4}/\d+|ATO ID \d{4}/\d+|PS LA \d{4}/\d+|\w{2,4} \d{4}/\d+)'
)
# Status markers that ride on a withdrawal/erratum citation line and are not
# title text, e.g. 'GSTR 2000/20W - Withdrawal'.
_RULING_TAIL_STATUS_RE = re.compile(
    r'^(Withdrawal|Withdrawn|Notice of Withdrawal|Erratum|Addendum|Corrigendum|'
    r'Draft|Final|Notice)s?\.?$',
    re.IGNORECASE,
)


def _title_norm(s: str) -> str:
    """Lowercase + strip all punctuation/whitespace runs — for title comparison."""
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _is_title_truncation(cand: str, authoritative: str) -> bool:
    """True when `cand` is provably an incomplete *prefix* of `authoritative`.

    CDN-0204: wrapped ATO titles drop their tail, so an incomplete extraction is
    a normalised prefix of the full title (e.g. '...subsection 38-190(1) of').
    A substring that is not a prefix (e.g. the full title minus a publisher
    prefix) is NOT treated as a truncation, so the extractor is not overridden
    by a differently-worded authoritative title.
    """
    c, a = _title_norm(cand), _title_norm(authoritative)
    if not c or not a:
        return False
    return c != a and a.startswith(c)

# CDN-0216: the shapes that mean a title string was cut out of a wrapped title or a
# body line rather than being a whole title. Mirrors title_fragment_reason() in
# scripts/scan_corpus_error_classes.py (the C19 detector) so extractor and detector agree.
_TITLE_CONNECTOR_END_RE = re.compile(
    r'\b(?:the|of|and|to|for|in|on|with|by|as|at|is|are|was|were|a|an|or|from|that|which)\s*$',
    re.IGNORECASE,
)


def _reads_as_fragment(title: str | None) -> bool:
    """True when `title` has the shape of an extracted fragment (CDN-0216)."""
    t = (title or "").strip()
    if not t:
        return True
    if t[:1].isalpha() and t[:1].islower():
        return True
    if re.match(r'^[\(\d]', t):
        return True
    return bool(_TITLE_CONNECTOR_END_RE.search(t))


def _is_fragment_title(cand: str, authoritative: str) -> bool:
    """`cand` reads as a fragment and is not simply the authoritative title itself.

    A title that starts lower case or with a digit is usually a cut body line, but it is
    also a legitimate company/brand title ('rhipe Limited', '1300 Smiles Limited') — those
    are not fragments, because the curated title agrees with them exactly.
    """
    if _title_norm(cand) == _title_norm(authoritative):
        return False
    return _reads_as_fragment(cand) and not _reads_as_fragment(authoritative)

def _strip_leading_citation(title: str | None) -> str | None:
    """Drop a leading citation prefix baked into a clean title.

    Some meta/summary titles embed the citation, e.g.
    'LCR 2026/1 - Payday Super: ...' → 'Payday Super: ...',
    'TD 94/82 - Income tax: ...' → 'Income tax: ...'.
    """
    if not title:
        return title
    t = re.sub(r"^[A-Z]{2,6} \d{2,4}/D?\d+\s*[—\-–]?\s*", "", title).strip()
    return t or title


_CHROME_IN_TITLE_RE = re.compile(
    r'\s(?:This cover sheet|There is a Compendium|Generated on|Status:|'
    r'Taxation Ruling|Taxation Determination|Relying on this|Table of Contents)\b',
    re.IGNORECASE,
)


def _trim_title_at_chrome(s: str) -> str:
    """Cut a title where it ran into ATO cover-sheet chrome mid-line.

    e.g. "...under an eligible policy? This cover sheet is provided for
    information only. ..." → "...under an eligible policy?".
    """
    m = _CHROME_IN_TITLE_RE.search(s or "")
    return (s[:m.start()].rstrip() if m else s).strip()


def _plausible_authoritative(title: str | None, stem: str) -> str | None:
    """Reject 'clean' titles that are really just the citation or too short.

    Some meta sidecars carry the citation as their title (e.g. 'PCG 2021/1');
    those must not override a real content-derived title.
    """
    if not title:
        return None
    if len(title.strip()) < 8:
        return None
    n = re.sub(r'[^a-z0-9]', '', title.lower())
    s = re.sub(r'[^a-z0-9]', '', (stem or '').lower())
    if n == s:
        return None
    return title


def _best_authoritative(meta_title: str | None, summary_title: str | None) -> str | None:
    """Pick the more complete of the two clean titles (meta sidecar, summary).

    meta.json titles are occasionally truncated mid-title (e.g. some GSTR
    rulings end '... subsection 38-190(1) of') while summaries/<stem>.json
    carries the full text; when one is a normalised prefix/substring of the
    other, prefer the longer one. Unrelated titles prefer the validated meta.
    """
    if not meta_title:
        return summary_title
    if not summary_title:
        return meta_title
    nm, ns = _title_norm(meta_title), _title_norm(summary_title)
    if nm and nm in ns:
        return summary_title
    if ns and ns in nm:
        return meta_title
    return meta_title


def _wrapped_title(lines: list[str], cite_idx: int, tail: str | None) -> str:
    """Extract a ruling's descriptive title from the lines after its citation.

    ATO PDF text wraps a long title across lines only when the title *starts*
    on the citation line (e.g. 'TR 2023/2 - Income tax: application of
    paragraph' / '8-1(2)(a) of the ...'). In that case the citation-line tail is
    joined with the following wrapped lines until a blank line, cover-sheet
    chrome, the next citation line or boilerplate. A bare citation line (e.g.
    'GSTR 2000/1W') carries the title on a single following line, so only that
    line is taken — joining further would run into body text.
    """
    if not tail:
        for j in range(cite_idx + 1, min(cite_idx + 8, len(lines))):
            nxt = lines[j].strip()
            if not nxt:
                continue
            # Skip single-word category headers (e.g. 'Withdrawal', 'Excise').
            if (re.match(r'^[A-Z][a-z]+$', nxt) or _RULING_TITLE_STOP_RE.match(nxt)
                    or _RULING_TITLE_SKIP_RE.match(nxt) or _RULING_CITE_LINE_RE.match(nxt)):
                continue
            return _trim_title_at_chrome(nxt)
        return ''
    parts = [tail.strip()]
    for j in range(cite_idx + 1, min(cite_idx + 8, len(lines))):
        nxt = lines[j].strip()
        if not nxt:
            break
        if (_RULING_TITLE_STOP_RE.match(nxt) or _RULING_TITLE_SKIP_RE.match(nxt)
                or _RULING_CITE_LINE_RE.match(nxt)):
            break
        parts.append(nxt)
    return _trim_title_at_chrome(' '.join(parts).strip())


@functools.lru_cache(maxsize=None)
def load_rulings() -> list[dict]:
    rulings = []

    # --- New Zealand: IRD Tax Technical -------------------------------------
    # Files live in their own directory as <TYPE>_<YEAR>_<N>.txt and are tagged with an NZ
    # jurisdiction so they never satisfy an AU search scope. Titles, citations, issue
    # dates and source URLs come from the nz_rulings_list.json manifest that
    # scripts/nz_scrape_rulings.py writes; the filename is only a fallback.
    nz_meta: dict[str, dict] = {}
    _nz_manifest = NZ_RULING_DIR / "nz_rulings_list.json"
    if _nz_manifest.exists():
        try:
            for _m in json.loads(_nz_manifest.read_text(encoding="utf-8")):
                _src = _m.get("source") or ""
                if _src:
                    nz_meta[Path(_src).stem] = _m
        except Exception:
            logger.exception("Error reading NZ ruling manifest")

    if NZ_RULING_DIR.exists():
        for f in sorted(NZ_RULING_DIR.glob("*.txt")):
            try:
                mm = re.match(r'^([A-Za-z]+)_(\d{2,4})_(\d+)', f.stem)
                if not mm:
                    continue
                nz_year = int(mm.group(2))
                if nz_year < 100:
                    nz_year += 1900 if nz_year >= 90 else 2000
                meta = nz_meta.get(f.stem, {})
                content = f.read_text(encoding="utf-8")
                rulings.append({
                    "citation": meta.get("citation") or f.stem,
                    "title": f.stem,
                    "full_title": meta.get("full_title") or "",
                    "type": mm.group(1).upper(),
                    "year": nz_year,
                    "withdrawn": bool(meta.get("withdrawn")) or _check_withdrawn(content),
                    "source": str(f),
                    "preview": meta.get("preview") or _strip_ato_chrome(content[:500]),
                    "jurisdiction": "NZ",
                    "collection": "nz-rulings",
                    "issued": meta.get("issued"),
                    "url": meta.get("url"),
                })
            except Exception:
                logger.exception("Error loading NZ ruling %s", f.name)

    for f in sorted(RULING_DIR.glob("*.txt")):
        if f.name.endswith(".meta.json"):
            continue
        try:
            meta_path = f.with_suffix(f.suffix + ".meta.json")
            if not meta_path.exists():
                meta_path = f.parent / (f.stem + ".txt.meta.json")
            title = f.stem
            year = 0
            ruling_type = "LCG"
            meta_title = None
            m_psla = re.match(r'^PS_?LA_(\d{2,4})_(?:D)?(\d+)', f.stem, re.IGNORECASE)
            m = re.match(r'^([A-Za-z]+)_(\d{2,4})_(\d+)', f.stem)
            if not m:
                # Draft citations: TD_2026_D1 → type TD, year 2026
                m = re.match(r'^([A-Za-z]+)_(\d{2,4})_D(\d+)', f.stem)
            if m_psla:
                # PS LA 1998/1 → filename PS_LA_1998_1: a two-token type prefix
                # the generic regex cannot match, so these fell through to the
                # 'LCG' default with year 0 (LK-11).
                ruling_type = "PS LA"
                year = int(m_psla.group(1))
                if year < 100:
                    year += 1900 if year >= 90 else 2000
            elif m:
                ruling_type = m.group(1).upper()
                # Normalize PSLA → PS LA for display consistency
                if ruling_type == "PSLA":
                    ruling_type = "PS LA"
                # Normalize AID → ATOID for display
                if ruling_type == "AID":
                    ruling_type = "ATOID"
                year = int(m.group(2))
                # Normalise 2-digit years (98 → 1998, 04 → 2004)
                if year < 100:
                    year += 1900 if year >= 90 else 2000
            else:
                # Single-number format: IT_262, SGR_2006_1, etc.
                m2 = re.match(r'^([A-Za-z]+)_(\d+)$', f.stem)
                if m2:
                    ruling_type = m2.group(1).upper()
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                validate_ruling_meta(f.stem, meta)
                title = meta.get("title", title)
                meta_title = _plausible_authoritative(
                    _strip_leading_citation(meta.get("title") or None), f.stem
                )
                ruling_type = meta.get("ruling_type") or meta.get("type") or ruling_type
                if meta.get("year"):
                    year = int(meta["year"])
                if meta.get("issue_date"):
                    dm = re.search(r'(\d{4})', str(meta.get("issue_date")))
                    if dm:
                        year = int(dm.group(1))
            content = f.read_text(encoding="utf-8")
            if year == 0:
                # LK-11: sequential-series citations (IT 100) carry no year and
                # no sidecar meta; recover it from the document body so
                # list_rulings has no `no_year` bucket.
                year = _year_from_ruling_text(content)
            # Extract descriptive title from content (line after the ruling citation)
            full_title = title
            # CDN-0123: prefer clean title from summaries/<stem>.json when available
            summary_title = _plausible_authoritative(
                _strip_leading_citation(_summary_title(f.stem)), f.stem
            )
            # CDN-0204: authoritative clean titles — the more complete of the
            # sidecar meta and summaries/<stem>.json titles.
            authoritative_title = _best_authoritative(meta_title, summary_title)
            # Strip ATO ID header lines before extracting title
            content_for_title = content
            if ruling_type == "PS LA" or ruling_type == "ATOID":
                # Remove known header lines
                ct_lines = content.splitlines()
                for ci, cl in enumerate(ct_lines):
                    if re.match(r'^(ATO\s+Interpretative\s+Decision|=+)$', cl.strip(), re.IGNORECASE):
                        continue
                    if re.match(r'^(File\s+Number|FOI\s+status)', cl.strip(), re.IGNORECASE):
                        continue
                    if not cl.strip():
                        continue
                    content_for_title = '\n'.join(ct_lines[ci:])
                    break
            lines = content_for_title.splitlines()
            for i, ln in enumerate(lines):
                ln = ln.strip()
                # Find the citation line, take the next non-empty line as the title
                _citation_re = r'^[A-Z]+ \d{4}/\d+'  # TR 2020/1, TD 1994/82, etc.
                _citation_2yr_re = r'^[A-Z]+ \d{2}/\d+'  # TD 94/82 (2-digit year)
                _citation_sequential_re = r'^[A-Z]+ \d+ -'  # IT 2346 - (sequential no year)
                _citation_ato_id_re = r'^ATO ID \d{4}/\d+'
                _citation_psla_re = r'^PS LA \d{4}/\d+'
                if re.match(_citation_re, ln) or re.match(_citation_2yr_re, ln) or re.match(_citation_sequential_re, ln) or re.match(_citation_ato_id_re, ln) or re.match(_citation_psla_re, ln) or re.match(r'^\w{2,4} \d{4}/\d+', ln):
                    # If the citation and title are on the same line (e.g. "IT 2346 - Income tax..."),
                    # extract the title directly from the citation line
                    title_from_citation = None
                    if ' - ' in ln:
                        # A huge 'tail' means the whole document is one line and
                        # this is body text, not a title fragment.
                        _tail = ln.split(' - ', 1)[1].strip()
                        if len(_tail) <= 200 and not _RULING_TAIL_STATUS_RE.match(_tail):
                            title_from_citation = _tail
                    cand = _wrapped_title(lines, i, title_from_citation)
                    # CDN-0123 (kept): a single-line file means there is no next line and
                    # title_from_citation can be a body fragment. CDN-0204: wrapped ATO
                    # titles make the old single-line candidate a mid-title fragment.
                    # Use the authoritative meta/summary title whenever the candidate is
                    # provably a truncation of it (or a runaway body fragment); otherwise
                    # keep the content-derived candidate (meta/summary titles can be bad).
                    if not cand:
                        full_title = authoritative_title or title
                    elif authoritative_title and (
                        len(cand) > 200
                        or _is_title_truncation(cand, authoritative_title)  # cand is truncated
                        or _is_fragment_title(cand, authoritative_title)    # CDN-0216
                    ):
                        full_title = authoritative_title
                    else:
                        full_title = cand
                    break
            withdrawn = _check_withdrawn(content)
            rulings.append({
                "citation": f.stem.replace("AID_", "ATOID_", 1) if f.stem.startswith("AID_") else f.stem,
                "title": title,
                "full_title": full_title,
                "type": ruling_type,
                "year": year,
                "withdrawn": withdrawn,
                "source": str(f),
                "preview": _strip_ato_chrome(content[:500]),
            })
        except Exception:
            logger.exception("Error loading ruling %s", f.name)
    for subdir in ["td", "tr", "pcg", "ps_la"]:
        p = ATO_RULING_DIR / subdir
        if not p.exists():
            continue
        for f in sorted(p.glob("*.txt")):
            try:
                content = f.read_text(encoding="utf-8")
                lines = content.splitlines()
                title = lines[0].strip() if lines else f.stem
                # Extract descriptive title: find the citation line, take next non-empty meaningful line
                full_title = title
                for i, ln in enumerate(lines):
                    ln = ln.strip()
                    _citation_re = r'^[A-Z]+ \d{4}/\d+'
                    _citation_2yr_re = r'^[A-Z]+ \d{2}/\d+'
                    if re.match(_citation_re, ln) or re.match(_citation_2yr_re, ln) or re.match(r'^\w{2,4} \d{4}/\d+', ln):
                        # If the citation and title are on the same line, extract from the citation line
                        title_from_citation = None
                        if ' - ' in ln:
                            title_from_citation = ln.split(' - ', 1)[1].strip()
                        for j in range(i + 1, min(i + 5, len(lines))):
                            next_ln = lines[j].strip()
                            if next_ln and not next_ln.startswith("Please") and not next_ln.startswith("PDF"):
                                full_title = next_ln
                                break
                        if full_title == title and title_from_citation:
                            full_title = title_from_citation
                        break
                year_match = re.search(r'(\d{4})', f.stem)
                year = int(year_match.group(1)) if year_match else 0
                rulings.append({
                    "citation": f.stem,
                    "title": title,
                    "full_title": full_title,
                    "type": subdir.upper().replace('_', ' '),
                    "year": year,
                    "withdrawn": _check_withdrawn(content),
                    "source": str(f),
                    "preview": _strip_ato_chrome(content[:500]),
                })
            except Exception:
                logger.exception("Error loading ATO ruling %s", f.name)

    # ── URL generators ────────────────────────────────────────────────────────
    _ato_doc_map = {
        "TR": "TXR",
        "TD": "TXD",
        "PCG": "COG",
        "LCG": "COG",
        "LCR": "COG",
        "PS LA": "ATOPSLA",
        "PS_LA": "ATOPSLA",
        "PSLA": "ATOPSLA",
        "GSTR": "GST",
        "MT": "MXR",
        "TA": "TPA",
        "SGR": "SGR",
        "AID": "AID",
        "ATOID": "AID",
        "CR": "CLR",
        "PR": "PRR",
    }

    def _ato_url(rtype: str, prefix: str, year: int | None, num: str) -> str | None:
        """Generate ATO URL — document viewer with PiT parameter.

        Format: law/view/document?DocID={code}/{prefix}{yr}{num}/NAT/ATO/00001&amp;PiT=99991231235958
        PiT=99991231235958 is the "latest point in time" parameter.
        Plain slashes work (no URL encoding needed).
        """
        if rtype == "IT":
            docid = f"ITR/IT{num}/NAT/ATO/00001"
        elif rtype in ("AID", "ATOID"):
            return f"https://www.ato.gov.au/law/view/document?docid=AID/AID{year}{num}/00001"
        else:
            code = _ato_doc_map.get(rtype)
            if not code:
                return None
            yr = str(year) if year else ""
            docid = f"{code}/{prefix}{yr}{num}/NAT/ATO/00001"
        return f"https://www.ato.gov.au/law/view/document?DocID={docid}&PiT=99991231235958"

    def _austlii_url(rtype: str, docid_num: str, year: int) -> str | None:
        ato_path_map = {
            "TR": f"ATOTR/{year}/TR{docid_num}.html",
            "TD": f"ATOTD/{year}/TD{docid_num}.html",
            "PCG": f"ATOPCG/{year}/PCG{docid_num}.html",
            "LCG": f"ATOLCG/{year}/LCG{docid_num}.html",
            "LCR": f"ATOLCR/{year}/LCR{docid_num}.html",
            "PR": f"ATOPR/{year}/PR{docid_num}.html",
            "CR": f"ATOCR/{year}/CR{docid_num}.html",
            "GSTR": f"ATOGSTR/{year}/GSTR{docid_num}.html",
            "MT": f"ATOMT/{year}/MT{docid_num}.html",
            "TA": f"ATOTA/{year}/TA{docid_num}.html",
            "SGR": f"ATOSGR/{year}/SGR{docid_num}.html",
            "ATOID": f"ATOAID/{year}/AID{docid_num}.html",
            "AID": f"ATOAID/{year}/AID{docid_num}.html",
            "PS LA": None,
            "PS_LA": None,
        }
        path = ato_path_map.get(rtype)
        if not path:
            return None
        return f"https://www8.austlii.edu.au/au/other/rulings/ato/{path}"

    for r in rulings:
        parts = r["citation"].split("_", 2)
        if len(parts) == 3:
            rtype, yr_raw, num = parts
            # Handle PS LA citations: "PS"_"LA"_"2011_10" → type="PS_LA", yr=2011, num=10
            if rtype.upper() == "PS" and yr_raw.upper() == "LA":
                rtype = "PSLA"
                yr_doc_num = num.split("_", 1)
                yr_raw = yr_doc_num[0]
                num = yr_doc_num[1] if len(yr_doc_num) > 1 else yr_doc_num[0]
            yr = str(r["year"]) if r["year"] else yr_raw
            # Build the docid number part: <year><num> with correct year width
            if rtype == "PSLA":
                r["citation_display"] = f"PS LA {yr}/{num}"
            elif rtype == "AID":
                r["citation_display"] = f"ATO ID {yr}/{num}"
            else:
                r["citation_display"] = f"{rtype} {yr}/{num}"
            yr_doc = str(r["year"])[-2:] if r["year"] and r["year"] < 2000 else str(r["year"]) if r["year"] else yr_raw
            docid_num = f"{yr_doc}{num}"
            r["ato_url"] = _ato_url(r["type"], rtype, r.get("year"), num) or ""
            r["austlii_url"] = _austlii_url(r["type"], docid_num, r["year"]) or ""
        elif len(parts) == 2 and parts[0].upper() == "IT":
            # IT rulings: IT_262 — sequential numbering, no year
            rtype, num = parts
            r["citation_display"] = f"IT {num}"
            # ATO URL: plain DocID with PiT parameter
            r["ato_url"] = f"https://www.ato.gov.au/law/view/document?DocID=ITR/IT{num}/NAT/ATO/00001&PiT=99991231235958"
            r["austlii_url"] = ""
        else:
            r["citation_display"] = r["citation"]
            r["ato_url"] = ""
            r["austlii_url"] = ""

    return rulings


@functools.lru_cache(maxsize=None)
def _load_ruling_section_index() -> dict[str, list[dict]]:
    """Ruling -> referenced sections, from every index on disk.

    `ruling_section_index.json` covers the AU rulings. `nz_ruling_section_index.json`
    covers the NZ ones (built by scripts/build_nz_ruling_section_index.py) and was
    written but never read here, so every NZ ruling served an empty
    `referenced_sections` list while the graph held the same edges.
    """
    index: dict[str, list[dict]] = {}
    for name in ("ruling_section_index.json", "nz_ruling_section_index.json"):
        path = DATA_DIR / name
        if not path.exists():
            continue
        try:
            index.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            logger.exception("Error reading ruling section index %s", name)
    return index


@functools.lru_cache(maxsize=None)
def _ruling_refs_by_citation_form() -> dict[str, list[dict]]:
    """Citation form ("BR Prd 04/01" -> "BR_Prd_04_01") -> referenced sections.

    The NZ index is keyed by the ruling's file stem (BRPRD_2004_1), and that stem is not
    derivable from the citation: "BR Prd 04/01" normalises to BR_Prd_04_01, never to
    BRPRD_2004_1. The rulings manifest is the only place both forms appear together, so
    the bridge is built from it once. Callers hold the citation, so this is what makes
    an NZ ruling's sections resolve at all.
    """
    index = _load_ruling_section_index()
    bridge: dict[str, list[dict]] = {}
    for row in load_rulings():
        stem = Path(str(row.get("source") or "")).stem
        if not stem or stem not in index:
            continue
        bridge[_norm_citation(str(row.get("citation") or ""))] = index[stem]
    return bridge


def load_ruling_section_refs(citation: str) -> list[dict]:
    index = _load_ruling_section_index()
    # AU keys are stems ("TR_2024_1"); callers often hold the display form ("TR 2024/1").
    for key in (citation, re.sub(r"[\s/]+", "_", citation).strip("_")):
        if key in index:
            return index[key]
    return _ruling_refs_by_citation_form().get(_norm_citation(citation), [])


# ---------------------------------------------------------------------------
# Section content
# ---------------------------------------------------------------------------

def find_section_path(act: str, section: str) -> str | None:
    tree = load_tree(act)
    for part in tree.get("parts", []):
        for sec in part.get("sections", []):
            if sec["id"].lower() == section.lower():
                return sec["path"]
        for div in part.get("divisions", []):
            for sec in div.get("sections", []):
                if sec["id"].lower() == section.lower():
                    return sec["path"]
            for sub in div.get("subdivisions", []):
                for sec in sub.get("sections", []):
                    if sec["id"].lower() == section.lower():
                        return sec["path"]
    return None


@functools.lru_cache(maxsize=None)
def get_act_section_content(act: str, section: str) -> tuple[dict, str]:
    section_path = find_section_path(act, section)
    # If no exact match in tree, try case-insensitive glob for markdown file
    if not section_path:
        for md in (DATA_DIR / act / "sections").rglob("*.md"):
            if md.stem.lower() == section.lower():
                section_path = str(md.relative_to(DATA_DIR / act / "sections"))
                break

    # If still no section_path, the section is not in the tree at all
    if not section_path:
        raise HTTPException(status_code=404, detail=f"Section {section} not found")

    md_path = DATA_DIR / act / "sections" / section_path
    if not md_path.exists():
        # This case handles when section_path was found via find_section_path (exact/case-insensitive),
        # but for some reason the file it pointed to doesn't exist.
        # Given find_section_path should point to existing files *or* we handled above for tree-only,
        # this might indicate a data inconsistency. For now, treat as tree-only.
        logger.warning("Section file '%s' expected at path '%s' not found. Returning empty content.", section, md_path)
        return {}, ""


    content = md_path.read_text(encoding="utf-8")
    content = _normalise_text(content)
    fm = {}
    body = content
    if content.startswith("---"):
        fm_end = re.search(r'\n---\s*\n', content)
        if fm_end:
            fm_text = content[3:fm_end.start()].strip()
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip('"')
            body = content[fm_end.end():]

    return fm, body


@functools.lru_cache(maxsize=None)
def _definition_section_file(act: str, section: str) -> str | None:
    sections_dir = DATA_DIR / act / "sections"
    for f in sections_dir.rglob(f"{section}.md"):
        return str(f)
    return None


def get_definition_text(act: str, term: str) -> dict | None:
    act = resolve_act_id(act)
    defs = load_definitions(act)
    if not defs:
        return None
    info = defs.get(_normalize_term_key(term))
    scan_key = _normalize_term_key(term)
    if not info:
        # Some index keys bundle synonyms in the heading, e.g.
        # "resident or resident of Australia" (ITAA 1936 s 6(1)) or
        # "royalty or royalties". Match the queried term against such
        # alternatives — but only when the alternatives are genuinely
        # synonymous: most " or " keys are ordinary phrases ("failure to do
        # an act or thing", "debenture of a company or unit trust") where a
        # mid-phrase " or " must NOT be treated as a separator.
        def _alt_synonym(a: str, b: str) -> bool:
            a, b = a.strip().lower(), b.strip().lower()
            if not a or not b:
                return False
            if a == b or a.startswith(b) or b.startswith(a):
                return True

            def _sing(x: str) -> str:
                if x.endswith("ies") and len(x) > 4:
                    return x[:-3] + "y"
                if x.endswith("s") and not x.endswith("ss"):
                    return x[:-1]
                return x

            sa, sb = _sing(a), _sing(b)
            return sa == sb or sa.startswith(sb) or sb.startswith(sa)

        for k, inf in defs.items():
            alts = [a.strip() for a in k.split(" or ")]
            if len(alts) > 1 and scan_key in alts and any(
                    _alt_synonym(scan_key, o) for o in alts if o != scan_key):
                info, scan_key = inf, k
                break
    if not info:
        return None
    section = info.get("section", "")
    if not section:
        return None

    md_file = _definition_section_file(act, section)
    if not md_file:
        return None
    md_path = Path(md_file)

    content = md_path.read_text(encoding="utf-8")
    if content.startswith("---"):
        fm_end = re.search(r"\n---\s*\n", content)
        body = content[fm_end.end():] if fm_end else content
    else:
        body = content
    # Normalize Unicode apostrophes/quotes in body text to match term keys
    body = body.replace("\u2018", "'").replace("\u2019", "'")
    body = body.replace("\u201c", '"').replace("\u201d", '"')
    # Strip standalone asterisk markers — they're PDF cross-reference artifacts
    # that appear inline within terms (e.g. "*life insurance policies means")
    body = body.replace("*", "")

    term_lower = scan_key
    escaped = re.escape(term_lower)
    # Patterns for definition anchors
    patterns = [
        rf'(?<!\w){escaped}(?:,\s[^.;\n]{{0,200}}?,)?\s+(?:has\s+(?:(?:the|a)\s+)?(?:same\s+)?meaning|is\s+defined\s+in|means|includes)(?:\s|:|$)',
        rf'(?<!\w){escaped}\s*:',
        # NZ IT Act 2007 style: "term— (a) means ..."
        rf'(?<!\w){escaped}\s*—',
    ]

    # A term occurrence can be the tail of a longer defined term (e.g.
    # "resident" inside "foreign resident"). Such an occurrence must not anchor
    # the lookup — it belongs to that longer term — so reject matches preceded
    # by words that form a known definition key in this act.
    other_keys = {k for k in defs if k != scan_key}

    def _tail_of_known_term(start: int) -> bool:
        before = body[max(0, start - 120):start]
        cut = max((before.rfind(ch) for ch in ".;:!?\n()"), default=-1)
        phrase = before[cut + 1:] if cut != -1 else before
        words = re.findall(r"[A-Za-z0-9][\w'\-]*", phrase)
        for n in range(1, min(6, len(words)) + 1):
            cand = " ".join(words[-n:]) + " " + term_lower
            if _normalize_term_key(cand) in other_keys:
                return True
        return False

    # Collect ALL matches so we can prefer the primary definition
    # over sub-definitions (e.g. prefer "dividend includes:" over
    # "demerger dividend means:...")
    candidates: list[tuple[re.Match, int]] = []
    for pat in patterns:
        for m in re.finditer(pat, body, re.IGNORECASE):
            before = body[max(0, m.start() - 60):m.start()].rstrip()
            is_primary = (
                m.start() == 0
                or before.endswith('.')
                or before.endswith('.\n')
                or before.endswith(';')
                or before.endswith('\n')
                or before.endswith(':')
                or not before
            )
            priority = 0 if is_primary else 1
            candidates.append((m, priority))

    if not candidates:
        return None

    # Sort: primary definitions first, then by position
    candidates.sort(key=lambda c: (c[1], c[0].start()))
    m = candidates[0][0]
    idx = m.start()

    # Guard against a false-positive anchor: if the best occurrence is the tail
    # of a longer defined term (e.g. "resident" inside "foreign resident"), it
    # belongs to that term, not this one. Refuse rather than return another
    # term's text; callers fall back to other acts / suggestions.
    if _tail_of_known_term(idx):
        return None

    # Find end: the boundary where this definition ends.
    # Strategy: Scan forward from the match position for:
    #   1. The next defined term in the same section (most reliable for dictionary-style sections)
    #   2. Next definition anchor (<a id="...">) or heading (####) — strong stop
    #   3. End of body
    rest = body[idx + len(m.group()):]

    # Use the sorted definitions index keys to determine the boundary.
    # Collect all terms in the same section, sort alphabetically,
    # and find the alphabetically-next term's definition anchor.
    end_pos = len(body)
    current_lower = scan_key
    same_section_terms = sorted([
        t for t, info in defs.items()
        if info.get("section") == section
    ])
    try:
        cur_idx = same_section_terms.index(current_lower)
    except ValueError:
        cur_idx = -1
    if cur_idx >= 0 and cur_idx + 1 < len(same_section_terms):
        next_term = same_section_terms[cur_idx + 1]
        escaped_t = re.escape(next_term)
        term_match = re.search(
            rf'(?:\n(?:[-•*]\s+)?|\.\s+)({escaped_t}\s+(?:has\s+(?:(?:the|a)\s+)?(?:same\s+)?meaning|means|includes)(?:\s|:|$))',
            rest,
            re.IGNORECASE,
        )
        if not term_match:
            term_match = re.search(
                rf'(?:\n(?:[-•*]\s+)?|\.\s+)({escaped_t}\s*:)',
                rest,
                re.IGNORECASE,
            )
        if not term_match:
            term_match = re.search(
                rf'(?:\n(?:[-•*]\s+)?|\.\s+|:\s+)({escaped_t}\s*—)',
                rest,
                re.IGNORECASE,
            )
        if term_match:
            end_pos = idx + len(m.group()) + term_match.start()

    # Fallback: next definition anchor or heading
    if end_pos == len(body):
        anchor_end = re.search(r'\n(?:####?\s|<a\s+id=")', rest, re.IGNORECASE)
        if anchor_end:
            end_pos = idx + len(m.group()) + anchor_end.start()

    text = body[idx:end_pos].strip()
    # Hard cap at 5000 characters to prevent boundary-bleeding regression
    MAX_DEF_LENGTH = 5000
    if len(text) > MAX_DEF_LENGTH:
        text = text[:MAX_DEF_LENGTH]
    text = re.sub(r'<a id="[^"]+"></a>\s*\n?', "", text)
    text = re.sub(r">\s*", "", text)
    text = re.sub(r"\*\*\((\d+)\)\*\*\s*", r"(\1) ", text)
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"\n{2,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Determine if the definition is a brief cross-reference
    # (e.g. "enterprise has the meaning given by section 9-20.")
    is_cross_ref = bool(re.match(
        r'^[^.]+\bhas\s+the\s+meaning\s+(given\s+by|in)\s',
        text,
        re.IGNORECASE,
    ))

    # Detect if text was truncated (doesn't end with sentence-ending punctuation)
    truncated = bool(text) and not re.search(r'[.\)"\'!?;]\s*$', text)

    return {
        "term": info.get("term", term),
        "act": act,
        "section": section,
        "anchor": info.get("anchor", ""),
        "text": text,
        "path": f"/{act}/s{section}#{info.get('anchor', '')}",
        "truncated": truncated,
        "is_cross_reference": is_cross_ref,
        "text_length": len(text),
    }


# What must follow a term for the occurrence to be a definition anchor.
# Allows an optional ", in relation to X," style qualifier before the verb.
_DEF_ANCHOR_SUFFIX = re.compile(
    r'(?:,\s[^.;\n]{0,200}?,)?'
    r'\s+(?:has\s+(?:(?:the|a)\s+)?(?:same\s+)?meaning|is\s+defined\s+in|means|includes)(?:\s|:|$)'
    r'|\s*:'
    r'|\s*—'
)


def _find_definition_start(key: str, body: str, pos: int) -> int | None:
    """First definition anchor for key at/after pos, preferring anchors at a
    sentence/line boundary; falls back to a full-body retry.

    Uses str.find for the term literal (re's scan is ~100x slower here) and
    validates each occurrence with a word-boundary check plus suffix regex.
    """
    for from_pos in (pos, 0) if pos else (0,):
        first_raw = None
        s = body.find(key, from_pos)
        checked = 0
        while s != -1 and checked < 200:
            checked += 1
            end = s + len(key)
            if (s == 0 or not (body[s - 1].isalnum() or body[s - 1] == "_")) \
                    and _DEF_ANCHOR_SUFFIX.match(body, end):
                if first_raw is None:
                    first_raw = s
                before = body[max(0, s - 60):s].rstrip()
                if not before or before[-1] in '.:;>"\n':
                    return s
            s = body.find(key, s + 1)
        if first_raw is not None:
            return first_raw
    return None


def _clean_definition_snippet(text: str) -> str:
    text = re.sub(r'<a id="[^"]+"></a>\s*\n?', "", text)
    text = re.sub(r">\s*", "", text)
    text = re.sub(r"\*\*\((\d+)\)\*\*\s*", r"(\1) ", text)
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"\n{2,}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@functools.lru_cache(maxsize=32)
def _definitions_with_text_cached(act: str, mtime: float) -> tuple:
    raw_terms = _load_definitions_store().get(act, {}).get("terms", {})
    if not raw_terms:
        return ()

    # Group terms by defining section so each section body is scanned once,
    # in alphabetical (= document) order, instead of per-term full scans.
    by_section: dict[str, list[str]] = {}
    for term in raw_terms:
        by_section.setdefault(raw_terms[term].get("section", ""), []).append(term)

    MAX_DEF_LENGTH = 5000
    results = []
    for section, terms in by_section.items():
        texts: dict[str, str] = {}
        md_file = _definition_section_file(act, section) if section else None
        if md_file:
            body = Path(md_file).read_text(encoding="utf-8")
            if body.startswith("---"):
                fm_end = re.search(r"\n---\s*\n", body)
                if fm_end:
                    body = body[fm_end.end():]
            body = body.replace("‘", "'").replace("’", "'")
            body = body.replace("“", '"').replace("”", '"')
            body = body.replace("*", "")
            search_body = body.lower()
            if len(search_body) != len(body):  # pathological unicode lowering
                search_body = body

            # Dictionary sections list terms alphabetically, so scan forward
            # from the previous hit; fall back to a full search on a miss.
            ordered = sorted(terms, key=lambda t: _normalize_term_key(t))
            pos = 0
            starts: list[tuple[int, str]] = []
            for term in ordered:
                start = _find_definition_start(_normalize_term_key(term), search_body, pos)
                if start is not None:
                    starts.append((start, term))
                    pos = max(pos, start)
            starts.sort()
            for i, (start, term) in enumerate(starts):
                end = starts[i + 1][0] if i + 1 < len(starts) else len(body)
                texts[term] = _clean_definition_snippet(body[start:end])[:MAX_DEF_LENGTH]

        for term in terms:
            info = raw_terms[term]
            results.append({
                "term": info.get("term", term),
                "section": _canon_section_id(section) if section else section,
                "anchor": info.get("anchor", ""),
                "text": texts.get(term) or info.get("definition", ""),
            })

    results.sort(key=lambda r: r["term"].lower())
    return tuple(results)


def load_definitions_with_text(act: str) -> list[dict]:
    """Every definition in an act with its resolved text, alphabetically."""
    act = resolve_act_id(act)
    path = _definitions_store_path()
    mtime = path.stat().st_mtime if path else 0.0
    return [dict(item) for item in _definitions_with_text_cached(act, mtime)]


def get_definition_across_acts(term: str, preferred_act: str | None = None) -> dict | None:
    """Look a term up across every act that carries a definitions index.

    Structural note: definitions do not live in a single place. ITAA 1997 and
    the GST Act each have a dictionary section (s 995-1 / s 195-1), but ITAA
    1936 scatters its definitions across the Act (s 6(1), s 317 for the CFC
    rules, s 318 for "associate", etc.), so a term absent from one act's index
    may be defined in another. Returning matches from all acts is an interim
    measure until the per-act indexes fully cover their scattered definitions.

    The requested act (if any) is returned as the primary match; other acts
    that define the same term are listed under ``also_defined_in``.
    """
    order: list[str] = []
    if preferred_act:
        order.append(preferred_act)
    for a in _all_definition_acts():
        if a not in order:
            order.append(a)

    matches: list[dict] = []
    for a in order:
        try:
            r = get_definition_text(a, term)
        except Exception:
            r = None
        if not r:
            # Plural fallback: index keys are singular (e.g. 'capital gain')
            singular = _singularise_term(term)
            if singular != term.strip():
                try:
                    r = get_definition_text(a, singular)
                except Exception:
                    r = None
        if r:
            matches.append(r)

    if not matches:
        return None

    primary = matches[0]
    return {
        **primary,
        "also_defined_in": [
            {
                "act": m["act"],
                "section": m["section"],
                "text": m["text"],
                "path": m.get("path", ""),
            }
            for m in matches[1:]
        ],
    }
