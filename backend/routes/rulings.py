from __future__ import annotations

import functools
import json
import re
import logging
from pathlib import Path

from fastapi import HTTPException, APIRouter, Request
from fastapi import Depends
from backend.services.content_access import gate, require_available
from fastapi.responses import FileResponse

from ..config import NZ_RULING_DIR, RULING_DIR
from ..services.data_loader import (
    load_rulings, get_act_section_content, load_ruling_section_refs, document_licence
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(gate("ruling"))])

@router.get("/api/rulings/{act}/{section}")
def rulings_for_section(act: str, section: str, limit: int = 50, offset: int = 0):
    from ..services.data_loader import get_rulings_for_section
    rulings = get_rulings_for_section(act, section, limit, offset)
    ruling_list = load_rulings()
    richer_rulings = []
    for r in rulings:
        found = next((item for item in ruling_list if item["citation"] == r["citation"]), None)
        if found:
            # copy — load_rulings() is cached, don't mutate the shared manifest
            richer_rulings.append({
                **found,
                "download_url": f"/api/ruling/{found['citation']}/download",
            })
        else:
            # Include basic info even without full manifest entry
            # Extract year from citation when possible (e.g. "TR_2024_1" → 2024)
            fallback_year = 0
            yr_m = re.match(r'^[A-Za-z]+_(\d{2,4})_', r["citation"])
            if yr_m:
                fallback_year = int(yr_m.group(1))
                if fallback_year < 100:
                    fallback_year += 1900 if fallback_year >= 90 else 2000
            richer_rulings.append({
                "citation": r["citation"],
                "title": r.get("title", r["citation"]),
                "type": "ruling",
                "year": fallback_year,
                "ato_url": "",
                "download_url": f"/api/ruling/{r['citation']}/download",
            })
    return {
        "act": act,
        "section": section,
        "count": len(richer_rulings),
        "rulings": richer_rulings,
    }

TYPE_DISPLAY: dict[str, str] = {
    "ATOID": "ATO ID – ATO Interpretative Decision",
    "CR": "CR – Class Ruling",
    "GSTR": "GSTR – GST Ruling",
    "IT": "IT – Income Tax Ruling",
    "LCG": "LCG – Law Companion Guideline",
    "MT": "MT – Miscellaneous Tax Ruling",
    "PCG": "PCG – Practical Compliance Guideline",
    "PR": "PR – Product Ruling",
    "PS LA": "PS LA – Practice Statement (Law Administration)",
    "SGR": "SGR – Superannuation Guarantee Ruling",
    "TA": "TA – Taxpayer Alert",
    "TD": "TD – Tax Determination",
    "TR": "TR – Tax Ruling",
}

def _norm_title(s: str) -> str:
    """Lowercase and drop every non-alphanumeric character — for comparisons."""
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


# ── date_of_effect guard (LK-02) ─────────────────────────────────────────────
# The summary generator used to ask for "YYYY-MM-DD or description"; the model
# invented `YYYY-01-01` placeholders when the ruling stated no such date
# (TR 2024/1 → '2024-01-01' while the source only says the ruling applies to
# years before and after its 31 January 2024 issue date). The prompt is now
# null-when-unknown, but existing summary JSONs still carry bad values, so drop
# a January-1 placeholder at serve time unless the source corroborates it.
_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_ISO_DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')


def _date_appears_in_source(iso: str, source: str) -> bool:
    """True when an ISO day-precision date is corroborated by the source text."""
    m = _ISO_DATE_RE.match(iso)
    if not m:
        return True
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return False
    month, abbr = _MONTH_NAMES[mo - 1], _MONTH_NAMES[mo - 1][:3]
    # Only day-bearing forms corroborate a day-precision date — a bare
    # "January 2024" must NOT match an unrelated "... 31 January 2024".
    forms = (
        iso,
        f"{d} {month} {y}", f"{d:02d} {month} {y}",
        f"{d} {abbr} {y}", f"{d:02d} {abbr} {y}",
        f"{d}/{mo}/{y}", f"{d:02d}/{mo:02d}/{y}",
        f"{d}.{mo:02d}.{y}", f"{d:02d}.{mo:02d}.{y}",
    )
    for form in forms:
        # Digit/punct boundaries so "1 January 2024" does not match inside
        # "... 31 January 2024".
        if re.search(r'(?<![\d/.\-])' + re.escape(form) + r'(?![\d/.\-])', source):
            return True
    return False


