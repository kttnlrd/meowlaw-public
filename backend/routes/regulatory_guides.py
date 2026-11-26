"""ASIC Regulatory Guides — browse, search, download.

RGs are standalone guidance documents published by ASIC. Unlike ATO rulings
they don't map to specific legislation sections, so this module is simpler:
a flat list grouped by status, a detail view with full text, and PDF download
from our server plus the direct ASIC source link.
"""
from __future__ import annotations

import json
import re
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi import Depends
from backend.services.content_access import gate
from fastapi.responses import FileResponse, RedirectResponse

from ..config import DATA_DIR
from ..services.data_loader import document_licence

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(gate("regulatory-guide"))])

RG_DIR = DATA_DIR / "regulatory-guides"
MANIFEST_PATH = RG_DIR / "rg_manifest.json"
PDF_DIR = RG_DIR / "pdfs"
TEXT_DIR = RG_DIR / "texts"
SUMMARY_DIR = RG_DIR / "summaries"
PARAGRAPHS_DIR = RG_DIR / "paragraphs"
_SECTION_INDEX: dict[str, list[dict]] | None = None
_REVERSE_SECTION_INDEX: dict[str, list[str]] | None = None

def _load_rg_section_index() -> dict[str, list[dict]]:
    global _SECTION_INDEX
    if _SECTION_INDEX is None:
        path = DATA_DIR / "rg_section_index.json"
        if path.exists():
            _SECTION_INDEX = json.loads(path.read_text(encoding="utf-8"))
        else:
            _SECTION_INDEX = {}
    return _SECTION_INDEX

def _load_reverse_section_index() -> dict[str, list[str]]:
    global _REVERSE_SECTION_INDEX
    if _REVERSE_SECTION_INDEX is None:
        path = DATA_DIR / "section_rg_index.json"
        if path.exists():
            _REVERSE_SECTION_INDEX = json.loads(path.read_text(encoding="utf-8"))
        else:
            _REVERSE_SECTION_INDEX = {}
    return _REVERSE_SECTION_INDEX

_STATUS_DISPLAY = {
    "current": "Current",
    "withdrawn": "Withdrawn",
    "no_pdf": "No PDF available",
    "unavailable": "Unavailable",
}

_MANIFEST_CACHE = None


def _load_manifest() -> list[dict]:
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is None:
        if not MANIFEST_PATH.exists():
            raise HTTPException(status_code=404, detail="Regulatory guide manifest not found")
        _MANIFEST_CACHE = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return _MANIFEST_CACHE


def _find_rg(rg_number: int) -> dict:
    for rg in _load_manifest():
        if rg["rg_number"] == rg_number:
            return rg
    raise HTTPException(status_code=404, detail=f"Regulatory Guide RG {rg_number} not found")


def _clean_text(text: str) -> str:
    """Strip repeated page-header/footer debris from extracted PDF text."""
    # Remove form-feed page breaks and consecutive page-header footer lines
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip()
        if not s:
            cleaned.append("")
            continue
        # Drop the repeated "REGULATORY GUIDE N: <title>" page footer / header
        if re.match(r"^REGULATORY GUIDE \d+.*$", s, re.IGNORECASE) and cleaned and cleaned[-1] == s:
            continue
        cleaned.append(s)
    out = "\n".join(cleaned)
    # Collapse 3+ blank lines to 2
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


_PARAGRAPHS_CACHE: dict[int, dict | None] = {}


def _load_paragraphs(rg_number: int) -> dict | None:
    """Load the paragraph-split JSON for one RG (cached), or None if unavailable."""
    if rg_number not in _PARAGRAPHS_CACHE:
        path = PARAGRAPHS_DIR / f"RG_{rg_number:03d}.json"
        data: dict | None = None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
        _PARAGRAPHS_CACHE[rg_number] = data
    return _PARAGRAPHS_CACHE[rg_number]


def get_rg_paragraphs(rg_number: int) -> list[dict] | None:
    """Return the ordered paragraph list for an RG, or None if not split."""
    data = _load_paragraphs(rg_number)
    if not data:
        return None
    return [
        {"num": p.get("num"), "text": (p.get("text") or "").strip()}
        for p in data.get("paragraphs", [])
    ]


