"""Private rulings API.

Serves the 57,608-strong ATO private ruling corpus
(<ato-private-ruling source>/{authnum}.json, per data_sources.yaml).

Endpoints:
  GET /api/private-rulings/tree          — years → counts (from index)
  GET /api/private-rulings?year=&limit=  — list by year (from index)
  GET /api/private-ruling/{authnum}      — full ruling JSON

Auth: these routes fall under the global /api bearer/SSO gate in main.py
(no whitelist entry), so they are NOT public — matches the confidentiality
of private ruling content.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends
from backend.services.content_access import gate
from fastapi.responses import JSONResponse, Response

from backend.config import PRIVATE_RULINGS_DIR
from backend.services import private_ruling_outcomes
from backend.services.data_loader import document_licence

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(gate("ato-private-ruling"))])

_BASE = Path(__file__).resolve().parents[2]
INDEX_PATH = _BASE / "data" / "private_rulings_index.json"

# Corpus location: the manifest's ato-private-ruling source (backend/data_sources.py).
_CORPUS_DIR = PRIVATE_RULINGS_DIR
# Original ATO legal-database HTML pages, downloaded alongside the JSON corpus.
_HTML_DIR = _CORPUS_DIR.parent / "html"


def _ato_url(authnum: str) -> str:
    # Public format for private rulings: EV DocID on the print view (no NAT
    # suffix). Verified live 2026-08-22 — the EVR/document?DocID=.../NAT/ATO/00001
    # variant 404s. The print page body is byte-identical to the scraped HTML.
    return f"https://www.ato.gov.au/law/view/print?DocID=EV/{authnum}&PiT=99991231235958"


@lru_cache(maxsize=1)
def _load_index() -> dict:
    try:
        data = json.loads(INDEX_PATH.read_text())
        if not isinstance(data, dict):
            logger.warning("[private-rulings] index malformed: %s", INDEX_PATH)
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("[private-rulings] cannot load index %s: %s", INDEX_PATH, exc)
        return {}


@router.get("/api/private-rulings/tree")
def private_rulings_tree():
    idx = _load_index()
    if not idx:
        return JSONResponse({"error": "private rulings index not built — "
                                      "run scripts/build_private_rulings_index.py"},
                            status_code=503)
    by_year: dict[int, int] = {}
    undated = 0
    for auth, meta in idx.items():
        y = meta.get("year")
        if y:
            by_year[y] = by_year.get(y, 0) + 1
        else:
            undated += 1
    years = [{"year": y, "count": c} for y, c in sorted(by_year.items(), reverse=True)]
    return {"total": len(idx), "undated": undated, "years": years}


@router.get("/api/private-rulings")
def private_rulings_list(
    year: int | None = Query(default=None),
    month: int | None = Query(default=None, ge=1, le=12),
    undated: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    idx = _load_index()
    if not idx:
        return JSONResponse({"error": "private rulings index not built — "
                                      "run scripts/build_private_rulings_index.py"},
                            status_code=503)
    items = []
    for auth, meta in idx.items():
        if undated:
            if meta.get("year") is not None:
                continue
        elif year is not None and meta.get("year") != year:
            continue
        if month is not None:
            m = re.match(r"(\d{4})-(\d{2})", meta.get("date_of_advice") or "")
            if not m or int(m.group(2)) != month:
                continue
        item = {
            "authnum": auth,
            "section": auth,
            "name": meta.get("name", ""),
            "date_of_advice": meta.get("date_of_advice", ""),
            "year": meta.get("year"),
            "ato_url": _ato_url(auth),
            "download_url": f"/api/private-ruling/{auth}/download",
        }
        private_ruling_outcomes.enrich(item)
        items.append(item)
    items.sort(key=lambda x: (x["date_of_advice"] or "", x["authnum"]), reverse=True)
    return {"total": len(items), "year": year, "rulings": items[offset:offset + limit]}


@router.get("/api/private-ruling/{authnum}")
def private_ruling_detail(authnum: str):
    authnum = authnum.strip()
    if not authnum.isdigit():
        raise HTTPException(status_code=400, detail="authnum must be numeric")
    path = _CORPUS_DIR / f"{authnum}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no private ruling {authnum}")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("[private-rulings] cannot read %s: %s", path, exc)
        raise HTTPException(status_code=500, detail="ruling file unreadable")
    # attach the canonical graph key so the frontend can show its neighbourhood
    data["graph_key"] = f"private_ruling:EV/{authnum}"
    data["ato_url"] = _ato_url(authnum)
    data["download_url"] = f"/api/private-ruling/{authnum}/download"
    data["licence"] = document_licence("private-rulings", source_url=data["ato_url"])
    return data


@router.get("/api/private-ruling/{authnum}/download")
def private_ruling_download(authnum: str):
    """Serve the original downloaded ATO HTML page; fall back to the JSON."""
    authnum = authnum.strip()
    if not authnum.isdigit():
        raise HTTPException(status_code=400, detail="authnum must be numeric")
    html_path = _HTML_DIR / f"{authnum}.html"
    if html_path.exists():
        try:
            return Response(
                content=html_path.read_bytes(),
                media_type="text/html",
                headers={"Content-Disposition": f'attachment; filename="{authnum}.html"'},
            )
        except OSError as exc:
            logger.error("[private-rulings] cannot read %s: %s", html_path, exc)
            raise HTTPException(status_code=500, detail="ruling file unreadable")
    json_path = _CORPUS_DIR / f"{authnum}.json"
    if json_path.exists():
        try:
            return Response(
                content=json_path.read_bytes(),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{authnum}.json"'},
            )
        except OSError as exc:
            logger.error("[private-rulings] cannot read %s: %s", json_path, exc)
            raise HTTPException(status_code=500, detail="ruling file unreadable")
    raise HTTPException(status_code=404, detail=f"no private ruling {authnum}")