def _sanitize_date_of_effect(value, source_text: str):
    """Drop a placeholder `YYYY-01-01` date_of_effect the source does not state.

    Returns the original value when it is a description, is null/empty, or is
    corroborated by the source; otherwise returns None (unknown).
    """
    if not value or not isinstance(value, str):
        return value if value != "" else None
    v = value.strip()
    m = _ISO_DATE_RE.match(v)
    if not m:
        return value  # a human-readable description — pass through
    # The invented value is always 1 January; only guard that shape.
    if not v.endswith("-01-01"):
        return value
    if source_text and _date_appears_in_source(v, source_text):
        return value
    logger.info("Dropped uncorroborated date_of_effect %r (LK-02)", v)
    return None


def _is_citation_only(desc: str, base: str, citation: str) -> bool:
    """True when the description IS the citation (raw stem or display form).

    CDN-0204: two rulings carry the ATOID stem as their description
    ('AID_2001_497'); those must not render as 'ATOID 2001/497 — AID_2001_497'.
    """
    d = _norm_title(desc)
    return not d or d in {_norm_title(base), _norm_title(citation)}


def _tree_title(r: dict) -> str:
    """Short title for tree sidebar — citation + truncated description.

    CDN-0204 regression: this compared `full_title != title`, so the 32 rulings
    whose recovered `full_title` equals `title` (e.g. GSTR 2000/17, 'Goods and
    services tax: tax invoices') rendered as a bare citation — the tree went from
    332 to 364/365 bare titles when 3b715b745 landed. The comparison is against
    the citation/stem instead: append the description whenever it actually says
    something the citation does not.
    """
    base = r.get("citation_display", r["citation"])
    desc = (r.get('full_title') or r.get('title') or '').strip()
    if desc and 'Legal database' not in desc and not _is_citation_only(desc, base, r.get("citation", "")):
        # Truncate to 120 chars for tree display
        short = desc[:120].rsplit(' ', 1)[0] if len(desc) > 120 else desc
        base = f"{base} — {short}"
    if r.get('withdrawn'):
        base += "  [WITHDRAWN]"
    return base