def get_rg_paragraph(rg_number: int, para: str) -> dict | None:
    """Return a single RG paragraph by its dotted number (e.g. '5.15'), or None."""
    data = _load_paragraphs(rg_number)
    if not data:
        return None
    for p in data.get("paragraphs", []):
        if str(p.get("num", "")).strip() == para:
            # Prefer the full manifest title over the truncated PDF-header title.
            title = data.get("title", "")
            try:
                title = _find_rg(rg_number).get("title", title)
            except Exception:
                pass
            return {
                "rg_number": rg_number,
                "paragraph": para,
                "title": title,
                "text": (p.get("text") or "").strip(),
            }
    return None


@router.get("/api/regulatory-guides")
def list_regulatory_guides(group: str = "status"):
    """List all ASIC Regulatory Guides, grouped by status (default) or flat."""
    manifest = _load_manifest()
    if group == "flat":
        return {
            "act": "ASIC Regulatory Guides",
            "parts": [
                {
                    "id": "rg-all",
                    "title": "Regulatory Guides",
                    "divisions": [],
                    "sections": [
                        {
                            "id": str(rg["rg_number"]),
                            "title": f"RG {rg['rg_number']} — {rg['title']}" + ("  [WITHDRAWN]" if rg["status"] == "withdrawn" else ""),
                            "path": str(rg["rg_number"]),
                        }
                        for rg in sorted(manifest, key=lambda r: r["rg_number"])
                    ],
                }
            ],
        }

    # Group by status
    grouped: dict[str, list[dict]] = {}
    for rg in manifest:
        grouped.setdefault(rg["status"], []).append(rg)

    parts = []
    for status in ("current", "no_pdf", "withdrawn", "unavailable"):
        if status not in grouped:
            continue
        rgs = sorted(grouped[status], key=lambda r: r["rg_number"])
        parts.append({
            "id": status,
            "title": _STATUS_DISPLAY.get(status, status),
            "divisions": [],
            "sections": [
                {
                    "id": str(rg["rg_number"]),
                    "title": f"RG {rg['rg_number']} — {rg['title']}",
                    "path": str(rg["rg_number"]),
                    "has_pdf": rg.get("has_pdf", False),
                    "page_url": rg.get("page_url", ""),
                    "pdf_url": rg.get("pdf_url", ""),
                }
                for rg in rgs
            ],
        })
    return {"act": "ASIC Regulatory Guides", "parts": parts}


@router.get("/api/regulatory-guide/{rg_number}")
def get_regulatory_guide(rg_number: int):
    """Retrieve an ASIC Regulatory Guide with summary + download links."""
    rg = _find_rg(rg_number)

    # Check for structured summary
    summary_path = SUMMARY_DIR / f"RG_{rg_number}.json"
    summary = None
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    body = ""
    if rg.get("has_text"):
        text_path = TEXT_DIR / f"RG_{rg_number:03d}.txt"
        if text_path.exists():
            body = _clean_text(text_path.read_text(encoding="utf-8", errors="replace"))

    paragraphs = get_rg_paragraphs(rg_number)

    return {
        "frontmatter": {
            "act": "ASIC Regulatory Guides",
            "title": f"RG {rg_number} — {rg['title']}",
            "part": "Regulatory Guide",
            "division": _STATUS_DISPLAY.get(rg["status"], rg["status"]),
        },
        "citation": f"RG {rg_number}",
        "descriptive_title": rg["title"],
        "status": _STATUS_DISPLAY.get(rg["status"], rg["status"]),
        "status_key": rg["status"],
        "body": body,
        "paragraphs": paragraphs or [],
        "paragraph_count": len(paragraphs) if paragraphs else 0,
        "has_pdf": rg.get("has_pdf", False),
        "page_url": rg.get("page_url", ""),
        "pdf_url": rg.get("pdf_url", ""),
        "date": rg.get("date", ""),
        "file_size": rg.get("file_size", 0),
        "download_url": f"/api/regulatory-guide/{rg_number}/download" if rg.get("has_pdf") else "",
        # Structured summary data (if available)
        "subject": summary.get("subject", "") if summary else "",
        "background": summary.get("background", "") if summary else "",
        # RG summaries are ASIC guidance, not rulings — keep the field neutral.
        "guidance": summary.get("ruling", "") if summary else "",
        "cases_referenced": summary.get("cases_referenced", []) if summary else [],
        "legislation_referenced": summary.get("legislation_referenced", []) if summary else [],
        "related_rulings": summary.get("related_rulings", []) if summary else [],
        "has_summary": summary is not None,
        "licence": document_licence("regulatory-guides", source_url=rg.get("page_url") or None),
    }


