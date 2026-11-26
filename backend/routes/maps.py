"""Procedural knowledge maps API.

Serves structured procedural maps (data/maps/*.json) to the frontend.
A map is a directed graph of nodes (start/event/decision/action/outcome/end)
with enriched statute, commentary, case and definition references.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.config import DATA_DIR
from backend.services.content_access import gate

router = APIRouter(dependencies=[Depends(gate("maps"))])

MAPS_DIR = DATA_DIR / "maps"


def _load_maps() -> dict[str, dict]:
    maps: dict[str, dict] = {}
    if not MAPS_DIR.exists():
        return maps
    for f in sorted(MAPS_DIR.glob("*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("id"):
                maps[m["id"]] = m
        except json.JSONDecodeError:
            continue
    return maps


@router.get("/api/maps")
def list_maps() -> list[dict]:
    """List all available procedural maps (metadata only)."""
    out = []
    for m in _load_maps().values():
        out.append(
            {
                "id": m["id"],
                "title": m.get("title", ""),
                "short": m.get("short", m.get("title", "")),
                "refs": m.get("refs", ""),
                "act": m.get("act", ""),
                "division": m.get("division", ""),
                "subdivision": m.get("subdivision", ""),
                "summary": m.get("summary", ""),
                "sort": m.get("sort", 0),
                "node_count": len(m.get("nodes", [])),
                "edge_count": len(m.get("edges", [])),
            }
        )
    out.sort(key=lambda m: (m["sort"] or 0, m["id"]))
    return out


@router.get("/api/maps/{map_id}")
def get_map(map_id: str) -> dict:
    """Return a single procedural map by id."""
    maps = _load_maps()
    if map_id not in maps:
        raise HTTPException(status_code=404, detail=f"Map '{map_id}' not found")
    return maps[map_id]