@router.get("/api/rulings-list")
def list_rulings(group: str = "year", jurisdiction: str | None = None,
                 act_title: str = "ATO public rulings"):
    """
    List all ATO rulings grouped by year or by ruling type.

    Parameters:
    - group: "year" (default) → Year → Type → Rulings
            "type"           → Type → Year → Rulings
    """
    rulings = load_rulings()
    if jurisdiction:
        # "AU" means the rows that carry no jurisdiction marker; NZ rows are tagged by
        # the loader. Without this the UI cannot separate the two corpora.
        want = jurisdiction.strip().upper()
        rulings = [r for r in rulings
                   if (str(r.get("jurisdiction") or "AU").upper() == want)]
    years = {}
    for r in rulings:
        year = r.get("year", 0)
        t = r.get("type", "Ruling")
        if year not in years:
            years[year] = {}
        if t not in years[year]:
            years[year][t] = []
        years[year][t].append(r)

    def ruling_sort_key(r):
        m = re.search(r'(\d+)$', r["citation"])
        return int(m.group(1)) if m else 0

    if group == "type":
        # Group: Type → Year → Rulings
        types: dict[str, dict] = {}
        for year, type_dict in years.items():
            for t, secs in type_dict.items():
                if t not in types:
                    types[t] = {}
                types[t][year] = secs

        parts = []
        for t in sorted(types.keys()):
            year_divs = []
            for year in sorted(types[t].keys(), reverse=True):
                sections = sorted(types[t][year], key=ruling_sort_key)
                year_divs.append({
                    "id": f"{t.lower().replace(' ', '-')}-{year}",
                    "title": str(year),
                    "subdivisions": [],
                    "sections": [
                        {
                            "id": r["citation"],
                            "title": _tree_title(r),
                            "jurisdiction": str(r.get("jurisdiction") or "AU").upper(),
                            "path": r["citation"],
                            "ato_url": r.get("ato_url", ""),
                            "austlii_url": r.get("austlii_url", ""),
                            "download_url": f"/api/ruling/{r['citation']}/download",
                        }
                        for r in sections
                    ],
                })
            parts.append({
                "id": t.lower().replace(' ', '-'),
                "title": TYPE_DISPLAY.get(t, t),
                "divisions": year_divs,
                "sections": [],
            })
    else:
        # Default: Year → Type → Rulings
        parts = []
        for year in sorted(years.keys(), reverse=True):
            divisions = []
            for t in sorted(years[year].keys()):
                sections = sorted(years[year][t], key=ruling_sort_key)
                divisions.append({
                    "id": f"{year}-{t.lower().replace(' ', '-')}",
                    "title": TYPE_DISPLAY.get(t, t),
                    "subdivisions": [],
                    "sections": [
                        {
                            "id": r["citation"],
                            "title": _tree_title(r),
                            "jurisdiction": str(r.get("jurisdiction") or "AU").upper(),
                            "path": r["citation"],
                            "ato_url": r.get("ato_url", ""),
                            "austlii_url": r.get("austlii_url", ""),
                            "download_url": f"/api/ruling/{r['citation']}/download",
                        }
                        for r in sections
                    ],
                })
            parts.append({
                "id": str(year),
                "title": "IT Rulings" if year == 0 else str(year),
                "divisions": divisions,
                "sections": [],
            })
    return {"act": act_title, "parts": parts}

CITATION_ALIASES = {"LCR": "LCG", "AID": "ATOID"}

_FOI_RE = re.compile(r'^FOI\s+status\s*:.*$', re.IGNORECASE | re.MULTILINE)

def _strip_foi(text: str) -> str:
    """Remove FOI status lines from text."""
    return _FOI_RE.sub('', text).strip()

