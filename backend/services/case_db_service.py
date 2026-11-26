"""Service layer for case-related database queries.

Reuses _sql() / _sql_dict() from tax_case_sql.py to query the
Cadena Knowledge Postgres database via docker exec.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

from backend.services.tax_case_sql import _sql, _sql_dict
from backend.services.text_cleaner import clean_case_paragraph

logger = logging.getLogger(__name__)

# lawkitty case data lives in files, not the cadena_knowledge Postgres:
#  - scripts/cleaned/summaries/{year}_{court}_{num}.json  (AI summaries)
#  - data/{aata,fca,fcafc,hca}_tax_cases.json             (citation manifests)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SUMMARIES_DIR = _REPO_ROOT / "scripts" / "cleaned" / "summaries"
_DATA_DIR = _REPO_ROOT / "data"
_MANIFEST_FILES = ("hca_tax_cases", "fca_tax_cases", "fcafc_tax_cases", "aata_tax_cases")


def citation_to_summary_filename(citation: str) -> str:
    """``[2016] HCA 45`` -> ``2016_HCA_45.json`` (the on-disk summary name)."""
    return (
        citation.replace(" ", "_").replace("[", "").replace("]", "").replace("/", "_") + ".json"
    )


def load_case_summary(citation: str) -> dict[str, Any] | None:
    """Load the AI summary JSON for a case citation, or None."""
    path = _SUMMARIES_DIR / citation_to_summary_filename(citation)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_case_manifest(citation: str) -> dict[str, Any] | None:
    """Load the citation-manifest entry (title/year/href) for a case, or None."""
    for name in _MANIFEST_FILES:
        path = _DATA_DIR / f"{name}.json"
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data:
                if isinstance(entry, dict) and entry.get("citation") == citation:
                    return entry
        except Exception:
            continue
    return None


def load_case_index_meta(citation: str) -> dict[str, Any] | None:
    """Look up a case in the full case-list index (SQLite ``case_index_meta``).

    Covers the ~71k FCA/FCAFC/HCA judgments that live in
    ``data/{fca,fcafc,hca}_all_cases.json`` but not in the tax manifests or the
    AI-summary store.  Returns None if the table or citation is absent.
    """
    try:
        import sqlite3
        from backend.config import SEARCH_DB
    except Exception:
        return None
    try:
        conn = sqlite3.connect(f"file:{SEARCH_DB}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT citation, case_name, court, year, subject, npa, catchwords, "
                "parallel_citations FROM case_index_meta WHERE citation = ?",
                (citation,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    try:
        parallel = json.loads(row["parallel_citations"] or "[]")
    except Exception:
        parallel = []
    return {
        "citation": row["citation"],
        "case_name": row["case_name"],
        "court": row["court"],
        "year": row["year"],
        "subject": row["subject"],
        "npa": row["npa"],
        "catchwords": row["catchwords"],
        "parallel_citations": parallel,
        "austlii_url": austlii_url_for(row["citation"]),
    }


def austlii_url_for(citation: str) -> str:
    """Build the AustLII viewdoc URL from a ``[year] COURT num`` citation."""
    parsed = _parse_citation(citation)
    if not parsed:
        return ""
    return (
        f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/"
        f"cth/{parsed['court']}/{parsed['year']}/{parsed['num']}.html"
    )


# ---------------------------------------------------------------------------
# Case-list index: decision dates + party-name aliases (code-side augment)
# ---------------------------------------------------------------------------
# The SQLite ``case_index_meta`` table carries no decision date; the source
# ``data/*_all_cases.json`` files (which back that table) do. The tax-case
# manifests carry titles but no dates either. These loaders expose the raw
# case-list entries so resolve_ref can fill decision_date and resolve a case
# by party name without a re-ingestion.
_ALL_CASES_FILES = ("hca_all_cases", "fca_all_cases", "fcafc_all_cases")


@functools.lru_cache(maxsize=1)
def _all_cases_index() -> dict[str, dict]:
    """citation -> raw case-list entry, built once per process from *_all_cases.json."""
    index: dict[str, dict] = {}
    for name in _ALL_CASES_FILES:
        path = _DATA_DIR / f"{name}.json"
        if not path.exists():
            continue
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Could not read case list %s", path)
            continue
        for e in entries if isinstance(entries, list) else []:
            cite = e.get("citation") if isinstance(e, dict) else None
            if cite and cite not in index:
                index[cite] = e
    return index


def load_case_list_date(citation: str) -> str | None:
    """Return the ISO decision date for a citation from the case-list files, or None."""
    entry = _all_cases_index().get(citation)
    if not entry:
        return None
    return entry.get("date") or None


def load_case_list_court_url(citation: str) -> str | None:
    """Return the court's own page for a citation (hcourt.gov.au / judgments.fedcourt.gov.au), or None."""
    return (_all_cases_index().get(citation) or {}).get("court_url") or None


# Corporate-status tokens and location tags that a caller is unlikely to type
# when naming a case by its party (e.g. "Ready Mixed Concrete (Vic) Pty Ltd").
_CASE_ALIAS_STOPWORDS = {
    "pty", "ltd", "limited", "inc", "incorporated", "corp", "corporation",
    "llc", "plc", "co", "group", "holdings", "trustee", "trust", "nominees",
}

