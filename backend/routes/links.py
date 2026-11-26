from __future__ import annotations

import logging
import re

from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request

from backend.services.content_access import (
    available_keys, node_allowed, require_act, user_from,
)

from backend.config import DATA_DIR
from backend.services.data_loader import (
    get_smartlinks_for_item,
    get_act_section_content,
    load_definitions,
)
from .cases import get_title_for_item

logger = logging.getLogger(__name__)
router = APIRouter()

# Cache of compiled definition-term alternation regex per act.
# Terms sorted longest-first so multi-word terms win over their prefixes.
_definition_pattern_cache: dict[str, re.Pattern] = {}


def _definition_pattern(act: str, definitions: dict) -> re.Pattern:
    """Return a single compiled regex matching any definition term for an act.

    Built once per act and cached — avoids recompiling ~1,700 patterns on
    every /api/section-refs call (the render-path processors did this and
    cost ~170ms per call).
    """
    pat = _definition_pattern_cache.get(act)
    if pat is not None:
        return pat
    terms = sorted(definitions.keys(), key=len, reverse=True)
    if not terms:
        _definition_pattern_cache[act] = re.compile(r"(?!)")  # never matches
        return _definition_pattern_cache[act]
    alt = "|".join(r"(?<!\w)" + re.escape(t) + r"(?!\w)" for t in terms)
    pat = re.compile(alt, re.IGNORECASE)
    _definition_pattern_cache[act] = pat
    return pat

_SECTION_REF = re.compile(
    r"(?:(?:section|sections|subsection|subsections)\s+|(?<!\w)s\s+)(\d+[A-Z]*[-\d]*)",
    re.IGNORECASE,
)
_DICTIONARY_SECTIONS = {"995-1", "6", "195-1"}
_incoming_cache: dict[str, dict[str, set[str]]] = {}


def _incoming_refs(act: str) -> dict[str, set[str]]:
    """Reverse index {section: {sections in the same act whose text cites it}}.

    A section's own text usually cites few others (s 8-1 cites only s 12-5);
    the useful "related" set is who cites it. Built once per act by scanning
    every section file (~1s for ITAA 1997).
    # ponytail: same-act only, process-lifetime cache; add cross-act/invalidation if data reloads live.
    """
    rev = _incoming_cache.get(act)
    if rev is not None:
        return rev
    rev = defaultdict(set)
    for md in (DATA_DIR / act / "sections").rglob("*.md"):
        me = md.stem.upper()
        if me in _DICTIONARY_SECTIONS:
            continue  # the dictionary cites everything — noise
        text = _clean_markdown_for_analysis(md.read_text(encoding="utf-8"))
        for m in _SECTION_REF.finditer(text):
            ref = m.group(1).upper().rstrip("-")
            if ref != me:
                rev[ref].add(me)
    _incoming_cache[act] = rev
    return rev


# Mapping for cross-act references: full act name (lowercase) to its ID
ACT_NAME_TO_ID = {
    "income tax assessment act 1997": "itaa-1997",
    "income tax assessment act 1936": "itaa-1936",
    "fringe benefits tax assessment act 1986": "fbtaa-1986",
    "superannuation industry (supervision) act 1993": "sis-1993",
    "taxation administration act 1953": "taa-1953",
    "income tax assessment regulation 1997": "itar-1997",
    "a new tax system (goods and services tax) act 1999": "gst-1999",
    "goods and services tax act 1999": "gst-1999",
}