def _extract_decision(body: str) -> str:
    """Extract the Yes/No decision from an ATOID body."""
    m = re.search(r'Decision\s*\n(.*?)(?:\n\n|\n[A-Z][a-z]+\s*\n|\Z)', body, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""

def _norm_citation(citation: str) -> str:
    """Citation to stem form: "TR 2020/1" and "BR Prd 26/01" both to underscore form.

    The NZ manifest keeps `citation` in display form ("BR Prd 04/01") while every lookup
    key - summary file stems and the `candidates` sets below - is the underscore stem.
    Comparing a raw manifest citation against a normalised candidate set therefore never
    matched, and 1,441 of 1,596 NZ rulings 404'd on both detail and download (measured
    2026-10-13). Normalising both sides fixes the class without per-row edits; AU
    citations are already underscore stems, so for them this is a no-op.
    """
    return re.sub(r'[\s/]+', '_', citation.replace("%20", " ")).strip('_')


def _is_nz_citation(citation: str) -> bool:
    """True when a citation resolves to an NZ IRD ruling (jurisdiction == NZ)."""
    norm = _norm_citation(citation)
    for r in load_rulings():
        if _norm_citation(r["citation"]) == norm or Path(str(r.get("source") or "")).stem == norm:
            return str(r.get("jurisdiction", "")).upper() == "NZ"
    return False


@router.get("/api/ruling/{citation:path}/download")
def download_ruling(citation: str, request: Request):
    citation = citation.replace("%20", " ")
    if _is_nz_citation(citation):
        require_available(request, "nz-ruling")
    normalized = _norm_citation(citation)
    candidates = {normalized}
    # Alias resolution: AID → ATOID, LCR → LCG
    prefix_m = re.match(r'^([A-Za-z]+)_(.*)$', normalized)
    if prefix_m and prefix_m.group(1).upper() in CITATION_ALIASES:
        candidates.add(f"{CITATION_ALIASES[prefix_m.group(1).upper()]}_{prefix_m.group(2)}")
    for r in load_rulings():
        if _norm_citation(r["citation"]) in candidates \
                or Path(str(r.get("source") or "")).stem in candidates:
            path = Path(r["source"])
            if path.exists():
                return FileResponse(
                    path=path,
                    filename=path.name,
                    media_type={
                        ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
                        ".pdf": "application/pdf", ".xml": "application/xml",
                    }.get(path.suffix.lower(), "text/plain; charset=utf-8"),
                    headers={"Content-Disposition":
                             ('inline; filename="%s"' if path.suffix.lower() in (".html", ".htm", ".pdf")
                              else 'attachment; filename="%s"') % path.name},
                )
    raise HTTPException(status_code=404, detail=f"Ruling {citation} not found")


@router.get("/api/ruling/{citation:path}")
def get_ruling(citation: str, request: Request):
    if _is_nz_citation(citation):
        require_available(request, "nz-ruling")
    data = _ruling_payload(citation)
    return {**data, "licence": document_licence("rulings", source_url=data.get("ato_url") or None)}


def _ruling_payload(citation: str):
    import re as _re
    citation = citation.replace("%20", " ")
    # Normalize: "TR 2020/1" → "TR_2020_1"
    normalized = _re.sub(r'[\s/]+', '_', citation).strip('_')
    candidates = {normalized}
    prefix_m = _re.match(r'^([A-Za-z]+)_(.*)$', normalized)
    if prefix_m and prefix_m.group(1).upper() in CITATION_ALIASES:
        candidates.add(f"{CITATION_ALIASES[prefix_m.group(1).upper()]}_{prefix_m.group(2)}")
        # Also add the reverse: if normalized is ATOID_2016_1, also check AID_2016_1
        alias_val = CITATION_ALIASES[prefix_m.group(1).upper()]
        if alias_val != prefix_m.group(1).upper():
            candidates.add(f"{prefix_m.group(1).upper()}_{prefix_m.group(2)}")
    # Also check reverse aliases: if citing ATOID_2016_1, also try AID_2016_1
    for alias_src, alias_dst in list(CITATION_ALIASES.items()):
        if alias_src != alias_dst and normalized.startswith(alias_dst + '_'):
            suffix = normalized[len(alias_dst) + 1:]
            candidates.add(f"{alias_src}_{suffix}")

    from ..services.data_loader import load_rulings as _load_rulings
    rulings_index = _load_rulings()

    # A stem request (BRPRD_2004_1, the form the tree and the graph use) has to reach the
    # same record as the citation form (BR Prd 04/01). Summaries are keyed by the citation
    # form, so resolve the stem through the manifest before the summary lookup.
    for _row in rulings_index:
        if Path(str(_row.get("source") or "")).stem == normalized:
            candidates.add(_norm_citation(_row["citation"]))
            break

    # First check for a structured summary. Australian summaries live under
    # data/rulings/summaries; the New Zealand ones land in data/nz_rulings/summaries from
    # scripts/run_nz_summary_fanout.py and are keyed by the same citation form as
    # `candidates`. Only the AU directory was searched, so every NZ summary written would
    # have stayed invisible.
    SUMMARY_DIRS = (RULING_DIR / "summaries", NZ_RULING_DIR / "summaries")
    referenced_sections = load_ruling_section_refs(citation)
    for ref in candidates:
        summary_path = next(
            (d / f"{ref}.json" for d in SUMMARY_DIRS if (d / f"{ref}.json").exists()),
            None,
        )
        if summary_path is not None:
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))

                # Build ATO URL
                ato_url = ""
                source_text = ""
                for r in rulings_index:
                    if _norm_citation(r["citation"]) in candidates:
                        ato_url = r.get("ato_url", "")
                        try:
                            source_text = Path(r["source"]).read_text(
                                encoding="utf-8", errors="replace")
                        except OSError:
                            source_text = ""
                        break

                # For ATO IDs, return structured data with the full body text
                # (ATOIDs are short — render them in full, summary box on top)
                if summary.get("type") == "ATO Interpretative Decision" or summary.get("full_text"):
                    body_raw = summary.get("body", "")
                    decision = _extract_decision(body_raw)
                    return {
                        "frontmatter": {
                            "act": "Public Rulings",
                            "title": summary.get("title", ref),
                            "part": "ATO ID",
                            "division": summary.get("subject", ""),
                        },
                        "citation": summary.get("citation", ref),
                        "descriptive_title": summary.get("title", ""),
                        "question": summary.get("question", ""),
                        "decision": decision,
                        "body": body_raw,
                        "type": "ATO ID",
                        "status": summary.get("status", "Final"),
                        "cases_referenced": summary.get("cases_referenced", []),
                        "legislation_referenced": summary.get("legislation_referenced", []),
                        "ato_url": ato_url,
                        "referenced_sections": referenced_sections,
                        "download_url": f"/api/ruling/{citation}/download",
                    }

                # For full rulings, return structured data (summary only, no full body)
                notice = _strip_foi(summary.get("notice", ""))
                return {
                    "frontmatter": {
                        "act": "Public Rulings",
                        "title": summary.get("title", ref),
                        "part": summary.get("type", ""),
                        "division": "",
                    },
                    "citation": summary.get("citation", ref),
                    "descriptive_title": summary.get("title", ""),
                    "subject": summary.get("subject", ""),
                    "background": summary.get("background", ""),
                    "ruling": summary.get("ruling", ""),
                    "notice": notice,
                    "type": summary.get("type", ""),
                    "status": summary.get("status", ""),
                    "date_of_effect": _sanitize_date_of_effect(
                        summary.get("date_of_effect", ""), source_text),
                    "cases_referenced": summary.get("cases_referenced", []),
                    "legislation_referenced": summary.get("legislation_referenced", []),
                    "related_rulings": summary.get("related_rulings", []),
                    "ato_url": ato_url,
                    "referenced_sections": referenced_sections,
                    "download_url": f"/api/ruling/{citation}/download",
                }
            except Exception:
                pass  # Fall through to raw text

    # Fall back to raw text from flat files — metadata only, no full body
    for r in rulings_index:
        if _norm_citation(r["citation"]) in candidates \
                or Path(str(r.get("source") or "")).stem in candidates:
            path = Path(r["source"])
            if path.exists():
                referenced_sections = load_ruling_section_refs(citation)
                # NZ rows carry their own descriptive title in the manifest; the loader's
                # `title` for them is the file stem ("BRPRD_2004_1"), which is not a title.
                served_title = (r.get("full_title") or r["title"]) \
                    if str(r.get("jurisdiction", "")).upper() == "NZ" else r["title"]
                return {
                    "frontmatter": {
                        "act": "Public Rulings",
                        "title": served_title,
                        "part": r["type"],
                        "division": str(r["year"]),
                    },
                    "descriptive_title": served_title,
                    "citation": r["citation"],
                    "type": r["type"],
                    "year": r["year"],
                    "ato_url": r.get("ato_url", ""),
                    "referenced_sections": referenced_sections,
                    "download_url": f"/api/ruling/{citation}/download",
                }
    raise HTTPException(status_code=404, detail=f"Ruling {citation} not found")

@router.get("/api/ruling-sections/{citation:path}")
def get_ruling_sections(citation: str):
    citation = citation.replace("%20", " ")
    referenced_sections = load_ruling_section_refs(citation)
    
    sections_with_titles = []
    for ref in referenced_sections:
        act = ref["act"]
        section = ref["section"]
        
        try:
            fm, body = get_act_section_content(act, section)
            sections_with_titles.append({
                "act": act,
                "section": section,
                "title": fm.get("title", section),
                "full_title": fm.get("full_title", fm.get("title", section)),
            })
        except HTTPException as e:
            logger.warning(f"Could not retrieve content for {act} {section}: {e.detail}")
            sections_with_titles.append({
                "act": act,
                "section": section,
                "title": f"Section {section} (Title not found)",
                "full_title": f"Section {section} (Title not found)",
            })
    return {
        "citation": citation,
        "referenced_sections": sections_with_titles,
    }