_GOV_CONJOINER_ALIAS_RE = re.compile(
    r"\s+and\s+(?=(?:the\s+)?(?:federal\s+|deputy\s+)?commissioner\b|australian securities\b)",
    re.IGNORECASE,
)


def _alias_key(name: str) -> str:
    """Normalise a party/case name into a whitespace/punctuation-insensitive key."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _alias_variants(case_name: str) -> list[str]:
    """Keys a caller might type for a case (full name, party, party short form)."""
    variants: list[str] = []
    name = case_name or ""
    full = _alias_key(name)
    if full:
        variants.append(full)
    # Drop a medium-neutral citation and any parenthetical (date/location tag).
    stripped = re.sub(r"\[?\d{4}\]?\s+[A-Za-z]+\s+\d+", " ", name)
    stripped = re.sub(r"\([^)]*\)", " ", stripped)
    base = _alias_key(stripped)
    if base and base not in variants:
        variants.append(base)
    # The private/taxpayer party (before " v " or " and <government party>").
    party = _GOV_CONJOINER_ALIAS_RE.split(stripped, maxsplit=1)[0]
    party = re.split(r"\s+v\s+", party, maxsplit=1)[0]
    party_key = _alias_key(party)
    if party_key and party_key not in variants:
        variants.append(party_key)
    short = " ".join(t for t in party_key.split() if t not in _CASE_ALIAS_STOPWORDS)
    if short and short not in variants:
        variants.append(short)
    # Multi-party / "Re X; Ex parte Y" titles — expose each party as a
    # short-form alias (e.g. "Wakim" from "Re Wakim; Ex parte McNally").
    for seg in re.split(r"\s*;\s*|\s+ex\s+parte\s+", stripped, flags=re.IGNORECASE):
        seg = re.sub(r"^re\s+", "", seg, flags=re.IGNORECASE)
        if " v " in seg:
            continue
        frag = _alias_key(seg)
        frag = " ".join(t for t in frag.split() if t not in _CASE_ALIAS_STOPWORDS)
        if frag and len(frag) >= 4 and frag not in variants:
            variants.append(frag)
    return variants


@functools.lru_cache(maxsize=1)
def case_alias_map() -> dict[str, str]:
    """Normalised party/case name -> medium-neutral citation.

    Built from the full case list plus the tax-case manifests, so
    ``fetch("Ready Mixed Concrete")`` resolves to ``[1969] HCA 12``. First
    writer wins, so a full-title key always beats a later short-form collision.
    """
    aliases: dict[str, str] = {}
    for cite, entry in _all_cases_index().items():
        for key in _alias_variants(entry.get("case_name") or ""):
            aliases.setdefault(key, cite)
    for name in _MANIFEST_FILES:
        path = _DATA_DIR / f"{name}.json"
        if not path.exists():
            continue
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            cite, title = e.get("citation"), e.get("title")
            if cite and title:
                for key in _alias_variants(title):
                    aliases.setdefault(key, cite)
    return aliases


def lookup_case_alias(name: str) -> str | None:
    """Resolve a party/case name to a medium-neutral citation, or None."""
    key = _alias_key(name)
    if not key:
        return None
    return case_alias_map().get(key)


# ---------------------------------------------------------------------------
# Case-citation hygiene (LK-29)
# ---------------------------------------------------------------------------
# Summary ``cases_cited`` entries are bare strings. Some carry only a truncated
# parallel report citation (e.g. "(1991) 173 CLR" with the page dropped). Repair
# them from the verified crosswalk where possible, otherwise tag them as
# unparsed so a caller does not mistake an incomplete cite for a complete one.
_MEDIUM_NEUTRAL_RE = re.compile(r"\[\d{4}\]\s+[A-Za-z]+\s+\d+")
# A complete report citation: "(1961) WALR 103", "(1991) 91 ATC 4546",
# "(1981) Q.B. 223", or a bare volume+reporter+page ("84 ATC 4146", "3 ATD 142").
# Reporter tokens are uppercase abbreviations, so this does not fire on prose
# like "16 April 1982". A cite that stops at the reporter ("(1991) 173 CLR") is
# NOT complete — it has lost its page.
_COMPLETE_REPORT_RE = re.compile(
    r"\(\d{4}\)\s+(?:\d+\s+)?[A-Z][A-Z.]{1,6}\s+\d+"
    r"|\b\d+\s+[A-Z][A-Z.]{1,6}\s+\d+\b"
)


@functools.lru_cache(maxsize=1)
def _case_crosswalk() -> dict[str, str]:
    """Medium-neutral citation -> parallel report citation (data/case_crosswalk.json)."""
    try:
        return json.loads((_DATA_DIR / "case_crosswalk.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _repair_report_citation(entry: str) -> str | None:
    """Return a medium-neutral citation whose report cite extends *entry*, or None."""
    key = _alias_key(entry)
    if not key:
        return None
    for mnc, report in _case_crosswalk().items():
        if report and _alias_key(report).startswith(key + " "):
            return mnc
    return None


def clean_case_citations(cases_cited: list) -> list:
    """Return case references as complete citations, or tagged ``[unparsed]``.

    A reference is complete when it carries a medium-neutral citation or a
    report citation with a page/number. A truncated report citation is repaired
    from the crosswalk (``"(1991) 173 CLR"`` -> ``"(1991) 173 CLR ([1991] HCA 39)"``);
    anything else is returned with an explicit ``[unparsed]`` marker.
    """
    cleaned: list = []
    for raw in cases_cited or []:
        if not isinstance(raw, str):
            cleaned.append(raw)
            continue
        entry = raw.strip()
        if not entry:
            continue
        if _MEDIUM_NEUTRAL_RE.search(entry) or _COMPLETE_REPORT_RE.search(entry):
            cleaned.append(entry)
            continue
        repaired = _repair_report_citation(entry)
        if repaired:
            cleaned.append(f"{entry} ({repaired})")
        else:
            cleaned.append(f"{entry} [unparsed]")
    return cleaned

# Citation regex: [YEAR] COURT NUMBER
_CITATION_RE = re.compile(r"\[(\d+)\]\s+(\w+)\s+(\d+)")

# AustLII navigation chrome that should never appear in paragraph_number values
_AUSTLII_NAV_CHROME = {
    "home", "databases", "worldlii", "search", "feedback",
    "database search", "name search", "recent decisions",
    "noteup", "download", "help", "index",
    "you are here", "last updated", "austlii",
    "print", "email", "full text", "cookie",
    "privacy", "disclaimer", "copyright",
}

# Recognised Australian court codes for citation validation
_VALID_COURT_CODES = {
    "HCA", "FCA", "FCAFC", "AATA", "AAT",
    "NSWCA", "NSWSC", "NSWCCA", "NSWDC", "NSWLC",
    "VSC", "VSCA", "VCC", "VMC",
    "QSC", "QCA", "QDC", "QMC",
    "SASC", "SASCFC", "SADC", "SAMC",
    "WASC", "WASCA", "WADC", "WAMC",
    "TASSC", "TASFC", "TASMC",
    "ACTSC", "ACTCA", "ACTMC",
    "NTSC", "NTCA", "NTMC",
    "FamCA", "FamCAFC",
    "FedCFamC1A", "FedCFamC1", "FedCFamC2",
    "IRCA", "AIRC",
    "ACCC", "ASIC",
    "HCA", "HCATrans",
    "ALRC", "VLRC",
    "SCCA", "SGCA", "SGHC",
    "UKSC", "UKHL", "UKPC",
    "NZSC", "NZCA", "NZHC",
    "CLR", "ALR", "ALJR", "FCR", "NSWLR",
}


def _is_valid_court_code(court: str) -> bool:
    """Check if a court code is recognised (or close enough to correct)."""
    return court in _VALID_COURT_CODES


def _is_austlii_chrome(text: str) -> bool:
    """Check if a text value is pure AustLII navigation chrome."""
    if not text or not isinstance(text, str):
        return False
    return text.strip().lower() in _AUSTLII_NAV_CHROME


# ---------------------------------------------------------------------------
# Helper: parse citation into components
# ---------------------------------------------------------------------------


def _parse_citation(citation: str) -> dict[str, str | int] | None:
    """Parse ``[2024] HCA 1`` → ``{year: 2024, court: 'HCA', num: 1}``."""
    m = _CITATION_RE.match(citation.strip())
    if not m:
        return None
    return {"year": int(m.group(1)), "court": m.group(2), "num": int(m.group(3))}


def _safe(val: str) -> str:
    """Escape single quotes for SQL."""
    return val.replace("'", "''")


_DATE_IN_PARENS_RE = re.compile(
    r'\((\d{1,2})\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(\d{4})\)'
)

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _extract_date_from_name_or_content(case: dict, safe_citation: str) -> str | None:
    """Try to extract an exact date from the case_name or document content.

    Looks for a trailing parenthetical date in AustLII format:
      ``... (10 June 2026)``

    Returns ``YYYY-MM-DD`` string or None.
    """
    # 1. Try case_name first
    case_name = case.get("case_name", "") or ""
    m = _DATE_IN_PARENS_RE.search(case_name)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2), int(m.group(3))
        month = _MONTH_MAP.get(month_str.lower())
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"

    # 2. Try the first line of the document content (which often repeats the
    #    citation with date: "Name [2025] FCAFC 15 (19 February 2025)")
    try:
        import subprocess
        r = subprocess.run(
            ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
             "-d", "cadena_knowledge", "-tA",
             "-c",
             f"SELECT SUBSTRING(content FROM 1 FOR 300) FROM documents "
             f"WHERE id = (SELECT document_id FROM cases "
             f"WHERE citation = '{safe_citation}' LIMIT 1) LIMIT 1;"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            snippet = r.stdout.strip()
            if snippet:
                m2 = _DATE_IN_PARENS_RE.search(snippet)
                if m2:
                    day, month_str, year = int(m2.group(1)), m2.group(2), int(m2.group(3))
                    month = _MONTH_MAP.get(month_str.lower())
                    if month:
                        return f"{year:04d}-{month:02d}-{day:02d}"
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Tool 1: get_case_metadata
# ---------------------------------------------------------------------------


def get_case_metadata(
    citation: str,
    include_legislation_refs: bool = False,
) -> dict[str, Any] | None:
    """Return case metadata + structural outline (no paragraph text).

    Args:
        citation: e.g. ``[2024] HCA 1``.
        include_legislation_refs: If True also return legislation references.

    Returns:
        Dict with case info, or None if not found.
    """
    # ── case row ──────────────────────────────────────────────────────────
    rows = _sql_dict(
        [
            "id",
            "citation",
            "case_name",
            "court",
            "decision_date",
            "judges",
            "outcome",
            "related_provisions",
            "related_rulings",
            "head_notes",
        ],
        "SELECT id, citation, case_name, court, decision_date::text, judges, "
        "outcome, related_provisions, related_rulings, head_notes::text "
        "FROM cases WHERE citation = %s LIMIT 1",
        (citation,),
    )
    if not rows:
        return None

    case = rows[0]
    case_id = case["id"]

    # Clean array fields
    for arr_field in ("judges", "related_provisions", "related_rulings"):
        if isinstance(case.get(arr_field), list):
            case[arr_field] = [str(s).strip('"') for s in case[arr_field]]
        elif case.get(arr_field) is None:
            case[arr_field] = []

    # Parse head_notes JSON
    if isinstance(case.get("head_notes"), str):
        try:
            import json

            case["head_notes"] = json.loads(case["head_notes"])
        except Exception:
            pass

    # Clean AustLII navigation garbage from key_terms
    _AUSTLII_NAV = {
        "databases", "noteup", "name search", "database search",
        "last updated", "notice", "commonwealth law reports",
        "austlii", "austlii home", "austlii database", "citation",
        "print", "download", "email", "full text", "help",
        "cookie", "privacy", "disclaimer", "copyright",
    }
    if isinstance(case.get("head_notes"), dict):
        key_terms = case["head_notes"].get("key_terms", [])
        if isinstance(key_terms, list):
            cleaned = [
                t for t in key_terms
                if isinstance(t, str) and t.strip().lower() not in _AUSTLII_NAV
            ]
            case["head_notes"]["key_terms"] = cleaned

    # ── content_length from documents ─────────────────────────────────────
    doc_rows = _sql_dict(
        ["content_length"],
        "SELECT LENGTH(content) as content_length FROM documents "
        "WHERE id = (SELECT document_id FROM cases WHERE citation = %s)",
        (citation,),
    )
    content_length = doc_rows[0]["content_length"] if doc_rows else None

    # ── cited_by_count ────────────────────────────────────────────────────
    cite_rows = _sql_dict(
        ["cnt"],
        "SELECT COUNT(*) as cnt FROM case_citations "
        "WHERE cited_citation = %s",
        (citation,),
    )
    cited_by_count = cite_rows[0]["cnt"] if cite_rows else 0

    # ── legislation_refs_count ────────────────────────────────────────────
    leg_rows = _sql_dict(
        ["cnt"],
        "SELECT COUNT(*) as cnt FROM case_legislation_refs "
        "WHERE case_id = %s",
        (case_id,),
    )
    legislation_refs_count = leg_rows[0]["cnt"] if leg_rows else 0

    # ── paragraph count ───────────────────────────────────────────────────
    para_rows = _sql_dict(
        ["cnt"],
        "SELECT COUNT(*) as cnt FROM case_paragraphs "
        "WHERE case_id = %s",
        (case_id,),
    )
    paragraph_count = para_rows[0]["cnt"] if para_rows else 0

    # ── section outline ───────────────────────────────────────────────────
    outline_rows = _sql_dict(
        ["section_type", "count", "start_seq", "end_seq"],
        "SELECT section_type, COUNT(*) as count, "
        "MIN(sequence_order) as start_seq, MAX(sequence_order) as end_seq "
        "FROM case_paragraphs "
        "WHERE case_id = %s "
        "GROUP BY section_type ORDER BY MIN(sequence_order)",
        (case_id,),
    )

    # ── download URLs ─────────────────────────────────────────────────────
    parsed = _parse_citation(citation)
    download_urls = {}
    if parsed:
        court = str(parsed["court"])
        year = str(parsed["year"])
        num = str(parsed["num"])
        download_urls["austlii_url"] = (
            f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/"
            f"cth/{court}/{year}/{num}.html"
        )

# ── assemble result ───────────────────────────────────────────────────
# ---------------------------------------------------------------------------
# Shared helper: fix ITAA 1936 sections mislabelled as ITAA 1997
# ---------------------------------------------------------------------------


def _fix_itaa1936_act_titles(leg_rows: list[dict]) -> None:
    """Post-process legislation refs to fix known ITAA 1936 sections mislabelled as ITAA 1997.

    These are simple-integer sections from ITAA 1936 Part III Div 6, 7, 7A, etc.
    that the ingestion logic defaulted to 1997. Modifies rows in-place.
    """
    _ITAA1936_SECTIONS_LOWER = {
        "s.25", "s.25(1)",
        "s.26", "s.26(a)",
        "s.260",
        "s.95", "s.96", "s.97", "s.97(1)", "s.98", "s.99", "s.100", "s.100a",
        "s.101", "s.102", "s.102a", "s.103", "s.103a", "s.104",
        "s.105", "s.106", "s.107", "s.108", "s.109", "s.109a",
        "s.109b", "s.109c", "s.109d", "s.109e", "s.109f", "s.109g",
        "s.109h", "s.109j", "s.109k", "s.109l", "s.109m", "s.109n",
        "s.109p", "s.109q", "s.109r", "s.109s", "s.109t", "s.109u",
        "s.109v", "s.109w", "s.109x", "s.109y", "s.109z", "s.109za", "s.109zb",
        "s.110", "s.111", "s.112", "s.113", "s.114", "s.115", "s.116",
        "s.117", "s.118", "s.119", "s.120", "s.121", "s.122", "s.123",
        "s.124", "s.125", "s.126", "s.127", "s.128",
        "s.160za", "s.160zb", "s.160zc", "s.160zd",
        "s.177a", "s.177b", "s.177c", "s.177d", "s.177e", "s.177f", "s.177g",
        "s.200", "s.201", "s.202", "s.202a",
        "s.221a", "s.221b", "s.221c", "s.221d",
        "s.221h", "s.221j", "s.221k", "s.221l",
        "s.221p", "s.221q", "s.221r", "s.221s", "s.221t",
        "s.221y", "s.221ya", "s.221yb", "s.221yc", "s.221yd",
        "s.221yh", "s.221yhj", "s.221yhk", "s.221yhl", "s.221yhm",
        "s.254", "s.255", "s.256", "s.257", "s.258",
        "s.255-1",  # TAA 1953
    }
    # Heuristic: any unhyphenated 109-series section reference (s.109* without hyphen)
    # defaults to ITAA 1936 (Division 7A and adjacent provisions).
    _109_SERIES_RE = re.compile(r'^s\.109[a-z0-9]+(?:\(.*\))?$')

    for row in leg_rows:
        ref = (row.get("section_reference") or "").lower().strip()
        at = row.get("act_title") or ""
        is_1997 = "1997" in at

        if not is_1997:
            continue

        # Exact match against known ITAA 1936 sections (all lowercase set)
        if ref in _ITAA1936_SECTIONS_LOWER:
            row["act_title"] = "Income Tax Assessment Act 1936"
            row["_act_corrected"] = True
            continue

        # Handle subsection variants like s.97(1) by stripping the subsection
        base_ref = re.sub(r'\(.*\)', '', ref).strip()
        if base_ref in _ITAA1936_SECTIONS_LOWER:
            row["act_title"] = "Income Tax Assessment Act 1936"
            row["_act_corrected"] = True
            continue

        # Heuristic: unhyphenated 109-series → ITAA 1936
        # Covers s.109B, s.109R, s.109XB, s.109XA(1), s.109BC, s.109XG, etc.
        if _109_SERIES_RE.match(ref) or _109_SERIES_RE.match(base_ref):
            row["act_title"] = "Income Tax Assessment Act 1936"
            row["_act_corrected"] = True


def get_case_metadata(
    citation: str,
    include_legislation_refs: bool = False,
) -> dict[str, Any] | None:
    """Return case metadata + structural outline (no paragraph text).

    Args:
        citation: e.g. ``[2024] HCA 1``.
        include_legislation_refs: If True also return legislation references.

    Returns:
        Dict with case info, or None if not found.
    """
    # ── case row ──────────────────────────────────────────────────────────
    rows = _sql_dict(
        [
            "id",
            "citation",
            "case_name",
            "court",
            "decision_date",
            "judges",
            "outcome",
            "related_provisions",
            "related_rulings",
            "head_notes",
        ],
        "SELECT id, citation, case_name, court, decision_date::text, judges, "
        "outcome, related_provisions, related_rulings, head_notes::text "
        "FROM cases WHERE citation = %s LIMIT 1",
        (citation,),
    )
    if not rows:
        return None

    case = rows[0]
    case_id = case["id"]

    # Clean array fields
    for arr_field in ("judges", "related_provisions", "related_rulings"):
        if isinstance(case.get(arr_field), list):
            case[arr_field] = [str(s).strip('"') for s in case[arr_field]]
        elif case.get(arr_field) is None:
            case[arr_field] = []

    # Parse head_notes JSON
    if isinstance(case.get("head_notes"), str):
        try:
            import json
            case["head_notes"] = json.loads(case["head_notes"])
        except Exception:
            pass

    # Clean AustLII navigation garbage from key_terms
    _AUSTLII_NAV = {
        "databases", "noteup", "name search", "database search",
        "last updated", "notice", "commonwealth law reports",
        "austlii", "austlii home", "austlii database", "citation",
        "print", "download", "email", "full text", "help",
        "cookie", "privacy", "disclaimer", "copyright",
    }
    if isinstance(case.get("head_notes"), dict):
        key_terms = case["head_notes"].get("key_terms", [])
        if isinstance(key_terms, list):
            cleaned = [
                t for t in key_terms
                if isinstance(t, str) and t.strip().lower() not in _AUSTLII_NAV
            ]
            case["head_notes"]["key_terms"] = cleaned

    # ── content_length from documents ─────────────────────────────────────
    doc_rows = _sql_dict(
        ["content_length"],
        "SELECT LENGTH(content) as content_length FROM documents "
        "WHERE id = (SELECT document_id FROM cases WHERE citation = %s)",
        (citation,),
    )
    content_length = doc_rows[0]["content_length"] if doc_rows else None

    # ── cited_by_count ────────────────────────────────────────────────────
    cite_rows = _sql_dict(
        ["cnt"],
        "SELECT COUNT(*) as cnt FROM case_citations "
        "WHERE cited_citation = %s",
        (citation,),
    )
    cited_by_count = cite_rows[0]["cnt"] if cite_rows else 0

    # ── legislation_refs_count ────────────────────────────────────────────
    leg_rows = _sql_dict(
        ["cnt"],
        "SELECT COUNT(*) as cnt FROM case_legislation_refs "
        "WHERE case_id = %s",
        (case_id,),
    )
    legislation_refs_count = leg_rows[0]["cnt"] if leg_rows else 0

    # ── paragraph_count ───────────────────────────────────────────────────
    para_rows = _sql_dict(
        ["cnt"],
        "SELECT COUNT(*) as cnt FROM case_paragraphs "
        "WHERE case_id = %s",
        (case_id,),
    )
    paragraph_count = para_rows[0]["cnt"] if para_rows else 0

    # ── section_outline (grouped paragraph section_types) ─────────────────
    outline_rows = _sql_dict(
        ["section_type", "cnt"],
        "SELECT section_type, COUNT(*) as cnt FROM case_paragraphs "
        "WHERE case_id = %s "
        "GROUP BY section_type ORDER BY MIN(paragraph_number)",
        (case_id,),
    )

    # ── download URLs ─────────────────────────────────────────────────────
    parsed = _parse_citation(citation)
    download_urls = {}
    if parsed:
        court = str(parsed["court"])
        year = str(parsed["year"])
        num = str(parsed["num"])
        download_urls["austlii_url"] = (
            f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/"
            f"cth/{court}/{year}/{num}.html"
        )

    # ── assemble result ───────────────────────────────────────────────────
    result: dict[str, Any] = {
        "citation": case.get("citation"),
        "case_name": case.get("case_name"),
        "court": case.get("court"),
        "decision_date": case.get("decision_date"),
        "decision_date_note": None,
        "judges": case.get("judges"),
        "outcome": case.get("outcome"),
        "head_notes": case.get("head_notes"),
        "related_provisions": case.get("related_provisions"),
        "related_rulings": case.get("related_rulings"),
        "content_length": content_length,
        "paragraph_count": paragraph_count,
        "cited_by_count": cited_by_count,
        "legislation_refs_count": legislation_refs_count,
        "section_outline": outline_rows,
        "download_urls": download_urls,
    }

    # Try to extract exact date from case_name (AustLII format: "... (10 June 2026)")
    # or from the first line of the document content.
    dd = case.get("decision_date")
    if dd and isinstance(dd, str) and dd.endswith("-01-01"):
        _extracted = _extract_date_from_name_or_content(case, _safe(citation))
        if _extracted:
            result["decision_date"] = _extracted
            result["decision_date_note"] = None
        else:
            result["decision_date_note"] = "Year only — exact date unknown (defaulted to Jan 1)"

    # ── optional legislation refs ─────────────────────────────────────────
    if include_legislation_refs:
        leg_rows = _sql_dict(
            ["act_title", "section_reference", "context", "paragraph_number"],
            "SELECT act_title, section_reference, context, paragraph_number "
            "FROM case_legislation_refs "
            "WHERE case_id = %s ORDER BY paragraph_number",
            (case_id,),
        )
        _fix_itaa1936_act_titles(leg_rows)
        result["legislation_refs"] = leg_rows

    return result


def get_case_references(citation: str) -> dict[str, Any]:
    """Return legislation references and case citations for a case.

    Primary source is the on-disk AI summary (``legislation_cited`` /
    ``cases_cited``) — the lawkitty case store. Falls back to the Postgres
    ``case_legislation_refs`` / ``case_citations`` tables only when they are
    available (internal Cadena system).
    """
    summary = load_case_summary(citation)
    if summary is not None:
        return {
            "legislation_refs": [r for r in summary.get("legislation_cited", []) if r],
            "case_citations": clean_case_citations(summary.get("cases_cited", [])),
            # cited_by reverse index is a deferred build — empty until then.
            "cited_by": [],
        }

    # Get case_id
    id_rows = _sql_dict(
        ["id"],
        "SELECT id FROM cases WHERE citation = %s LIMIT 1",
        (citation,),
    )
    if not id_rows:
        return {"legislation_refs": [], "case_citations": [], "note": "Case not found"}

    case_id = id_rows[0]["id"]
    cid_str = str(case_id)

    # Legislation refs
    leg_rows = _sql_dict(
        ["act_title", "section_reference", "context", "paragraph_number"],
        "SELECT act_title, section_reference, context, paragraph_number "
        "FROM case_legislation_refs "
        "WHERE case_id = %s ORDER BY paragraph_number",
        (cid_str,),
    )
    # Filter AustLII chrome from paragraph_number in legislation refs
    leg_rows = [
        r for r in leg_rows
        if not _is_austlii_chrome(r.get("paragraph_number"))
    ]
    # Fix ITAA 1936 sections mislabelled as ITAA 1997
    _fix_itaa1936_act_titles(leg_rows)

    # Case citations (cases this case cites)
    cite_rows = _sql_dict(
        ["cited_citation", "cited_case_name", "context", "paragraph_number"],
        "SELECT cc.cited_citation, "
        "COALESCE(cc.cited_case_name, c.case_name) AS cited_case_name, "
        "cc.context, cc.paragraph_number "
        "FROM case_citations cc "
        "LEFT JOIN cases c ON cc.cited_citation = c.citation "
        "WHERE cc.citing_case_id = %s ORDER BY cc.paragraph_number",
        (cid_str,),
    )

    # Cases that cite this case
    cited_by_rows = _sql_dict(
        ["citation", "case_name"],
        "SELECT c.citation, c.case_name FROM case_citations cc "
        "JOIN cases c ON c.id = cc.citing_case_id "
        "WHERE cc.cited_citation = %s "
        "GROUP BY c.citation, c.case_name ORDER BY c.citation",
        (citation,),
    )

    # ── Fix 1: Exclude self-citations ────────────────────────────────────
    cite_rows = [r for r in cite_rows if r.get("cited_citation") != citation]
    cited_by_rows = [r for r in cited_by_rows if r.get("citation") != citation]

    # ── Fix 2: Strip AustLII navigation chrome from paragraph_number ─────
    cite_rows = [
        r for r in cite_rows
        if not _is_austlii_chrome(r.get("paragraph_number"))
    ]

    # ── Fix 3: Drop malformed court codes ────────────────────────────────
    def _has_valid_court_code(cit: str) -> bool:
        parsed = _parse_citation(cit)
        if parsed is None:
            return False
        return _is_valid_court_code(str(parsed["court"]))

    cite_rows = [
        r for r in cite_rows
        if _has_valid_court_code(r.get("cited_citation", ""))
    ]

    return {
        "legislation_refs": leg_rows,
        "case_citations": cite_rows,
        "cited_by": cited_by_rows,
    }


# ---------------------------------------------------------------------------
# Tool 2: get_case_paragraphs
# ---------------------------------------------------------------------------


def get_case_paragraphs(
    citation: str,
    section_types: list[str] | None = None,
    paragraph_start: int = 0,
    paragraph_limit: int = 50,
    range_start: int | None = None,
    range_end: int | None = None,
) -> dict[str, Any]:
    """Return paragraphs from a case, filtered and paginated.

    At least one of *section_types* or *range_start* must be provided.
    Hard caps: 100 paragraphs max, 50 000 characters total content.
    """
    # ── validate at least one filter ── otherwise default to first paragraphs
    if not section_types and range_start is None and range_end is None:
        # No filter — return first paragraphs (will be caught by the WHERE clause
        # which already scopes by case_id)
        pass

    paragraph_limit = min(200, max(1, paragraph_limit))

    # ── WHERE clauses ─────────────────────────────────────────────────────
    params: list = [citation]
    wheres = [
        "cp.case_id = (SELECT id FROM cases WHERE citation = %s)"
    ]

    if section_types:
        types = [t for t in section_types if t]
        if types:
            wheres.append("cp.section_type = ANY(%s)")
            params.append(types)

    if range_start is not None:
        wheres.append(f"cp.sequence_order >= {int(range_start)}")
    if range_end is not None:
        wheres.append(f"cp.sequence_order <= {int(range_end)}")

    where_clause = " AND ".join(wheres)

    # ── total_matching count ──────────────────────────────────────────────
    count_rows = _sql_dict(
        ["cnt"],
        f"SELECT COUNT(*) as cnt FROM case_paragraphs cp WHERE {where_clause}",
        tuple(params),
    )
    total_in_case_before_filter = count_rows[0]["cnt"] if count_rows else 0

    # ── total_in_case count ───────────────────────────────────────────────
    case_count_rows = _sql_dict(
        ["cnt"],
        "SELECT COUNT(*) as cnt FROM case_paragraphs cp "
        "WHERE cp.case_id = (SELECT id FROM cases WHERE citation = %s)",
        (citation,),
    )
    total_in_case = case_count_rows[0]["cnt"] if case_count_rows else 0

    # ── fetch paragraphs ──────────────────────────────────────────────────
    rows = _sql_dict(
        [
            "paragraph_number",
            "paragraph_label",
            "section_type",
            "content",
            "sequence_order",
        ],
        "SELECT cp.paragraph_number, cp.paragraph_label, cp.section_type, "
        "REPLACE(cp.content, '|', ' ') as content, cp.sequence_order "
        "FROM case_paragraphs cp "
        f"WHERE {where_clause} "
        "ORDER BY cp.sequence_order NULLS LAST, cp.paragraph_number "
        f"OFFSET {int(paragraph_start)} LIMIT {paragraph_limit}",
        tuple(params),
    )

    # Clean AustLII navigation noise from each paragraph
    filtered_rows = []
    for r in rows:
        content = r.get("content") or ""
        content = clean_case_paragraph(content)
        if not content:
            continue  # skip paragraphs that are pure noise
        r["content"] = content
        filtered_rows.append(r)

    return {
        "citation": citation,
        "total_matching": len(filtered_rows),
        "total_in_case": total_in_case_before_filter,
        "returned_count": len(filtered_rows),
        "truncated": False,
        "warning": (
            "Paragraphs may be segmented mid-sentence. Cross-reference with "
            "the full judgment before citing."
        ),
        "paragraphs": filtered_rows,
    }


# ---------------------------------------------------------------------------
# Tool 3: search_case_paragraphs
# ---------------------------------------------------------------------------


def search_case_paragraphs(
    query: str,
    citation: str | None = None,
    section_types: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Full-text search across case paragraphs using ILIKE.

    If *citation* is provided, search is scoped to that one case.
    If None, searches all cases (cross-case).

    Args:
        query: The search string.
        citation: Optional case citation to scope the search.
        section_types: Optional list of section types to filter.
        limit: Max results. 100 max for within-case, 30 max for cross-case.

    Returns:
        Dict with query, citation_filter, total_matches, results.
    """
    # Escape LIKE wildcards so a literal % or _ in the query matches literally.
    like_q = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    # ── enforce per-mode limits ───────────────────────────────────────────
    if citation:
        limit = min(100, max(1, limit))
    else:
        limit = min(30, max(1, limit))

    # ── build WHERE clauses ───────────────────────────────────────────────
    params: list = ["%" + like_q + "%"]
    wheres = ["cp.content ILIKE %s"]

    if citation:
        wheres.append("c.citation = %s")
        params.append(citation)

    if section_types:
        types = [t for t in section_types if t]
        if types:
            wheres.append("cp.section_type = ANY(%s)")
            params.append(types)

    where_clause = " AND ".join(wheres)

    # ── count total matches ───────────────────────────────────────────────
    count_rows = _sql_dict(
        ["cnt"],
        f"SELECT COUNT(*) as cnt "
        f"FROM case_paragraphs cp "
        f"JOIN cases c ON c.id = cp.case_id "
        f"WHERE {where_clause}",
        tuple(params),
    )
    total_matches = count_rows[0]["cnt"] if count_rows else 0

    # ── fetch results ─────────────────────────────────────────────────────
    rows = _sql_dict(
        [
            "citation",
            "case_name",
            "court",
            "section_type",
            "paragraph_number",
            "snippet",
            "content_length",
            "sequence_order",
        ],
        f"SELECT c.citation, c.case_name, c.court, "
        f"cp.section_type, cp.paragraph_number, "
        f"REPLACE(cp.content, '|', ' ') as content, "
        f"LENGTH(cp.content) as content_length, "
        f"cp.sequence_order "
        "FROM case_paragraphs cp "
        "JOIN cases c ON c.id = cp.case_id "
        f"WHERE {where_clause} "
        "ORDER BY c.citation, cp.sequence_order "
        f"LIMIT {limit}",
        tuple(params),
    )

    # Build centred snippets
    snippet_results = []
    for row in rows:
        content = row.get("snippet") or ""
        idx = content.lower().find(query.lower())
        if idx >= 0:
            window = 150
            start = max(0, idx - window)
            end = min(len(content), idx + len(query) + window)
            snippet = content[start:end]
            if start > 0:
                snippet = "..." + snippet.lstrip()
            if end < len(content):
                snippet = snippet.rstrip() + "..."
        else:
            snippet = content[:300]
        row["snippet"] = snippet
        snippet_results.append(row)

    return {
        "query": query,
        "citation_filter": citation,
        "total_matches": total_matches,
        "results": snippet_results,
    }


