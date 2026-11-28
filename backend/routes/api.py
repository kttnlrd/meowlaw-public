"""API route assembly."""
from __future__ import annotations

import asyncio as _asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from backend.config import INSTANCE
from backend.services import content_access, corpus_chains

from .acts import router as acts_router
from .definitions import router as definitions_router
from .search import router as search_router
from .cases import router as cases_router
from .rulings import router as rulings_router
from .links import router as links_router
from .comments import router as comments_router
from .tax_cases import router as tax_cases_router
from .user_prefs import router as user_prefs_router
from .admin import router as admin_router
from .admin_usage import router as admin_usage_router
from .data_versions import router as data_versions_router

from .graph import router as graph_router
from .private_rulings import router as private_rulings_router
from .issues import router as issues_router
from .regulatory_guides import router as regulatory_guides_router
from .afsa_guides import router as afsa_guides_router
from .treaties import router as treaties_router
from .workflows import router as workflows_router
from .content_types import router as content_types_router

router = APIRouter()
router.include_router(acts_router)
router.include_router(definitions_router)
router.include_router(search_router)
router.include_router(cases_router)
router.include_router(rulings_router)
router.include_router(links_router)
router.include_router(comments_router)
router.include_router(tax_cases_router)
router.include_router(user_prefs_router)
router.include_router(admin_router)
router.include_router(admin_usage_router)
router.include_router(data_versions_router)
router.include_router(graph_router)
router.include_router(private_rulings_router)
router.include_router(issues_router)
router.include_router(regulatory_guides_router)
router.include_router(afsa_guides_router)
router.include_router(treaties_router)
router.include_router(workflows_router)
router.include_router(content_types_router)


VERSION = "1.2.0"

CHANGELOG = [
    {
        "version": "1.2.0",
        "date": "2026-11-27",
        "title": "Faster loading, pinned sources and collections",
        "changes": [
            "New features:",
            "|– The app opens straight away; a source's tree loads its chapters first and each chapter's sections when you open it",
            "|– Pin the sources you use most: they stay at the top of the sidebar on every device",
            "|– Any body of material can be added as a collection and browsed, searched and read through the AI connector like the rest",
            "|– A loading bar for large sources",
            "Security:",
            "|– Licensed and private sources read as not found to accounts without access, on the website and through the AI connector",
            "|– An account granted a source keeps it when the source is switched off for everyone else",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-11-27",
        "title": "Sign in with Google or Microsoft",
        "changes": [
            "New features:",
            "|– Sign in with your Google account, or a Microsoft work, school or personal account, as well as an emailed login code",
            "Security:",
            "|– Licensed commentary, procedural maps, workflows and interpretive chains are now available only to accounts that have been granted them, on the website and through the AI connector",
            "|– Stronger protection for login codes and sign-in redirects",
            "Other:",
            "|– Source code published under the GNU AGPL v3",
        ],
    },
    {
        "version": "0.1 beta",
        "date": "2026-10-03",
        "title": "New legislation",
        "changes": [
            "New content:",
            "|– Ten new Commonwealth Acts are now searchable: Competition and Consumer Act 2010, Criminal Code 1995, Fair Work Act 2009, Migration Act 1958, Privacy Act 1988, Evidence Act 1995, National Disability Insurance Scheme Act 2013, Judiciary Act 1903, Administrative Decisions (Judicial Review) Act 1977 and the Constitution",
        ],
    },
    {
        "version": "0.1 beta",
        "date": "2026-09-30",
        "title": "Public beta",
        "changes": [
            "New features:",
            "|– Web app: search legislation, case law, rulings and regulatory guides in one place",
            "|– AI connector: connect Claude Desktop or any MCP client for cited answers",
            "|– Search graphing: jump between related content and definitions from any section",
            "|– Procedural maps: interactive decision trees for CGT, Div 7A, residency and more",
            "|– Private rulings browser: 57,000+ rulings searchable by number",
            "",
            "Bugs fixed:",
            "|– Sessions not persisting on mobile (site now forces HTTPS)",
        ],
    },
]


@router.get("/api/info")
def api_info():
    """Return version and changelog."""
    return {
        "name": INSTANCE,
        "version": VERSION,
        "changelog": CHANGELOG,
    }


# --- tax-encode proxy (Interpretive chains, ALPHA) --------------------------------------
# The browser never reaches tax-encode; these pass its JSON through unchanged. If it is not
# running on this host the endpoint says so, and the frontend hides the entry point.
import os as _os
import time as _time
from urllib.parse import quote as _quote

import httpx as _httpx

TAX_API_URL = _os.environ.get("TAX_API_URL", "http://127.0.0.1:8793").rstrip("/")
_TAX_TTL = 600
_tax_cache: dict = {}


async def _tax_get(kind: str, act: str, section: str):
    key = (kind, act, section)
    hit = _tax_cache.get(key)
    if hit and _time.monotonic() - hit[0] < _TAX_TTL:
        return hit[1]
    headers = {}
    if _os.environ.get("TAX_API_TOKEN"):
        headers["Authorization"] = f"Bearer {_os.environ['TAX_API_TOKEN']}"
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{TAX_API_URL}/{kind}/{_quote(act, safe='')}/{_quote(section, safe='')}",
                            headers=headers)
        body = r.json()
    except Exception:
        return {"error": "chains unavailable", "detail": "tax-encode is not reachable from this host"}
    if r.status_code == 200:
        _tax_cache[key] = (_time.monotonic(), body)
    return body


@router.get("/api/tax/health")
async def tax_health(request: Request):
    """Chains need either tax-encode or the corpus builder, so report both."""
    if not await _asyncio.to_thread(content_access.is_available, request, "chains"):
        return {"ok": False, "detail": "chains disabled"}
    body: dict = {"ok": False, "corpus_acts": list(corpus_chains.NZ_ACTS)}
    try:
        async with _httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{TAX_API_URL}/health")
        if r.status_code == 200:
            body.update(r.json())
            body["ok"] = True
        else:
            body["detail"] = f"tax-encode HTTP {r.status_code}"
    except Exception:
        body["detail"] = "tax-encode is not reachable from this host"
    if not body["ok"]:
        # corpus chains can still serve the NZ Acts, which is better than hiding the view
        body["ok"] = True
        body["degraded"] = "tax-encode unavailable; serving corpus chains only"
    return body


@router.get("/api/tax/chains/{act}/{section}", dependencies=[Depends(content_access.gate("chains"))])
async def tax_chains(act: str, section: str):
    """Tax-encode first; for acts it does not encode (the NZ Acts) build chains from the corpus.

    tax-encode only holds ITAA 1936/1997 divisions, so every NZ provision used to return 404 and
    the chains view had nothing to draw.
    """
    body = await _tax_get("chains", act, section)
    if body.get("nodes"):
        return body
    if corpus_chains.covers(act):
        return await _asyncio.to_thread(corpus_chains.build, act, section)
    return body
