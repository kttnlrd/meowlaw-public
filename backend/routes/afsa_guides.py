"""AFSA practice guidance — browse, search, serve paragraphs.

AFSA (Australian Financial Safety Authority) publishes practice statements,
guidelines and directions for bankruptcy + personal insolvency. Unlike ASIC
RGs they carry no PDF or withdrawn status — every doc is a live HTML page.
This module mirrors the RG surface: a flat alphabetical list, a detail view
with full text + numbered paragraphs, and bankruptcy-act section refs.
"""
from __future__ import annotations

import json
import re
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi import Depends
from backend.services.content_access import gate

from ..config import DATA_DIR

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(gate("afsa-guide"))])

AFSA_DIR = DATA_DIR / "afsa"
MANIFEST_PATH = AFSA_DIR / "afsa_manifest.json"
TEXTS_DIR = AFSA_DIR / "texts"
PARAS_DIR = AFSA_DIR / "paragraphs"

_MANIFEST_CACHE = None
_PARAGRAPHS_CACHE: dict[str, dict | None] = {}


def _load_manifest() -> list[dict]:
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is None:
        if not MANIFEST_PATH.exists():
            raise HTTPException(status_code=404, detail="AFSA practice guidance manifest not found")
        _MANIFEST_CACHE = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return _MANIFEST_CACHE


def _find_afsa(slug: str) -> dict:
    for d in _load_manifest():
        if d["slug"] == slug:
            return d
    raise HTTPException(status_code=404, detail=f"AFSA practice guidance '{slug}' not found")


def _load_paragraphs(slug: str) -> dict | None:
    if slug not in _PARAGRAPHS_CACHE:
        path = PARAS_DIR / f"{slug}.json"
        data: dict | None = None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
        _PARAGRAPHS_CACHE[slug] = data
    return _PARAGRAPHS_CACHE[slug]


def get_afsa_paragraphs(slug: str) -> list[dict] | None:
    """Return the ordered paragraph list for one AFSA doc, or None if unsplit."""
    data = _load_paragraphs(slug)
    if not data:
        return None
    return [
        {"num": p.get("num"), "heading": p.get("heading") or "", "text": (p.get("text") or "").strip()}
        for p in data.get("paragraphs", [])
    ]


def get_afsa_paragraph(slug: str, para: str) -> dict | None:
    """Return one AFSA paragraph by its dotted number (e.g. '1.1'), or None."""
    data = _load_paragraphs(slug)
    if not data:
        return None
    for p in data.get("paragraphs", []):
        if str(p.get("num", "")).strip() == para:
            return {
                "slug": slug,
                "paragraph": para,
                "title": data.get("title", ""),
                "text": (p.get("text") or "").strip(),
            }
    return None


@router.get("/api/afsa-guides")
def list_afsa_guides(group: str = "flat"):
    """List all AFSA practice-guidance documents, flat alphabetical."""
    manifest = _load_manifest()
    sections = [
        {"id": d["slug"], "title": d["title"], "path": d["slug"]}
        for d in sorted(manifest, key=lambda x: x["title"].lower())
    ]
    return {
        "act": "AFSA Practice Guidance",
        "parts": [
            {
                "id": "afsa-all",
                "title": "Practice statements, guidelines & directions",
                "divisions": [],
                "sections": sections,
            }
        ],
    }


@router.get("/api/afsa-guide/{slug}")
def get_afsa_guide(slug: str):
    """Retrieve one AFSA practice-guidance document with full text + paragraphs."""
    d = _find_afsa(slug)
    body = ""
    text_path = TEXTS_DIR / f"{slug}.txt"
    if text_path.exists():
        body = text_path.read_text(encoding="utf-8", errors="replace")
    paragraphs = get_afsa_paragraphs(slug)
    return {
        "frontmatter": {
            "act": "AFSA Practice Guidance",
            "title": d["title"],
            "part": "Practice guidance",
            "division": "",
        },
        "citation": slug,
        "descriptive_title": d["title"],
        "body": body,
        "paragraphs": paragraphs or [],
        "paragraph_count": len(paragraphs) if paragraphs else 0,
        "has_pdf": False,
        "page_url": d["url"],
        "pdf_url": "",
        "download_url": "",
        "date": "",
    }


