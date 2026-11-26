from __future__ import annotations

import logging
import re
import functools

from fastapi import APIRouter, Request

from backend.services.content_access import act_key, available_keys, require_act, user_from

from backend.services.data_loader import load_tree, load_acts_meta, get_act_section_content, licence_notice
from backend.processors.markdown import (
    link_definitions, format_definition_terms,
    link_section_references, link_cross_act_references, auto_link_definitions,
)
from backend.services.text_cleaner import strip_scraped_markup

from .rulings import list_rulings, get_ruling
from .tax_cases import list_tax_cases_tree, get_tax_case_by_citation
from .regulatory_guides import list_regulatory_guides, get_regulatory_guide
from .afsa_guides import list_afsa_guides, get_afsa_guide

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/acts")
def list_acts(request: Request, jurisdiction: str | None = None):
    # Copy — load_acts_meta() is lru_cached; appending pseudo-acts to the
    # cached list would grow it on every call.
    acts = list(load_acts_meta())
    acts.append({"id": "rulings", "name": "Public Rulings", "compilation_no": None, "compilation_date": None})
    acts.append({"id": "tax-cases", "name": "Tax Cases", "compilation_no": None, "compilation_date": None})
    acts.append({"id": "private-rulings", "name": "Private Rulings", "compilation_no": None, "compilation_date": None})
    acts.append({"id": "regulatory-guides", "name": "ASIC Regulatory Guides", "compilation_no": None, "compilation_date": None})
    acts.append({"id": "afsa-guides", "name": "AFSA Practice Guidance", "compilation_no": None, "compilation_date": None})
    acts.append({"id": "treaties", "name": "Tax Treaties", "compilation_no": None, "compilation_date": None})
    if jurisdiction:
        want = jurisdiction.strip().upper()
        acts = [a for a in acts if str(a.get("jurisdiction") or "").upper() == want]
    avail = available_keys(user_from(request))
    return [a for a in acts if act_key(a["id"]) in avail]


_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _build_private_rulings_tree() -> dict:
    """Years → months → rulings as a Tree for the sidebar.

    Divisions are years; subdivisions are months (with counts). Sections are
    empty — the per-month ruling list loads on demand via
    /api/private-rulings?year=&month= when a month is expanded. 57,608
    sections inline would make the tree ~15MB.
    """
    from .private_rulings import _load_index

    idx = _load_index()
    by_ym: dict[tuple[int, int], int] = {}
    undated = 0
    for meta in idx.values():
        m = re.match(r"(\d{4})-(\d{2})", meta.get("date_of_advice") or "")
        if m:
            key = (int(m.group(1)), int(m.group(2)))
            by_ym[key] = by_ym.get(key, 0) + 1
        else:
            undated += 1
    years: dict[int, list[tuple[int, int]]] = {}
    for (y, m), c in by_ym.items():
        years.setdefault(y, []).append((m, c))
    divisions = []
    for y in sorted(years, reverse=True):
        months = sorted(years[y], reverse=True)
        subdivisions = [
            {
                "id": f"{y}-{m}",
                "title": f"{_MONTH_NAMES[m - 1]} ({c})",
                "sections": [],
            }
            for m, c in months
        ]
        divisions.append({
            "id": str(y),
            "title": f"{y} ({sum(c for _, c in months)})",
            "subdivisions": subdivisions,
            "sections": [],
        })
    if undated:
        divisions.append({
            "id": "undated",
            "title": f"Undated ({undated})",
            "subdivisions": [{
                "id": "undated-all",
                "title": f"Undated ({undated})",
                "sections": [],
            }],
            "sections": [],
        })
    return {
        "act": "Private Rulings",
        "parts": [{
            "id": "private-rulings",
            "title": "Private Rulings",
            "divisions": divisions,
            "sections": [],
        }],
    }


@router.get("/api/tree/{act}")
def get_tree(act: str, request: Request):
    require_act(request, act)  # mixed router: gate per act, not per file
    if act == "rulings":
        return list_rulings()
    if act == "nz-rulings":
        # Same shape as the ATO rulings tree, filtered to the NZ corpus, so the
        # existing rulings list UI renders it with no component changes.
        return list_rulings(jurisdiction="NZ", act_title="NZ tax rulings")
    if act == "tax-cases":
        return list_tax_cases_tree()
    if act == "private-rulings":
        return _build_private_rulings_tree()
    if act == "regulatory-guides":
        return list_regulatory_guides()
    if act == "afsa-guides":
        return list_afsa_guides()
    lic = licence_notice(act)
    # Copy before adding: load_tree() is lru_cached.
    return {**load_tree(act), "licence": lic} if lic else load_tree(act)


