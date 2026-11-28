"""Client for the local searchd service — Rust hybrid retrieval for LawKitty.

searchd owns both retrieval halves: tantivy BM25 and usearch HNSW, fused with
RRF, plus a decisive boost when a query reads as an exact section reference
(the gap the old path had — `26AH` ranked third behind prose that merely
mentioned it).

This module talks to it over loopback and maps its hits into the shape the app's
JSON API already exposes, so the frontend and MCP clients see no contract change.

searchd is an accelerator, not a new dependency: if it is unreachable the caller
keeps using the in-process path, so a dead sidecar degrades ranking rather than
taking search down.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Iterable, Optional

import requests

log = logging.getLogger(__name__)

SEARCHD_URL = os.environ.get("SEARCHD_URL", "http://127.0.0.1:8791").rstrip("/")
_PROBE_TTL = 15.0  # seconds a health result is trusted
_probe_cache: tuple = (0.0, False)


def available(force: bool = False) -> bool:
    """Is searchd up? Cached, so a hot search path doesn't re-probe every query."""
    global _probe_cache
    ts, ok = _probe_cache
    if not force and (time.monotonic() - ts) < _PROBE_TTL:
        return ok
    try:
        r = requests.get(f"{SEARCHD_URL}/health", timeout=0.5)
        ok = r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        ok = False
    _probe_cache = (time.monotonic(), ok)
    if not ok:
        log.debug("searchd unavailable at %s", SEARCHD_URL)
    return ok


def ann_candidates(
    q: str,
    *,
    limit: int = 50,
    acts: Any = None,
    types: Any = None,
    series: Any = None,
    year: Any = None,
    court: Any = None,
) -> Optional[list]:
    """Vector-retrieval candidates in the app's shape, or None if unavailable.

    mode="ann" returns the ANN hits alone: the caller already has its own lexical
    results, so including searchd's BM25 would count lexical evidence twice.
    """
    r = hybrid(q, limit=limit, act=acts, types=types, series=series, year=year, court=court, mode="ann")
    if r is None:
        return None
    out = []
    for h in r["results"]:
        h = {**h, "type": h.get("source_type")}
        out.append(h)
    return out


_SERIES_RE = re.compile(r"[A-Z]+(?: [A-Z]+)*")


def ruling_series(ref: str) -> str:
    """'TR 2025/1' -> 'TR'; 'PS LA 2010/1', 'PSLA_2010_1', 'PS_LA_1998_1' -> 'PS LA'.

    The one ruling-series rule. searchd/build_index.py stamps every ruling row
    with this value and searchd matches it exactly (case-insensitive), so the
    Python fallback and the ruling FTS lane use this same function: a partial
    key such as `GST` matches nothing on every path, `GSTR` matches GSTR.
    """
    m = _SERIES_RE.match((ref or "").strip().upper().replace("_", " "))
    if not m:
        return ""
    return "PS LA" if m.group(0) == "PSLA" else m.group(0)


def normalize_series(keys: Any) -> Optional[set]:
    """rtype keys -> canonical series set ("psla", "PS_LA" -> "PS LA"); None if no keys.

    A key that isn't a series at all is kept verbatim so it matches nothing
    rather than silently dropping the filter.
    """
    keys = [k for k in (keys or ()) if k and k.strip()]
    return {ruling_series(k) or k.strip().upper() for k in keys} or None


def _join(v: Any) -> Optional[str]:
    if not v:
        return None
    return v if isinstance(v, str) else ",".join(str(x) for x in v)


def hybrid(
    q: str,
    *,
    limit: int = 20,
    offset: int = 0,
    act: Any = None,
    types: Any = None,
    series: Any = None,
    year: Any = None,
    court: Any = None,
    mode: Optional[str] = None,
) -> Optional[dict]:
    """Query searchd and return the app's response shape, or None if it's down.

    act / types / series are optional slicers (a sequence or a pre-joined
    string). series ("TR", "PS LA") matches the route's rtype filter: it
    constrains public rulings only, and searchd applies it before retrieval, so
    a filtered query needs no larger k to keep recall.
    mode="lex" runs BM25 only — no embedding call at all, for the instant paint.
    """
    if not q or not q.strip():
        return None
    if not available():
        return None

    params = {"q": q.strip(), "k": max(1, min(100, limit + max(0, offset)))}
    if (a := _join(act)):
        params["act"] = a
    if (t := _join(types)):
        params["type"] = t
    if (sr := _join(series)):
        params["series"] = sr
    if (y := _join(year)):
        params["year"] = y  # strict: only rows with one of these years
    if (c := _join(court)):
        params["court"] = c  # strict: only cases from these courts
    if mode:
        params["mode"] = mode

    try:
        r = requests.get(f"{SEARCHD_URL}/api/search", params=params, timeout=5.0)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:  # noqa: BLE001 - any failure means "fall back"
        log.warning("searchd query failed (%s); falling back", e)
        return None

    hits = payload.get("hits") or []
    results = [
        {
            "source_type": h.get("kind") or "section",
            "act": h.get("act") or "",
            "section": h.get("section") or "",
            "title": h.get("title") or "",
            "snippet": h.get("snippet") or "",
            "score": h.get("score"),
            "fusion_score": h.get("rrf", 0.0),
            "embedding_id": h.get("embedding_id"),
            # surfaced so the UI can show *why* something ranked first
            "exact": bool(h.get("exact")),
        }
        for h in hits
    ]

    return {
        "results": results[offset:],
        "total": len(results),
        "offset": offset,
        "limit": limit,
        "facets": payload.get("facets") or {},
        "meta": {
            "reranked": True,
            "source": "searchd",
            "lexical_only": bool(payload.get("lexical_only")),
            "timings": {
                "embed_ms": payload.get("embed_ms"),
                "ann_ms": payload.get("ann_ms"),
                "bm25_ms": payload.get("bm25_ms"),
                "total_ms": payload.get("total_ms"),
            },
            "error": payload.get("error"),
        },
    }
