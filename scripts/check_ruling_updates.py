#!/usr/bin/env python3
"""Check ATO website for new and amended rulings.

Uses the same sequential enumeration pattern as bulk_scrape_rulings.py
but only for the current and previous year (fast monthly check).
Detects new rulings (not on disk) and amended rulings.

Amendment detection uses SIDECAR HASHES (data/rulings/*.txt.meta.json):
the normalized remote body hash is stored at fetch time, and later runs
compare the fresh remote hash against the stored one. This makes the
comparison stable-only (local file corruption can never cause phantom
amendments) and removes the need for local text to be a pure function
of remote HTML. First run after deployment baselines the sidecars.

Outputs JSON to stdout with change log.

Usage:
  python3 scripts/check_ruling_updates.py
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("check_rulings")

RULINGS_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data/rulings")
RULINGS_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
DELAY = 0.5  # seconds between requests
MAX_MISSES = 5  # consecutive 404s before giving up on a year

# Ruling types to check (only current + last year)
# Same DocID format as bulk_scrape_rulings.py
TYPES = {
    "TR":   {"code": "TXR/TR",  "year": "pre2000"},  # pre2000 mode is vestigial: main() only checks current/prior year, and extract_title requires 4-digit years. Do not reuse for historical backfill without adding a year-mode-aware title parser.
    "TD":   {"code": "TXD/TD",  "year": "4digit"},
    "PCG":  {"code": "COG/PCG", "year": "4digit"},
    "LCG":  {"code": "COG/LCG", "year": "4digit"},
    "GSTR": {"code": "GST/GSTR","year": "4digit"},
    "PS_LA":{"code": "PSR/PS",  "year": "4digit"},
    "TA":   {"code": "TPA/TA",  "year": "4digit"},
    "MT":   {"code": "MXR/MT",  "year": "4digit"},
    "SGR":  {"code": "SGR/SGR", "year": "4digit"},
    "CR":   {"code": "CLR/CR",  "year": "4digit"},
    "PR":   {"code": "PRR/PR",  "year": "4digit"},
}

# fetch_ruling status sentinels
OK = "ok"
NOT_FOUND = "not_found"
UNPARSEABLE = "unparseable"
RATE_LIMITED = "rate_limited"
TRANSPORT_ERROR = "transport_error"

CITATION_RE = re.compile(r"\b(?:TR|TD|PCG|LCG|GSTR|PS\s+LA|TA|MT|SGR|CR|PR)\s+\d{4}/\d+\b")


def build_url(rtype: str, cfg: dict, year: int, num: int) -> str:
    """Build the ATO print view URL for a given ruling."""
    code = cfg["code"]
    year_mode = cfg["year"]
    if year_mode == "pre2000":
        yr = year % 100 if year < 2000 else year
    else:
        yr = year
    docid = f"{code}{yr}{num}"
    return f"https://www.ato.gov.au/law/view/print?DocID={docid}/NAT/ATO/00001"


def fetch_ruling(url: str) -> tuple[str, str, str]:
    """Fetch a ruling. Returns (normalised text, title, status).

    status is one of OK / NOT_FOUND / UNPARSEABLE / RATE_LIMITED /
    TRANSPORT_ERROR so the caller can distinguish a genuine end-of-series
    404 from a transient failure or a page that exists but cannot be parsed.
    """
    try:
        r = curl.get(url, impersonate="chrome120", headers=HEADERS, timeout=20)
    except Exception as e:
        log.warning("Request error %s: %s", url, e)
        return ("", "", TRANSPORT_ERROR)
    if r.status_code == 429 or r.status_code >= 500:
        return ("", "", RATE_LIMITED)
    if r.status_code != 200:
        return ("", "", NOT_FOUND)
    title = extract_title(r.text)
    if not title:
        return ("", "", UNPARSEABLE)
    text = normalize_ruling_text(r.text)
    if len(text) < 100:
        log.warning("Unparseable (body too short): %s", url)
        return ("", "", UNPARSEABLE)
    return (text, title, OK)


def extract_title(page_html: str) -> str:
    """Extract the ruling title from the ATO print view HTML.

    New template (2026): <h2>TR 2026/1</h2> immediately followed by
    <h3>The actual ruling title</h3>. The <title> tag now just says
    "TR 2026/1 | Legal database", which is useless. Fallbacks: og:title /
    DC.Title meta, then <title> minus the suffix.
    """
    m = re.search(r"<h2[^>]*>\s*(TR|TD|PCG|LCG|GSTR|PS\s+LA|TA|MT|SGR|CR|PR)\s+\d{4}/\d+\s*</h2>\s*<h3[^>]*>(.*?)</h3>", page_html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()
    else:
        # Fallback 1: og:title / DC.Title meta
        m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:title|DC\.Title)["\'][^>]*content=["\']([^"\']+)', page_html, re.I)
        if m:
            title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
        else:
            # Fallback 2: <title> tag minus "| Legal database"
            m = re.search(r"<title>(.*?)</title>", page_html, re.I | re.S)
            if not m:
                return ""
            title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
            title = re.sub(r"\s*\|\s*Legal database\s*$", "", title)
    # Fallback paths keep the citation prefix ("TA 2025/1 - Managed investment
    # trusts: ..."); strip it so the change log carries the clean title.
    return re.sub(r"^(?:TR|TD|PCG|LCG|GSTR|PS\s+LA|TA|MT|SGR|CR|PR)\s+\d{4}/\d+\s*[-–—]?\s*", "", title, flags=re.I).strip()


def normalize_ruling_text(text: str) -> str:
    """Normalise raw HTML/text to a canonical body for hashing.

    Anchors at the substantive-body heading for the ruling type, falling back
    to the citation line for short forms that lack headings. html.unescape
    handles the &bull; &ndash; &mdash; entities the 2026 ATO template
    introduced. NOTE: this only needs to be STABLE (same input -> same hash),
    not bidirectional with the local corpus, because amendment detection
    compares fresh remote hash against the stored sidecar hash.
    """
    t = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t)
    # Strip the 2026-template <title> chrome: "TR 2026/1 | Legal database"
    t = re.sub(r"^\s*(?:TR|TD|PCG|LCG|GSTR|PS\s+LA|TA|MT|SGR|CR|PR)\s+\d{4}/\d+\s*\|\s*Legal database\s*", "", t, flags=re.I)
    # Strip the PDF-authorised-version boilerplate (new template sentence)
    t = re.sub(r"Please note that the PDF version is the authorised version of this (?:ruling|guideline|determination|alert)\.?\s*", "", t, flags=re.I)
    # Strip local-only ingestion artifacts that have no remote equivalent
    t = re.sub(r"HEAD NOTE\s*", " ", t)
    t = re.sub(r"\bTaxpayer Alert\b\s*", " ", t)
    t = re.sub(r"//\s*Practice Statement Law Administration\b\s*", " ", t)
    # Anchor: substantive-body heading for the ruling type, else citation line.
    # NB: the bare "Ruling" substring is deliberately NOT an anchor — it
    # latches onto arbitrary occurrences (e.g. "Ruling TR 93/17" inside a PCG)
    # and makes comparison non-deterministic.
    for anchor in (
        "What this Ruling is about",
        "What this Guideline is about",
        "What this Determination is about",
        "What this Alert is about",
    ):
        i = t.find(anchor)
        if i != -1:
            t = t[i:]
            break
    else:
        m = CITATION_RE.search(t)
        if m:
            t = t[m.start():]
    # Cut trailing boilerplate
    for marker in ("Copyright notice", "Our commitment to your privacy", "Feedback about this page"):
        j = t.find(marker)
        if j != -1:
            t = t[:j]
    return t.strip()


def content_hash(text: str) -> str:
    """SHA-256 of the full canonical body (catches changes anywhere)."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def rtype_filename(rtype: str, year: int, num: int) -> str:
    """Build the filename for a ruling on disk."""
    safe = rtype.replace(" ", "_")
    return f"{safe}_{year}_{num}.txt"