@router.get("/api/section/{act}/{section:path}")
def get_section(act: str, section: str, request: Request):
    require_act(request, act)
    if act in ("rulings", "nz-rulings"):
        return get_ruling(section, request)

    if act == "tax-cases":
        return get_tax_case_by_citation(section)

    if act == "private-rulings":
        from .private_rulings import private_ruling_detail
        return private_ruling_detail(section)

    if act == "regulatory-guides":
        return get_regulatory_guide(int(section))

    if act == "afsa-guides":
        return get_afsa_guide(section)

    # Strip leading 's' prefix from section id if present (e.g. s8-1 → 8-1).
    # Sections are stored WITHOUT the s prefix in the data files.
    # Digit lookahead guard: genuine ids like 'schedule-2' (itaa-1936) must NOT be stripped.
    if section.startswith('s') and len(section) > 1 and section[1].isdigit():
        section = section[1:]

    fm, body = _render_section(act, section)

    result = {"frontmatter": fm, "body": body}
    lic = licence_notice(act)
    if lic:
        result["licence"] = lic

    # Definition-library hint for dictionary sections (mirror of the MCP
    # get_section note). Every act's dictionary section gets a hint directing
    # callers to the per-term definition library instead of the raw run-on text.
    _DICT_SECTIONS = {
        "itaa-1997": {"995-1": ("the ITAA 1997", "s995-1")},
        "itaa-1936": {"317": ("Part X (CFC measures) of the ITAA 1936", "s317"), "6": ("the ITAA 1936", "s6")},
        "gst-1999": {"195-1": ("the GST Act", "s195-1")},
        "fbt-1986": {"136": ("the FBT Act", "s136")},
        "taa-1953": {"2": ("the TAA 1953", "s2")},
        "sis-1993": {"10": ("the SIS Act", "s10")},
        "aml-ctf-2006": {"5": ("the AML/CTF Act", "s5")},
        "nz-it-2007": {"YA-1": ("the NZ IT Act 2007", "YA 1")},
        "corporations-act-2001": {"9": ("the Corporations Act 2001", "s9")},
    }
    dict_info = _DICT_SECTIONS.get(act, {}).get(section)
    if dict_info:
        label, ref = dict_info
        result["hint"] = (
            f"This is an interpretation/definitions section for {label} ({ref}). "
            f"Use the definition library: /api/definitions/{act} (all terms) or "
            f"/api/definition/{act}/TERM (single term) instead of reading the "
            f"full section text."
        )

    # Include ASIC Regulatory Guides for corps act sections
    avail = available_keys(user_from(request))
    if act == "corporations-act-2001" and "regulatory-guide" in avail:
        try:
            from .regulatory_guides import get_rgs_for_section
            rg_result = get_rgs_for_section(act, section)
            if rg_result["count"] > 0:
                result["regulatory_guides"] = rg_result["regulatory_guides"]
        except Exception:
            pass

    # Include AFSA practice guidance for bankruptcy act sections
    if act == "bankruptcy-act-1966" and "afsa-guide" in avail:
        try:
            from .afsa_guides import get_afsa_for_section
            afsa_result = get_afsa_for_section(act, section)
            if afsa_result["count"] > 0:
                result["afsa_guides"] = afsa_result["afsa_guides"]
        except Exception:
            pass

    return result


@functools.lru_cache(maxsize=None)
def _render_section(act: str, section: str) -> tuple[dict, str]:
    """Render a section's body (definition/formatter passes) — pure function of
    (act, section), so cache it. Frontmatter and body are read-only downstream;
    callers never mutate them."""
    fm, body = get_act_section_content(act, section)

    # Strip scraped markup FIRST (CDN-0094) — clean artifacts before processors inject anchors
    body = strip_scraped_markup(body)

    # Then run definition/formatters — these inject <a id> anchors that survive cleanup
    body = format_definition_terms(body, section, act)
    body = link_definitions(body, act)
    body = link_section_references(body, act)
    body = link_cross_act_references(body, act)
    body = auto_link_definitions(body, act, section)
    return fm, body