def _clean_markdown_for_analysis(text: str) -> str:
    """Remove code blocks and existing markdown links to prevent false positives."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", "", text)
    return text


@router.get("/api/smart-links/{item_type}/{item_id:path}")
def get_smart_links(item_type: str, item_id: str, request: Request):
    avail = available_keys(user_from(request))
    act = item_id.split("/", 1)[0].split("#", 1)[0] if item_type in ("section", "part") else ""
    if not node_allowed(avail, f"section:{act}" if act else item_type):
        raise HTTPException(404, "Not available")
    if item_type in ("section", "part"):
        item_id = item_id.replace("/", "#")

    links = get_smartlinks_for_item(item_type, item_id)

    links_with_titles = []
    for link in links:
        link_type = link.get("type")
        link_id = link.get("id")
        if link_type and not node_allowed(avail, link_type):
            continue
        if link_type and link_id:
            title = get_title_for_item(link_type, link_id)
            links_with_titles.append({**link, "title": title})
        else:
            links_with_titles.append(link)

    return {
        "item_type": item_type,
        "item_id": item_id,
        "links": links_with_titles,
    }


@router.get("/api/section-refs/{act}/{section}")
def get_section_references(act: str, section: str, request: Request):
    require_act(request, act)  # same-act sections + that act's definitions only
    try:
        fm, body = get_act_section_content(act, section)
    except Exception:
        return {"act": act, "section": section, "sections": [], "definitions": []}

    if not body:
        return {"act": act, "section": section, "sections": [], "definitions": []}

    # Dictionary sections (995-1 / 6 / 195-1) ARE the definition index — the
    # Related panel is meaningless there. Early-return: scanning the 310KB body
    # against section/definition regexes takes ~7s and produces noise.
    if section in ("995-1", "6", "195-1"):
        return {"act": act, "section": section, "sections": [], "definitions": []}

    cleaned = _clean_markdown_for_analysis(body)
    current_upper = section.upper()

    # 1. Same-act section references
    same_act_refs: set[str] = set()

    # "section 8-1", "sections 6-5 and 6-10", "subsection 70-45"
    for m in re.finditer(
        r"(?:section|sections|subsection|subsections)\s+(\d+[A-Z]*[-\d]*)",
        cleaned,
        re.IGNORECASE,
    ):
        ref_id = m.group(1).strip().upper()
        if ref_id != current_upper:
            same_act_refs.add(ref_id)

    # Shorthand "s 8-1" — ensure 's' is standalone
    for m in re.finditer(
        r"(?<!\w)s\s+(\d+[A-Z]*[-\d]*)",
        cleaned,
        re.IGNORECASE,
    ):
        ref_id = m.group(1).strip().upper()
        if ref_id != current_upper:
            same_act_refs.add(ref_id)

    # 2. Cross-act references
    cross_act_refs: set[tuple[str, str]] = set()
    for m in re.finditer(
        r"(?:section|sections|subsection|subsections|\bs)\s+(\d+[A-Z]*[-\d]*)\s+of\s+the\s+([A-Za-z\s()]+Act\s+\d{4})",
        cleaned,
        re.IGNORECASE,
    ):
        ref_section = m.group(1).strip().upper()
        full_name = m.group(2).strip().lower()
        ref_act = ACT_NAME_TO_ID.get(full_name)
        if ref_act and (ref_act != act or ref_section != current_upper):
            cross_act_refs.add((ref_act, ref_section))

    # 3. Definition terms — match terms that appear in the section body.
    # Direct key scan against load_definitions (single cached alternation regex),
    # instead of running the render-path processors (170ms/call, and misses
    # bare [*term*] links emitted by auto_link_definitions when no anchor exists).
    definitions = load_definitions(act)
    found_definitions: dict[str, dict] = {}
    if definitions:
        pat = _definition_pattern(act, definitions)
        # Scan raw body (not cleaned) so term text is intact; protect code blocks/links
        scan_body = re.sub(r"```[\s\S]*?```|`[^`]+`|\[[^\]]+\]\([^)]+\)", " ", body)
        for m in pat.finditer(scan_body):
            key = m.group(0).lower()
            if key in definitions and key not in found_definitions:
                info = definitions[key]
                found_definitions[key] = {
                    "term": info.get("term", key),
                    "section": info.get("section", ""),
                    "anchor": info.get("anchor", ""),
                    "title": info.get("title", key),
                }

    # 4. Build result with titles
    all_sections: list[dict] = []
    for ref_id in sorted(same_act_refs):
        title = get_title_for_item("section", f"{act}#{ref_id}")
        all_sections.append({"id": ref_id, "act": act, "title": title})

    for ref_act, ref_section in sorted(cross_act_refs):
        title = get_title_for_item("section", f"{ref_act}#{ref_section}")
        all_sections.append({"id": ref_section, "act": ref_act, "title": title})

    cited_by = [
        {"id": ref_id, "act": act, "title": get_title_for_item("section", f"{act}#{ref_id}")}
        for ref_id in sorted(_incoming_refs(act).get(current_upper, ()))
    ]

    return {
        "act": act,
        "section": section,
        "sections": all_sections,
        "cited_by": cited_by,
        "definitions": list(found_definitions.values()),
    }
