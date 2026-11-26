from __future__ import annotations

import json
import re
import logging
from pathlib import Path

from fastapi import HTTPException, APIRouter
from fastapi import Depends
from backend.services.content_access import gate

from ..config import DATA_DIR, CASE_DIR
from ..services.data_loader import (
    load_tree, load_cases, get_act_section_content, short_case_name, load_rulings
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(gate("case"))])

@router.get("/api/cases/{act}/{section}")
def cases_for_section(act: str, section: str, limit: int = 50, offset: int = 0):
    from ..services.data_loader import get_cases_for_section
    cases = get_cases_for_section(act, section, limit, offset)
    return {
        "act": act,
        "section": section,
        "count": len(cases),
        "cases": cases,
    }

def _get_section_title(act: str, section_id: str) -> str | None:
    try:
        tree = load_tree(act)
        for part in tree.get("parts", []):
            for sec in part.get("sections", []):
                if sec["id"] == section_id: return sec.get("title")
            for div in part.get("divisions", []):
                for sec in div.get("sections", []):
                    if sec["id"] == section_id: return sec.get("title")
                for sub in div.get("subdivisions", []):
                    for sec in sub.get("sections", []):
                        if sec["id"] == section_id: return sec.get("title")
    except HTTPException:
        pass
    return None

def _get_part_title(act: str, part_id: str) -> str | None:
    try:
        tree = load_tree(act)
        for part in tree.get("parts", []):
            if part["id"] == part_id: return part.get("title")
    except HTTPException:
        pass
    return None

def get_title_for_item(item_type: str, item_id: str) -> str:
    if item_type == "section":
        try:
            act_code, section_id = item_id.split("#")
            title = _get_section_title(act_code, section_id)
            if title: return title
            # Fallback to loading section content if not found in tree
            try:
                fm, _ = get_act_section_content(act_code, section_id)
                return fm.get("title", section_id)
            except HTTPException:
                return section_id
        except ValueError: # Not a valid section ID format
            return section_id
    elif item_type == "case":
        from ..services.data_loader import load_case_name_index
        entry = load_case_name_index().get(item_id)
        return entry["title"] if entry else item_id
    elif item_type == "ruling":
        rulings = load_rulings()
        ruling = next((r for r in rulings if r["citation"] == item_id), None)
        return ruling["title"] if ruling else item_id
    elif item_type == "part":
        try:
            act_code, part_id = item_id.split("#")
            title = _get_part_title(act_code, part_id)
            return title if title else part_id
        except ValueError: # Not a valid part ID format
            return item_id
    return item_id

@router.get("/api/cases")
def list_cases():
    cases = load_cases()
    # Group by category then year
    categories = {"tax": {}, "asic": {}, "other": {}}
    cat_names = {"tax": "Tax Cases", "asic": "ASIC Cases", "other": "Other Cases"}
    for c in cases:
        cat = c.get("category", "other")
        year = c.get("year", 0)
        if year not in categories[cat]:
            categories[cat][year] = []
        categories[cat][year].append(c)
    # Build tree structure: Part=category, Division=year, Section=case
    parts = []
    for cat in ["tax", "asic", "other"]:
        if not categories[cat]:
            continue
        divisions = []
        for year in sorted(categories[cat].keys(), reverse=True):
            divisions.append({
                "id": f"{cat}-{year}",
                "title": str(year),
                "subdivisions": [],
                "sections": [
                    {"id": c["citation"], "title": c["short_name"], "path": c["citation"]}
                    for c in sorted(categories[cat][year], key=lambda x: x["citation"])
                ],
            })
        parts.append({
            "id": cat,
            "title": cat_names[cat],
            "divisions": divisions,
            "sections": [],
        })
    return {"act": "Cases", "parts": parts}

@router.get("/api/case/{citation:path}")
def get_case(citation: str):
    citation = citation.replace("%20", " ").replace("%5B", "[").replace("%5D", "]")
    for f in CASE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("citation") == citation:
                content = data.get("content", "")
                case_name = data.get("case_name", "Unknown")
                body = f"# {case_name}\n\n**Citation:** {data.get('citation', '')}\n\n**Court:** {data.get('court', '')}\n\n**Date:** {data.get('decision_date', '')}\n\n---\n\n{content}"
                return {
                    "frontmatter": {
                        "act": "Cases",
                        "title": case_name,
                        "part": data.get("year"),
                        "division": data.get("court"),
                    },
                    "body": body,
                    "citation": data.get("citation"),
                    "court": data.get("court"),
                    "year": data.get("year"),
                    "source_url": data.get("source_url"),
                    "short_name": short_case_name(case_name),
                }
        except Exception:
            logger.exception(f"Error loading case {f.name}")
    raise HTTPException(status_code=404, detail=f"Case {citation} not found")
