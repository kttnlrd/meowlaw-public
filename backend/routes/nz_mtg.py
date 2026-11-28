"""NZ Master Tax Guide (admin-gated), served directly from disk."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.data_sources import resolve
from backend.services.content_access import gate

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(gate("nz-mtg"))])

# Resolved from data_sources.local.yaml; unconfigured -> a path that never exists (404).
_NZ_MTG_DIR = resolve()["nz-mtg"].path or Path("/nonexistent")
_SLUG = re.compile(r"^[\w¶.-]+$")


def _load(name: str):
    p = _NZ_MTG_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="NZ Master Tax Guide not found")
    return json.loads(p.read_text(encoding="utf-8"))


@router.get("/api/nz-mtg/chapters")
async def chapters():
    paras = {s["id"]: s.get("paragraph", "") for s in _load("section_index.json")}
    parts = _load("tree.json").get("parts", [])
    result = [
        {
            "id": p["id"],
            "title": p["title"],
            "sections": [
                {"id": s["id"], "title": s["title"], "paragraph": paras.get(s["id"], "")}
                for s in p.get("sections", [])
            ],
        }
        for p in parts
    ]
    return {"chapters": result, "total": len(result)}


@router.get("/api/nz-mtg/section/{section_id}")
async def section(section_id: str):
    if not _SLUG.match(section_id):
        raise HTTPException(status_code=400, detail="Bad section id")
    p = _NZ_MTG_DIR / "sections" / f"{section_id}.md"
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"Section {section_id} not found")
    text = p.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    if text.startswith("---"):
        head, _, text = text[3:].partition("\n---")
        for line in head.splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip().strip('"')
    return {
        "id": section_id,
        "title": meta.get("title", ""),
        "paragraph": meta.get("paragraph", ""),
        "chapter": meta.get("part", ""),
        "content": text.lstrip("\n"),
    }


@router.get("/api/nz-mtg/search")
async def search(q: str, limit: int = 20):
    q = (q or "").strip().lower()
    if not q:
        return {"query": q, "results": [], "total": 0}
    hits = [
        s
        for s in _load("section_index.json")
        if q in s.get("title", "").lower() or q in s.get("paragraph", "").lower()
    ]
    return {"query": q, "results": hits[: min(limit, 50)], "total": len(hits)}