def sidecar_path(fname: str) -> Path:
    """Sidecar metadata path for a ruling file (bulk_scrape writes these)."""
    return RULINGS_DIR / f"{fname}.meta.json"


def read_sidecar_hash(fname: str) -> str | None:
    """Return the stored remote-hash from the sidecar, if present."""
    p = sidecar_path(fname)
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return meta.get("remote_hash")
    except (json.JSONDecodeError, OSError):
        return None


def write_sidecar_hash(fname: str, h: str) -> None:
    """Persist the remote-hash in the sidecar, preserving other fields."""
    p = sidecar_path(fname)
    meta = {}
    if p.exists():
        try:
            meta = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta["remote_hash"] = h
    meta["last_checked"] = datetime.now().isoformat()
    p.write_text(json.dumps(meta, indent=1), encoding="utf-8")


def get_existing_rulings() -> dict[str, dict]:
    """Scan disk for existing rulings. Returns {filename: {type, year, num}}.

    Only ruling types actually checked (TYPES) are indexed; AID_* and other
    private/background rulings are skipped (they are never enumerated here).
    """
    existing = {}
    for f in RULINGS_DIR.glob("*.txt"):
        if f.name.count("_") < 2:
            continue
        parts = f.stem.split("_")
        if len(parts) < 3:
            continue
        rtype = parts[0]
        if parts[0] == "PS" and parts[1] == "LA":
            rtype = "PS_LA"
            parts = parts[1:]
        if rtype not in TYPES:
            continue
        try:
            year = int(parts[-2])
            num = int(parts[-1])
        except (ValueError, IndexError):
            continue
        existing[f.name] = {"type": rtype, "year": year, "num": num, "path": str(f)}
    return existing