@router.get("/api/regulatory-guide/{rg_number}/sections")
def get_regulatory_guide_sections(rg_number: int):
    """Return the Corps Act sections referenced by this RG."""
    index = _load_rg_section_index()
    key = f"RG_{rg_number}"
    sections = index.get(key, [])

    # Enrich with titles from the corps act tree
    try:
        from backend.services.data_loader import load_tree
        tree = load_tree("corporations-act-2001")
        sec_map: dict[str, str] = {}
        for part in tree.get("parts", []):
            for sec in part.get("sections", []):
                sec_map[sec["id"]] = sec.get("title", "")
            for div in part.get("divisions", []):
                for sec in div.get("sections", []):
                    sec_map[sec["id"]] = sec.get("title", "")
                for sub in div.get("subdivisions", []):
                    for sec in sub.get("sections", []):
                        sec_map[sec["id"]] = sec.get("title", "")
        enriched = []
        for s in sections:
            sid = s["section"]
            enriched.append({
                "act": s["act"],
                "section": sid,
                "title": sec_map.get(sid, ""),
                "path": f"/api/section/{s['act']}/{sid}",
            })
        sections = enriched
    except Exception:
        pass

    return {"rg_number": rg_number, "count": len(sections), "sections": sections}


@router.get("/api/regulatory-guides/for-section/{act}/{section}")
def get_rgs_for_section(act: str, section: str):
    """Return the RGs that reference a given legislation section."""
    rev = _load_reverse_section_index()
    key = f"{act}#{section}"
    rg_keys = rev.get(key, [])
    if not rg_keys:
        base = section.split("(")[0].strip()
        if base != section:
            key = f"{act}#{base}"
            rg_keys = rev.get(key, [])

    manifest = _load_manifest()
    manifest_map = {rg["rg_number"]: rg for rg in manifest}
    result = []
    for k in rg_keys:
        rg_num = int(k.split("_")[1])
        rg = manifest_map.get(rg_num, {})
        result.append({
            "rg_number": rg_num,
            "title": rg.get("title", ""),
            "status": _STATUS_DISPLAY.get(rg.get("status", ""), rg.get("status", "")),
            "path": f"/api/regulatory-guide/{rg_num}",
        })

    return {
        "act": act,
        "section": section,
        "count": len(result),
        "regulatory_guides": result,
    }


@router.get("/api/regulatory-guide/{rg_number}/download")
def download_regulatory_guide(rg_number: int):
    """Download an RG PDF from our server, else redirect to the ASIC copy."""
    rg = _find_rg(rg_number)
    if not rg.get("has_pdf"):
        raise HTTPException(status_code=404, detail=f"RG {rg_number} has no PDF available")
    pdf_path = PDF_DIR / f"RG_{rg_number:03d}.pdf"
    if not pdf_path.exists():
        if rg.get("pdf_url"):
            return RedirectResponse(rg["pdf_url"])
        raise HTTPException(status_code=404, detail=f"PDF for RG {rg_number} not found on server")
    return FileResponse(
        path=pdf_path,
        filename=f"RG_{rg_number:03d}.pdf",
        media_type="application/pdf",
    )


@router.get("/api/regulatory-guides/search")
def search_regulatory_guides(q: str, limit: int = 20):
    """Full-text search across ASIC Regulatory Guide titles and bodies."""
    q = q.strip().lower()
    if not q:
        return {"query": q, "count": 0, "results": []}

    manifest = _load_manifest()
    results = []
    for rg in manifest:
        score = 0
        # Title match (higher score)
        if q in rg["title"].lower():
            score += 10
        if f"rg {rg['rg_number']}".lower() in q or str(rg["rg_number"]) == q:
            score += 20
        # Body match
        body = ""
        if rg.get("has_text"):
            text_path = TEXT_DIR / f"RG_{rg['rg_number']:03d}.txt"
            if text_path.exists():
                body = text_path.read_text(encoding="utf-8", errors="replace").lower()
        if q in body:
            score += 1
        if score > 0:
            results.append({
                "rg_number": rg["rg_number"],
                "title": rg["title"],
                "status": rg["status"],
                "score": score,
                "has_pdf": rg.get("has_pdf", False),
                "page_url": rg.get("page_url", ""),
            })

    results.sort(key=lambda r: (-r["score"], r["rg_number"]))
    return {"query": q, "count": len(results), "results": results[:limit]}