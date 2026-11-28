"""ATO Legal Database search proxy.

Wraps the ATO's lawservices search API (POST /API/v1/law/lawservices/result —
the same endpoint the private-rulings scraper uses) for the explorer
frontend "ATO search" button. Handles Akamai warm-up with a fresh cookie
jar, global request pacing (~0.3 rps), block cooldown, and a short TTL
cache so repeated queries don't hammer the ATO.

Endpoints:
  GET /api/ato/search?q=...&start=1&pageSize=20&df=3819
      q         free-text query
      start     result offset (1-based, ATO pagination)
      pageSize  results per page (max 100)
      df        ATO document-family filter (3819 = private rulings; empty = all)
  GET /api/ato/search/health
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends
from backend.services.content_access import gate
from fastapi.responses import JSONResponse

API_URL = "https://www.ato.gov.au/API/v1/law/lawservices/result"
LAW_URL = "https://www.ato.gov.au/law/"
DOC_URL = "https://www.ato.gov.au/law/view/document"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Query parameter name the ATO API expects. Confirmed by scripts/probe_ato_search.py.
QUERY_PARAM = "query"

# ATO pacing: ~0.3 requests/sec with a shared slot timer.
MIN_GAP = 3.5
COOLDOWN_SECONDS = 90  # after a block, pause the whole endpoint

_lock = threading.Lock()
_next_slot = 0.0
_cooldown_until = 0.0
_jar = Path("/tmp/ato_explorer_cookies.txt")

_cache: dict[tuple, tuple[float, dict]] = {}
CACHE_TTL = 300  # 5 minutes


def _curl(args: list[str], timeout: int = 25) -> str:
    try:
        p = subprocess.run(
            ["curl", "-s", "-m", str(timeout)] + args,
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return p.stdout
    except Exception:
        return ""


def _pace() -> None:
    """Wait for the shared request slot; extend cooldown when blocked."""
    global _next_slot, _cooldown_until
    with _lock:
        now = time.time()
        if now < _cooldown_until:
            time.sleep(_cooldown_until - now)
            now = time.time()
        wait = _next_slot - now
        if wait > 0:
            time.sleep(wait)
        _next_slot = time.time() + MIN_GAP


def _warm_up() -> bool:
    """Establish Akamai trust with a fresh cookie jar."""
    _jar.unlink(missing_ok=True)
    _curl(["-c", str(_jar), "-A", USER_AGENT, LAW_URL, "-o", "/dev/null"])
    body = _curl(["-c", str(_jar), "-A", USER_AGENT, LAW_URL])
    return len(body) > 500


def _post(params: list[tuple[str, str]]) -> str:
    args = [
        "-b", str(_jar), "-c", str(_jar), "-A", USER_AGENT,
        "-H", "Referer: https://www.ato.gov.au/law/",
        "-X", "POST", API_URL,
        "--data-urlencode", "src=qa",
        "--data-urlencode", "stype=find",
        "--data-urlencode", "pit=99991231235958",
        "--data-urlencode", "df=",
        "--data-urlencode", "pageSize=8",
        "--data-urlencode", "start=1",
    ]
    for k, v in params:
        args += ["--data-urlencode", f"{k}={v}"]
    return _curl(args)


def _parse(body: str) -> dict:
    """Parse the ATO results HTML into {total, results[]}."""
    m_total = re.search(r'total="?(\d+)"?', body)
    total = int(m_total.group(1)) if m_total else 0

    # Each result: anchor with docid + title text, followed by a <strong> year/type
    results = []
    for m in re.finditer(
        r'href="[^"]*docid=([A-Za-z0-9%/]+)[^"]*"[^>]*>(.*?)</a>\s*(?:<strong>\s*([^<]{1,40})\s*</strong>)?',
        body, re.S | re.I,
    ):
        docid, title, strong = m.group(1), m.group(2), m.group(3)
        title = re.sub(r"<[^>]+>", "", title)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        prefix = docid.split("%2F")[0].split("/")[0].upper()
        results.append({
            "docid": docid.replace("%2F", "/"),
            "prefix": prefix,
            "type": _TYPE_NAMES.get(prefix, prefix),
            "title": title[:220],
            "year": strong.strip() if strong else "",
            "link": f"{DOC_URL}?docid={docid}&pit=99991231235958",
        })
    return {"total": total, "results": results}


_TYPE_NAMES = {
    "EV": "Private ruling",
    "AID": "ATO interpretative decision",
    "TD": "Taxation determination",
    "CR": "Class ruling",
    "TR": "Taxation ruling",
    "IT": "Income tax ruling",
    "PR": "Product ruling",
    "PSLA": "Practice statement",
    "MT": "Miscellaneous tax ruling",
    "CT": "Consolidation ruling",
    "ATO": "ATO document",
}


def _search(q: str, start: int, page_size: int, df: str) -> dict:
    key = (q, start, page_size, df)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]

    global _cooldown_until
    for attempt in range(3):
        _pace()
        params = [
            ("src", "qa"), ("stype", "find"), ("pit", "99991231235958"),
            ("df", df), ("pageSize", str(page_size)), ("start", str(start)),
            (QUERY_PARAM, q),
        ]
        body = _curl(["-b", str(_jar), "-c", str(_jar), "-A", USER_AGENT,
                      "-H", "Referer: https://www.ato.gov.au/law/",
                      "-X", "POST", API_URL] +
                     [a for kv in params for a in ("--data-urlencode", f"{kv[0]}={kv[1]}")])
        if len(body) > 500 and "Access Denied" not in body and "unavailable" not in body.lower():
            data = _parse(body)
            with _lock:
                _cache[key] = (time.time(), data)
            return data
        # Blocked — cooldown, refresh jar, retry
        with _lock:
            _cooldown_until = time.time() + COOLDOWN_SECONDS
        _warm_up()
    raise HTTPException(502, "ATO Legal Database unreachable (Akamai block). Try again shortly.")


router = APIRouter(dependencies=[Depends(gate("ato-private-ruling"))])


@router.get("/api/ato/search")
def ato_search(
    q: str = Query(..., min_length=2),
    start: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    df: str = Query(""),
):
    try:
        return _search(q.strip(), start, pageSize, df)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"ATO search failed: {e}")


@router.get("/api/ato/search/health")
def ato_health():
    return JSONResponse({"ok": True, "query_param": QUERY_PARAM})
