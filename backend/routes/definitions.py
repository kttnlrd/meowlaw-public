from __future__ import annotations

import logging
import re

from fastapi import HTTPException, APIRouter
from fastapi import Depends
from backend.services.content_access import gate

from backend.services.data_loader import (
    load_definitions,
    load_definitions_with_text,
    get_definition_text,
    get_act_section_content,
    get_definition_across_acts,
    resolve_act_id,
    _all_definition_acts,
)

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(gate("legislation"))])


@router.get("/api/definitions")
def list_definition_acts():
    acts = [
        {"act": act, "count": len(load_definitions(act))}
        for act in sorted(_all_definition_acts())
    ]
    return {"acts": acts}


@router.get("/api/definitions/{act}/search")
def search_definitions(act: str, q: str = ""):
    act = resolve_act_id(act)
    query = q.strip().lower()
    if not query:
        return {"act": act, "query": q, "count": 0, "terms": []}
    matches = [
        item for item in load_definitions_with_text(act)
        if query in item["term"].lower() or query in item["text"].lower()
    ]
    # Term matches first, then text-only matches
    matches.sort(key=lambda item: query not in item["term"].lower())
    matches = matches[:50]
    return {"act": act, "query": q, "count": len(matches), "terms": matches}


@router.get("/api/definitions/{act}")
def get_definitions(act: str):
    act = resolve_act_id(act)
    terms = load_definitions_with_text(act)
    return {"act": act, "count": len(terms), "terms": terms}


@router.get("/api/definition/{act}/{term}")
def get_definition(act: str, term: str):
    # Return the term wherever it is defined, preferring the requested act.
    result = get_definition_across_acts(term, preferred_act=resolve_act_id(act))
    if not result:
        raise HTTPException(status_code=404, detail=f"Definition for '{term}' not found")
    return result


@router.get("/api/definition-text/{act}/{term}")
def get_definition_text_route(act: str, term: str):
    result = get_definition_text(act, term)
    if not result:
        raise HTTPException(status_code=404, detail=f"Definition for '{term}' not found")
    return result


@router.get("/api/section-defined-terms/{act}/{section}")
def section_defined_terms(act: str, section: str):
    """Return defined terms that appear in a section's body text.

    For dictionary sections (6, 195-1, 995-1), terms are returned from the
    definitions index because the raw body uses plain-text definition
    patterns (not wrapped in *asterisks*), which the formatting pipeline
    only wraps at serve time.
    """
    act = resolve_act_id(act)
    defs = load_definitions(act)
    if not defs:
        return {"act": act, "section": section, "count": 0, "terms": []}

    # Dictionary sections: return all terms from the index for this section
    DICT_SECTIONS = frozenset({
        ("itaa-1936", "6"), ("itaa-1997", "995-1"), ("gst-1999", "195-1"),
        ("fbt-1986", "136"), ("taa-1953", "2"), ("sis-1993", "10"),
        ("aml-ctf-2006", "5"), ("nz-it-2007", "YA-1"),
    })
    if (act, section) in DICT_SECTIONS:
        found = [
            {"term": term, "section": info.get("section", ""), "anchor": info.get("anchor", "")}
            for term, info in defs.items()
            if info.get("section") == section
        ]
        found.sort(key=lambda t: len(t["term"]), reverse=True)
        return {"act": act, "section": section, "count": len(found), "terms": found}

    # Non-dictionary sections: scan body text for *italicized* terms
    try:
        fm, body = get_act_section_content(act, section)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error loading section content")
        return {"act": act, "section": section, "count": 0, "terms": []}

    if not body:
        return {"act": act, "section": section, "count": 0, "terms": []}

    found = []
    seen = set()
    for m in re.finditer(r"\*([^*\n]+?)\*", body):
        term = m.group(1).strip()
        key = term.lower()
        if key in defs and key not in seen:
            info = defs[key]
            seen.add(key)
            found.append({
                "term": term,
                "section": info.get("section", ""),
                "anchor": info.get("anchor", ""),
            })
    found.sort(key=lambda t: len(t["term"]), reverse=True)
    return {"act": act, "section": section, "count": len(found), "terms": found}
