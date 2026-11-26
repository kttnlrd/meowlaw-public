"""Tax cases API — simplified search + deprecated tree endpoints."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

from fastapi import APIRouter, Request
from fastapi import Depends
from backend.services.content_access import gate
from fastapi.responses import JSONResponse

from backend.config import DATA_DIR
from backend.services.case_db_service import get_case_metadata, load_case_index_meta, load_case_list_court_url
from backend.services.data_loader import document_licence

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(gate("case"))])

# Map court → data file
COURT_FILES = {
    "hca": DATA_DIR / "hca_tax_cases.json",
    "fca": DATA_DIR / "fca_tax_cases.json",
    "fcafc": DATA_DIR / "fcafc_tax_cases.json",
    "aata": DATA_DIR / "aata_tax_cases.json",
}

COURT_LABELS = {
    "hca": "High Court",
    "fca": "Federal Court",
    "fcafc": "Full Federal Court",
    "aata": "AAT",
}

CITATION_RE = re.compile(r"\[(\d{4})\]\s+(\S+)\s+(\d+)")
COURT_TO_CASE_COURT = {"hca": "HCA", "fca": "FCA", "fcafc": "FCAFC", "aata": "AATA"}
# In-memory cache for all tax cases
_tax_cases_data: list[dict[str, Any]] | None = None
_tax_cases_catchwords: dict[str, str] = {}
_tax_cases_section_refs: dict[str, list[dict]] = {}


def _add_urls(court: str, case: dict) -> dict:
    """Attach austlii_url (and court-specific primary URL) derived from the citation."""
    m = CITATION_RE.match(case.get("citation", ""))
    if m:
        year, case_court, number = m.groups()
        case["austlii_url"] = (
            f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/{case_court}/{year}/{number}.html"
        )
    # HCA judgment page from bulk index href
    href = case.get("href", "")
    if court == "hca" and href:
        case["hca_url"] = f"https://www.hcourt.gov.au{href}"
    # Extract real Federal Court URL from search-system redirect
    raw_url = case.get("url", "")
    if raw_url and "judgments.fedcourt.gov.au" in raw_url:
        try:
            clean = raw_url.replace("&amp;", "&")
            parsed = urlparse(clean)
            qs = parse_qs(parsed.query)
            target = qs.get("url", [""])[0]
            if target:
                case["fedcourt_url"] = target
        except Exception:
            pass
    return case


def _load_all_tax_cases() -> list[dict[str, Any]]:
    """Load and cache all tax cases from disk."""
    global _tax_cases_data
    if _tax_cases_data is not None:
        return _tax_cases_data

    all_cases = []
    for court, path in COURT_FILES.items():
        if path.exists():
            with open(path) as f:
                cases = json.load(f)
            for c in cases:
                c["court_key"] = court
                c["court_label"] = COURT_LABELS.get(court, court)
                c = _add_urls(court, c)
            all_cases.extend(cases)

    # Load catchwords
    cw_path = DATA_DIR / "case_catchwords.json"
    if cw_path.exists():
        with open(cw_path) as f:
            _tax_cases_catchwords.update(json.load(f))

    # Load section refs
    refs_path = DATA_DIR / "case_section_refs.json"
    if refs_path.exists():
        with open(refs_path) as f:
            _tax_cases_section_refs.update(json.load(f))

    # Enrich with catchwords
    for c in all_cases:
        citation = c.get("citation", "")
        if citation in _tax_cases_catchwords:
            c["catchwords"] = _tax_cases_catchwords[citation]
        if citation in _tax_cases_section_refs:
            c["section_refs"] = _tax_cases_section_refs[citation]

    _tax_cases_data = all_cases
    return all_cases


# ---------------------------------------------------------------------------
# PRIMARY: Simplified flat search endpoint
# ---------------------------------------------------------------------------


@router.get("/api/tax-cases/search")
def search_tax_cases(q: str = "", limit: int = 50):
    """Search tax cases by name, citation, or catchwords.

    Returns a flat list of matching cases with weblinks.
    """
    all_cases = _load_all_tax_cases()
    q = q.strip().lower()
    if not q:
        return {"total": len(all_cases), "results": all_cases[:limit]}

    results = []
    for c in all_cases:
        title = (c.get("title") or "").lower()
        citation = (c.get("citation") or "").lower()
        catchwords = (c.get("catchwords") or "").lower()

        if q in title or q in citation or q in catchwords:
            results.append(c)
        elif not q.startswith("["):
            # Fuzzy: check if all query words appear somewhere in title/citation/catchwords
            words = q.split()
            haystack = f"{title} {citation} {catchwords}"
            if all(w in haystack for w in words):
                results.append(c)

    return {
        "total": len(results),
        "results": results[:limit],
    }


@router.get("/api/tax-cases/sidebar")
def tax_cases_sidebar():
    """Lightweight case tree for the sidebar: court -> year -> [{citation, title}]."""
    all_cases = _load_all_tax_cases()
    courts: dict[str, dict[str, list[dict]]] = {}
    for c in all_cases:
        court = c.get("court_key", "")
        year = c.get("citation", "")[1:5]
        if not year:
            continue
        courts.setdefault(court, {}).setdefault(year, []).append(
            {"citation": c.get("citation", ""), "title": c.get("title", "")}
        )

    result = []
    for court in COURT_FILES:
        years = courts.get(court, {})
        year_list = []
        for year in sorted(years, reverse=True):
            cases = sorted(years[year], key=lambda c: c["citation"])
            year_list.append({"year": year, "count": len(cases), "cases": cases})
        result.append({
            "court": court,
            "label": COURT_LABELS.get(court, court),
            "count": sum(y["count"] for y in year_list),
            "years": year_list,
        })
    return {"courts": result}


@router.get("/api/tax-cases/case/{citation:path}/download")
def download_case_html(citation: str):
    """Serve the raw AustLII HTML for a case as a downloadable file."""
    m = CITATION_RE.match(citation)
    if not m:
        return JSONResponse({"error": f"Could not parse citation: {citation}"}, status_code=400)
    year, court, number = m.groups()
    filename = f"{year}_{court}_{number}.html"
    filepath = DATA_DIR / "case_texts" / filename
    if not filepath.exists():
        return JSONResponse({"error": f"Raw HTML not found for {citation}"}, status_code=404)
    from fastapi.responses import FileResponse
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/tax-cases/case/{citation:path}")
def get_tax_case_by_citation(citation: str):
    """Fetch a single case's full detail by exact citation.

    Merges flat JSON metadata with Postgres-backed enriched data.
    """
    # 1. Get flat JSON data (title, catchwords, section_refs, weblinks)
    case = None
    for c in _load_all_tax_cases():
        if c.get("citation") == citation:
            case = dict(c)  # shallow copy so we don't mutate the cache
            break

    # 2. If not in the tax manifests, try the full case-list index (FCA/FCAFC/HCA)
    if not case:
        ci = load_case_index_meta(citation)
        if ci:
            case = {
                "citation": ci.get("citation"),
                "title": ci.get("case_name"),
                "court": ci.get("court"),
                "court_label": ci.get("court"),
                "year": ci.get("year"),
                "subject": ci.get("subject"),
                "npa": ci.get("npa"),
                "catchwords": ci.get("catchwords"),
                "parallel_citations": ci.get("parallel_citations", []),
                "austlii_url": ci.get("austlii_url"),
            }

    # 3. If not in JSON, try Postgres DB directly (covers DB-only cases)
    if not case:
        try:
            enriched = get_case_metadata(citation, include_legislation_refs=True)
            if enriched:
                from backend.services.case_db_service import build_download_urls
                case = {
                    "citation": enriched.get("citation"),
                    "title": enriched.get("case_name"),
                    "court": enriched.get("court"),
                    "court_label": enriched.get("court"),
                    "decision_date": enriched.get("decision_date"),
                    "judges": enriched.get("judges"),
                    "outcome": enriched.get("outcome"),
                    "head_notes": enriched.get("head_notes"),
                    "related_provisions": enriched.get("related_provisions"),
                    "related_rulings": enriched.get("related_rulings"),
                    "content_length": enriched.get("content_length"),
                    "paragraph_count": enriched.get("paragraph_count"),
                    "cited_by_count": enriched.get("cited_by_count"),
                    "legislation_refs_count": enriched.get("legislation_refs_count"),
                    "section_outline": enriched.get("section_outline"),
                    "legislation_refs": enriched.get("legislation_refs"),
                }
                # Extract catchwords from head_notes JSON
                hn = enriched.get("head_notes") or {}
                if isinstance(hn, dict):
                    cw = hn.get("catchwords", [])
                    if isinstance(cw, list):
                        case["catchwords"] = ", ".join(cw)
                    elif isinstance(cw, str):
                        case["catchwords"] = cw
                dl = build_download_urls(citation)
                if dl:
                    case["austlii_url"] = dl.get("austlii_url")
        except Exception as exc:
            logger.warning(f"Could not fetch case {citation} from DB: {exc}")

    if not case:
        return JSONResponse({"error": "Case not found"}, status_code=404)

    # 3. Enrich with Postgres data (best-effort — DB may be unavailable)
    try:
        enriched = get_case_metadata(citation, include_legislation_refs=True)
        if enriched:
            # Merge enriched fields into the case dict (don't overwrite title/citation)
            if enriched.get("case_name") and not case.get("title"):
                case["title"] = enriched["case_name"]
            if enriched.get("court"):
                case["db_court"] = enriched["court"]
            if enriched.get("decision_date"):
                case["decision_date"] = enriched["decision_date"]
            if enriched.get("judges"):
                case["judges"] = enriched["judges"]
            if enriched.get("outcome"):
                case["outcome"] = enriched["outcome"]
            if enriched.get("head_notes"):
                case["head_notes"] = enriched["head_notes"]
            if enriched.get("related_provisions"):
                case["related_provisions"] = enriched["related_provisions"]
            if enriched.get("related_rulings"):
                case["related_rulings"] = enriched["related_rulings"]
            if enriched.get("content_length") is not None:
                case["content_length"] = enriched["content_length"]
            if enriched.get("paragraph_count") is not None:
                case["paragraph_count"] = enriched["paragraph_count"]
            if enriched.get("cited_by_count") is not None:
                case["cited_by_count"] = enriched["cited_by_count"]
            if enriched.get("legislation_refs_count") is not None:
                case["legislation_refs_count"] = enriched["legislation_refs_count"]
            if enriched.get("section_outline"):
                case["section_outline"] = enriched["section_outline"]
            if enriched.get("download_urls"):
                for k, v in enriched["download_urls"].items():
                    if k not in case or not case[k]:
                        case[k] = v
            if enriched.get("legislation_refs"):
                case["legislation_refs"] = enriched["legislation_refs"]
    except Exception as exc:
        logger.warning(f"Could not enrich case {citation} from DB: {exc}")

    court_url = load_case_list_court_url(citation) or case.get("hca_url") or case.get("fedcourt_url")
    lic = document_licence("tax-cases", citation, source_url=court_url)
    if lic:
        case["licence"] = lic
    return case


# ---------------------------------------------------------------------------
# DEPRECATED: Legacy tree endpoints (kept for backward compat)
# ---------------------------------------------------------------------------


@router.get("/api/tax-cases/{court}")
def get_tax_cases(court: str, request: Request):
    """[DEPRECATED] Return tax cases for a given court, grouped by year.
    Use /api/tax-cases/search instead.
    """
    court = court.lower()
    if court not in COURT_FILES:
        return {"error": f"Unknown court '{court}'. Valid: {', '.join(COURT_FILES)}"}, 404

    path = COURT_FILES[court]
    if not path.exists():
        return {"error": f"Data file not found for {court}"}, 500

    with open(path) as f:
        cases = json.load(f)

    all_cases = _load_all_tax_cases()
    cw_map = {c.get("citation", ""): c.get("catchwords") for c in all_cases if c.get("catchwords")}
    refs_map = {c.get("citation", ""): c.get("section_refs") for c in all_cases if c.get("section_refs")}

    # Group by year
    by_year: dict[str, list[dict]] = {}
    for c in cases:
        c = _add_urls(court, c)
        citation = c.get("citation", "")
        if citation in cw_map:
            c["catchwords"] = cw_map[citation]
        if citation in refs_map:
            c["section_refs"] = refs_map[citation]
        year = c.get("citation", "")[1:5]
        if year:
            by_year.setdefault(year, []).append(c)

    sorted_years = sorted(by_year.keys(), reverse=True)
    result = []
    for year in sorted_years:
        year_cases = sorted(by_year[year], key=lambda c: c.get("citation", ""))
        result.append({
            "year": year,
            "count": len(year_cases),
            "cases": year_cases,
        })

    return {
        "court": court,
        "label": COURT_LABELS.get(court, court),
        "total": len(cases),
        "years": result,
        "_deprecated": "Use /api/tax-cases/search instead",
    }


def list_tax_cases_tree() -> dict:
    """Return tax cases as a Tree structure for the frontend sidebar:
    court (part) → year (division) → case (section).
    """
    sidebar = tax_cases_sidebar()
    parts = []
    for court_data in sidebar.get("courts", []):
        divisions = []
        for year_data in court_data.get("years", []):
            sections = [
                {
                    "id": c["citation"],
                    "title": c.get('title', ''),
                    "path": c["citation"],
                }
                for c in year_data.get("cases", [])
            ]
            divisions.append({
                "id": f"{court_data['court']}-{year_data['year']}",
                "title": str(year_data["year"]),
                "subdivisions": [],
                "sections": sections,
            })
        parts.append({
            "id": court_data["court"],
            "title": court_data["label"],
            "divisions": divisions,
            "sections": [],
        })
    return {"act": "Tax Cases", "parts": parts}


@router.get("/api/tax-cases")
def list_tax_case_sources():
    """[DEPRECATED] List available tax case sources. Use /api/tax-cases/search instead."""
    sources = []
    for court, path in COURT_FILES.items():
        if path.exists():
            with open(path) as f:
                cases = json.load(f)
            years = sorted(set(
                c.get("citation", "")[1:5]
                for c in cases
                if len(c.get("citation", "")) > 5
            ), reverse=True)
            sources.append({
                "court": court,
                "label": COURT_LABELS.get(court, court),
                "total": len(cases),
                "years": years,
            })
    return {"sources": sources, "_deprecated": "Use /api/tax-cases/search instead"}


@router.get("/api/section-tax-cases/{act}/{section}")
def get_section_tax_cases(act: str, section: str):
    """[DEPRECATED] Return tax cases that reference a given legislation section.
    Use /api/tax-cases/search instead.
    """
    all_cases = _load_all_tax_cases()
    result = []
    for c in all_cases:
        refs = c.get("section_refs", [])
        for ref in refs:
            if ref.get("act") == act and ref.get("section") == section:
                result.append({
                    "citation": c.get("citation", ""),
                    "court": c.get("court_key", ""),
                    "title": c.get("title", ""),
                    "catchwords": c.get("catchwords", ""),
                })
                break
    return {"act": act, "section": section, "cases": result, "_deprecated": "Use /api/tax-cases/search instead"}