@router.get("/api/afsa-guide/{slug}/sections")
def get_afsa_guide_sections(slug: str):
    """Return the Bankruptcy Act sections referenced by this AFSA doc (from graph)."""
    _find_afsa(slug)
    sections: list[dict] = []
    graph_path = DATA_DIR / "graph.db"
    if graph_path.exists():
        try:
            conn = sqlite3.connect(f"file:{graph_path}?mode=ro", uri=True)
            rows = conn.execute(
                """SELECT DISTINCT n.key, n.label FROM graph_edges e
                   JOIN nodes src ON src.id = e.source_id
                   JOIN nodes n ON n.id = e.target_id
                   WHERE src.key LIKE ? AND e.edge_type = 'cites' AND n.node_type = 'section'
                   ORDER BY n.label""",
                (f"afsa_guide:{slug}:%",),
            ).fetchall()
            conn.close()
            for key, label in rows:
                # key: section:bankruptcy-act-1966:<id>
                parts = key.split(":")
                sec_id = parts[-1] if len(parts) >= 3 else key
                sections.append({
                    "act": "bankruptcy-act-1966",
                    "section": sec_id,
                    "title": label,
                    "path": f"/api/section/bankruptcy-act-1966/{sec_id}",
                })
        except sqlite3.Error:
            logger.exception("AFSA sections lookup failed (non-fatal)")
    return {"slug": slug, "count": len(sections), "sections": sections}


def get_afsa_for_section(act: str, section: str) -> dict:
    """Return the AFSA practice docs that cite a given Bankruptcy Act section."""
    result: list[dict] = []
    graph_path = DATA_DIR / "graph.db"
    if graph_path.exists():
        try:
            conn = sqlite3.connect(f"file:{graph_path}?mode=ro", uri=True)
            rows = conn.execute(
                """SELECT DISTINCT n.key FROM graph_edges e
                   JOIN nodes n ON n.id = e.source_id
                   JOIN nodes t ON t.id = e.target_id
                   WHERE t.key = ? AND e.edge_type = 'cites' AND n.node_type = 'afsa_guide'""",
                (f"section:{act}:{section}",),
            ).fetchall()
            conn.close()
            manifest = {d["slug"]: d for d in _load_manifest()}
            seen: set[str] = set()
            for (key,) in rows:
                # key: afsa_guide:<slug> or afsa_guide:<slug>:<para>
                parts = key.split(":")
                slug = parts[1] if len(parts) > 1 else ""
                if slug in seen:
                    continue
                d = manifest.get(slug)
                if d:
                    seen.add(slug)
                    result.append({"slug": slug, "title": d["title"],
                                   "path": f"/api/afsa-guide/{slug}"})
        except sqlite3.Error:
            logger.exception("AFSA for-section lookup failed (non-fatal)")
    return {"act": act, "section": section, "count": len(result), "afsa_guides": result}


@router.get("/api/afsa-guides/search")
def search_afsa_guides(q: str, limit: int = 20):
    """Full-text search across AFSA practice-guidance titles and bodies."""
    q = q.strip().lower()
    if not q:
        return {"query": q, "count": 0, "results": []}
    manifest = _load_manifest()
    results = []
    for d in manifest:
        score = 0
        if q in d["title"].lower():
            score += 10
        body = ""
        text_path = TEXTS_DIR / f"{d['slug']}.txt"
        if text_path.exists():
            body = text_path.read_text(encoding="utf-8", errors="replace").lower()
        if q in body:
            score += 1
        if score > 0:
            results.append({"slug": d["slug"], "title": d["title"], "score": score, "page_url": d["url"]})
    results.sort(key=lambda r: (-r["score"], r["title"]))
    return {"query": q, "count": len(results), "results": results[:limit]}