# ---------------------------------------------------------------------------
# Tool 4: build_download_urls
# ---------------------------------------------------------------------------


def build_download_urls(citation: str) -> dict[str, Any] | None:
    """Return download links for a case, plus case name.

    URLs are generated from the citation pattern. Does NOT hit the
    database for anything but case_name retrieval.

    Returns:
        Dict with citation, case_name, austlii_url, note; or None if
        the citation cannot be parsed or case not found.
    """
    parsed = _parse_citation(citation)
    if not parsed:
        return None

    court = str(parsed["court"])
    year = str(parsed["year"])
    num = str(parsed["num"])

    # Build AustLII URL
    austlii_url = (
        f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/"
        f"cth/{court}/{year}/{num}.html"
    )

    # Build court-specific URL
    court_url = None
    if court == "HCA":
        court_url = f"https://www.hcourt.gov.au/cases/case_{year}_{num}.html"
    elif court in ("FCA", "FCAFC"):
        court_url = (
            f"https://www.judgments.fedcourt.gov.au/judgment/Judgments/"
            f"{court}/{year}/"
        )

    # Fetch case_name from DB so we can include it
    name_rows = _sql_dict(
        ["case_name", "court", "decision_date"],
        "SELECT case_name, court, decision_date::text "
        "FROM cases WHERE citation = %s LIMIT 1",
        (citation,),
    )
    case_name = name_rows[0].get("case_name") if name_rows else None

    # Fetch paragraph count + content length
    para_count = 0
    content_length = None
    if name_rows:
        case_id_rows = _sql_dict(
            ["id"],
            "SELECT id FROM cases WHERE citation = %s LIMIT 1",
            (citation,),
        )
        if case_id_rows:
            cid = case_id_rows[0]["id"]
            cnt_rows = _sql_dict(
                ["cnt"],
                "SELECT COUNT(*) as cnt FROM case_paragraphs "
                "WHERE case_id = %s",
                (cid,),
            )
            para_count = cnt_rows[0]["cnt"] if cnt_rows else 0
            doc_rows = _sql_dict(
                ["content_length"],
                "SELECT LENGTH(content) as content_length FROM documents "
                "WHERE id = (SELECT document_id FROM cases WHERE citation = %s)",
                (citation,),
            )
            if doc_rows:
                content_length = doc_rows[0]["content_length"]

    return {
        "citation": citation,
        "case_name": case_name,
        "content_length": content_length,
        "paragraph_count": para_count,
        "sources": {
            "text": {
                "url": f"/api/tax-cases/case/{urllib.parse.quote(citation)}/download",
                "fetchable": True,
                "note": "Full judgment. Use this to read or verify.",
            },
            "court": {
                "url": court_url,
                "fetchable": court_url is not None,
                "note": "For citation and human reference. May block non-browser clients.",
            },
            "austlii": {
                "url": austlii_url,
                "fetchable": False,
                "note": "For citation and human reference. Bot-protected, typically 403 to automated clients.",
            },
        },
    }
