"""API routes for Double Tax Agreements (Treaties)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends
from backend.services.content_access import gate

from backend.config import TREATIES_DIR
from backend.services.data_loader import is_plain_id
from backend.services.search_service import search_treaties as fts_search_treaties

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(gate("treaty"))])


@router.get("/api/treaties")
def list_treaties():
    """List all treaty countries with metadata."""
    if not TREATIES_DIR.exists():
        raise HTTPException(status_code=404, detail="Treaties directory not found")
    countries = []
    for child in sorted(TREATIES_DIR.iterdir()):
        if not child.is_dir():
            continue
        tree_path = child / "tree.json"
        if not tree_path.exists():
            continue
        try:
            tree = json.loads(tree_path.read_text(encoding="utf-8"))
            countries.append({
                "slug": child.name,
                "treaty": tree.get("treaty", child.name),
                "schedule": tree.get("schedule"),
                "total": tree.get("total", 0),
            })
        except Exception:
            continue
    return {"countries": countries, "total": len(countries)}


@router.get("/api/treaties/full-tree")
def get_full_treaty_tree():
    """Return the nested tree: every country with its articles, for the sidebar.

    One call so the frontend can render expandable country -> article nodes
    without firing 42 per-country requests.
    """
    if not TREATIES_DIR.exists():
        raise HTTPException(status_code=404, detail="Treaties directory not found")
    countries = []
    for child in sorted(TREATIES_DIR.iterdir()):
        if not child.is_dir():
            continue
        tree_path = child / "tree.json"
        if not tree_path.exists():
            continue
        try:
            tree = json.loads(tree_path.read_text(encoding="utf-8"))
            countries.append({
                "slug": child.name,
                "treaty": tree.get("treaty", child.name),
                "schedule": tree.get("schedule"),
                "total": tree.get("total", 0),
                "articles": tree.get("articles", []),
            })
        except Exception:
            continue
    return {"act": "treaties", "countries": countries, "total": len(countries)}


@router.get("/api/treaties/search")
def search_treaties(q: str = Query(..., description="Search query"), limit: int = 20):
    """Full-text search across all treaty articles."""
    if not q or not q.strip():
        return {"results": [], "total": 0, "query": q}
    from backend.services.search_service import init_search_index
    from backend.config import SEARCH_DB
    if not SEARCH_DB.exists():
        init_search_index()
    result = fts_search_treaties(q.strip(), limit=min(limit, 50))
    return {"query": q, **result}


@router.get("/api/treaties/{country}")
def get_treaty_tree(country: str):
    """Get the article tree for a single treaty country."""
    tree_path = TREATIES_DIR / country / "tree.json"
    if not is_plain_id(country) or not tree_path.exists():
        raise HTTPException(status_code=404, detail=f"Treaty for '{country}' not found")
    try:
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        return tree
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read treaty: {e}")


@router.get("/api/treaties/{country}/article/{article}")
def get_treaty_article(
    country: str,
    article: str,
):
    """Get the text of a single article for a treaty country.
    Accepts article number OR slug (e.g. '3' or 'article-03-general-definitions').
    """
    tree_path = TREATIES_DIR / country / "tree.json"
    if not is_plain_id(country) or not tree_path.exists():
        raise HTTPException(status_code=404, detail=f"Treaty for '{country}' not found")
    try:
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read tree: {e}")

    # Resolve article by number or slug
    try:
        n = int(article)
    except ValueError:
        n = None

    art_info = None
    for a in tree.get("articles", []):
        if n is not None and a["article"] == n:
            art_info = a
            break
        if a.get("slug") == article:
            art_info = a
            break
    if not art_info:
        raise HTTPException(status_code=404, detail=f"Article '{article}' not found for {country}")

    art_path = TREATIES_DIR / country / art_info["file"]
    if not art_path.exists():
        raise HTTPException(status_code=404, detail=f"Article file not found")

    content = art_path.read_text(encoding="utf-8", errors="replace")
    # Strip the YAML frontmatter block (--- ... ---) from the raw body.
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].lstrip("\n")
    return {
        "country": tree["treaty"],
        "country_slug": country,
        "article": art_info["article"],
        "title": art_info["title"],
        "slug": art_info["slug"],
        "content": content,
    }