def check_ruling_type(
    rtype: str,
    cfg: dict,
    year: int,
    existing: dict[str, dict],
) -> dict:
    """Check a single ruling type for a single year. Returns change log."""
    new_rulings = []
    amended_rulings = []
    withdrawn_rulings = []
    errors = []
    stats = {"checked": 0, "new": 0, "amended": 0, "withdrawn": 0, "errors": 0, "baselined": 0}

    misses = 0
    num = 1
    seen = set()
    while misses < MAX_MISSES:
        url = build_url(rtype, cfg, year, num)
        text, title, status = fetch_ruling(url)

        if status == RATE_LIMITED:
            stats["errors"] += 1
            errors.append({"type": rtype, "year": year, "num": num, "error": f"HTTP 429/5xx: {url}"})
            log.warning("Rate limited at %s; backing off", url)
            time.sleep(5)
            continue  # not a miss; do not advance the enumeration
        if status == TRANSPORT_ERROR:
            stats["errors"] += 1
            errors.append({"type": rtype, "year": year, "num": num, "error": f"Transport: {url}"})
            misses += 1
            num += 1
            continue
        if status == UNPARSEABLE:
            stats["errors"] += 1
            errors.append({"type": rtype, "year": year, "num": num, "error": f"Unparseable: {url}"})
            misses += 1
            num += 1
            continue
        if status == NOT_FOUND:
            misses += 1
            num += 1
            continue

        h = content_hash(text)
        fname = rtype_filename(rtype, year, num)
        seen.add(fname)
        stats["checked"] += 1
        stored = read_sidecar_hash(fname)

        if fname not in existing:
            stats["new"] += 1
            new_rulings.append({"type": rtype, "year": year, "num": num, "title": title[:200], "hash": h})
            write_sidecar_hash(fname, h)
        elif stored is None:
            # No baseline yet: persist the hash without flagging an amendment.
            # First run after deployment baselines the whole corpus.
            stats["baselined"] += 1
            write_sidecar_hash(fname, h)
        elif stored != h:
            stats["amended"] += 1
            amended_rulings.append({
                "type": rtype, "year": year, "num": num,
                "title": title[:200], "old_hash": stored, "new_hash": h,
            })
            write_sidecar_hash(fname, h)

        misses = 0
        num += 1
        time.sleep(DELAY)

    # Withdrawal detection: local rulings of this type/year not seen remotely
    for fname, info in existing.items():
        if info["type"] == rtype and info["year"] == year and fname not in seen:
            stats["withdrawn"] += 1
            withdrawn_rulings.append({"type": rtype, "year": year, "num": info["num"], "filename": fname})

    return {
        "type": rtype,
        "year": year,
        "new": new_rulings,
        "amended": amended_rulings,
        "withdrawn": withdrawn_rulings,
        "errors": errors,
        "stats": stats,
    }


def main():
    t0 = time.time()
    current_year = datetime.now().year
    years_to_check = {current_year, current_year - 1}

    # Build existing index once
    existing = get_existing_rulings()
    total_existing = len(existing)
    log.info("Existing rulings on disk: %d", total_existing)

    results = []
    total_new = 0
    total_amended = 0
    total_checked = 0

    for rtype, cfg in sorted(TYPES.items()):
        for year in sorted(years_to_check):
            r = check_ruling_type(rtype, cfg, year, existing)
            results.append(r)
            total_new += r["stats"]["new"]
            total_amended += r["stats"]["amended"]
            total_checked += r["stats"]["checked"]

    output = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_seconds": round(time.time() - t0, 2),
        "existing_on_disk": total_existing,
        "years_checked": sorted(years_to_check),
        "total_checked": total_checked,
        "total_new_rulings": total_new,
        "total_amended_rulings": total_amended,
        "results": results,
        "summary": {
            "new_by_type": {r["type"]: r["stats"]["new"] for r in results if r["stats"]["new"]},
            "amended_by_type": {r["type"]: r["stats"]["amended"] for r in results if r["stats"]["amended"]},
            "withdrawn_by_type": {r["type"]: r["stats"]["withdrawn"] for r in results if r["stats"]["withdrawn"]},
        },
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
