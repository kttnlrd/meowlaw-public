"""Keays Insolvency (admin-gated), served directly from disk."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.data_sources import resolve
from backend.services.content_access import gate

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(gate("insolvency"))])

# Resolved from data_sources.local.yaml; unconfigured -> a path that never exists (404).
_INSOLVENCY_DIR = resolve()["insolvency"].path or Path("/nonexistent")


def _chapters() -> list[dict]:
    path = _INSOLVENCY_DIR / "ch-tree.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Insolvency textbook not found")
    return json.loads(path.read_text(encoding="utf-8")).get("chapters", [])


def _text(c: dict) -> str:
    f = _INSOLVENCY_DIR / c["file"]
    return f.read_text(encoding="utf-8") if f.is_file() else ""


@router.get("/api/insolvency/chapters")
async def chapters():
    chs = _chapters()
    return {"chapters": chs, "total": len(chs)}


@router.get("/api/insolvency/chapter/{chapter}")
async def chapter(chapter: int, offset: int = 0, limit: int = 5000):
    result = next((c for c in _chapters() if c.get("chapter") == chapter), None)
    if not result:
        raise HTTPException(status_code=404, detail=f"Chapter {chapter} not found")
    lines = _text(result).splitlines()
    total = len(lines)
    selected = lines[offset : offset + limit]
    return {
        "chapter": chapter, "title": result.get("title", ""), "slug": result.get("slug", ""),
        "content": "\n".join(selected), "offset": offset, "limit": limit,
        "total_lines": total, "returned_lines": len(selected),
    }


@router.get("/api/insolvency/search")
async def search(q: str, limit: int = 20):
    if not q or not q.strip():
        return {"results": [], "total": 0, "query": q}
    # ponytail: substring scan of 21 chapters on disk; FTS (insolvency_fts) if it gets slow.
    ql, hits = q.strip().lower(), []
    for c in _chapters():
        for n, line in enumerate(_text(c).splitlines()):
            if ql in line.lower():
                hits.append({"chapter": c["chapter"], "title": c["title"], "line": n, "snippet": line.strip()[:300]})
    return {"query": q, "results": hits[: min(limit, 50)], "total": len(hits)}