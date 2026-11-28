#!/usr/bin/env python3
"""
Bulk ATO ruling scraper — curl_cffi, print view, sequential enumeration.

Usage:
  python3 scripts/bulk_scrape_rulings.py --types TR,TD --start-year 1992 --end-year 2026
  python3 scripts/bulk_scrape_rulings.py --types AID --start-year 2001 --end-year 2026
  python3 scripts/bulk_scrape_rulings.py --types IT  (sequential, no years)
"""
import os
import argparse, json, logging, re, sys, time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bulk_scrape")

RULINGS_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "rulings"))
RULINGS_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = RULINGS_DIR.parent / "ato_rulings" / "bulk_scrape_progress.json"
PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
DELAY = 1.5  # seconds between requests
MAX_MISSES = 10  # consecutive 404s before skipping a year

# ── DocID configurations ─────────────────────────────────────────────────────
# format: (code, prefix, url_type, year_mode)
#   url_type: "print" = /law/view/print?DocID=.../NAT/ATO/00001
#             "aid"   = /law/view/document?docid=.../00001
#   year_mode: "4digit" = year as-is (e.g. 2024)
#              "2digit" = year % 100 (e.g. 24)
#              "pre2000" = 4digit for >=2000, 2digit for <2000
#              "none" = no year in URL (IT)

TYPES = {
    # Standard binding rulings
    "TR":   {"code": "TXR/TR",  "url": "print", "year": "pre2000", "start": 1992, "end": 2026},
    "TD":   {"code": "TXD/TD",  "url": "print", "year": "4digit",  "start": 1992, "end": 2026},

    # Guidance
    "PCG":  {"code": "COG/PCG", "url": "print", "year": "4digit",  "start": 2016, "end": 2026},
    "LCG":  {"code": "COG/LCG", "url": "print", "year": "4digit",  "start": 2015, "end": 2026},
    "GSTR": {"code": "GST/GSTR","url": "print", "year": "4digit",  "start": 1999, "end": 2026},

    # Practice statements
    "PS LA":{"code": "PSR/PS",  "url": "print", "year": "4digit",  "start": 1998, "end": 2026},

    # Other ruling types
    "TA":   {"code": "TPA/TA",  "url": "print", "year": "4digit",  "start": 2000, "end": 2026},
    "MT":   {"code": "MXR/MT",  "url": "print", "year": "4digit",  "start": 2000, "end": 2026},
    "SGR":  {"code": "SGR/SGR", "url": "print", "year": "4digit",  "start": 2003, "end": 2026},

    # New types discovered in testing
    "CR":   {"code": "CLR/CR",  "url": "print", "year": "4digit",  "start": 2000, "end": 2026},
    "PR":   {"code": "PRR/PR",  "url": "print", "year": "4digit",  "start": 2000, "end": 2026},

    # ATO IDs (different URL format)
    "AID":  {"code": "AID/AID", "url": "aid",   "year": "4digit",  "start": 2001, "end": 2026},

    # Income Tax (sequential, no year)
    "IT":   {"code": "ITR/IT",  "url": "print", "year": "none",    "start": 0,    "end": 0},
}

# Sequential-types that don't use year-based enumeration
SEQUENTIAL_TYPES = {"IT"}


def load_progress():
    p = {}
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE) as f:
                p = json.load(f)
        except Exception:
            pass
    return p


def save_progress(progress):
    progress["_last_updated"] = datetime.now().isoformat()
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


def build_url(rtype, cfg, year, num):
    """Build the ATO URL for a given ruling type, year, and number."""
    code = cfg["code"]
    year_mode = cfg["year"]

    if year_mode == "none":
        docid_part = f"{code}{num}"
    elif year_mode == "2digit":
        yr = year % 100
        docid_part = f"{code}{yr}{num}"
    elif year_mode == "pre2000":
        yr = year % 100 if year < 2000 else year
        docid_part = f"{code}{yr}{num}"
    else:
        docid_part = f"{code}{year}{num}"

    if cfg["url"] == "aid":
        return f"https://www.ato.gov.au/law/view/document?docid={docid_part}/00001"
    else:
        return f"https://www.ato.gov.au/law/view/print?DocID={docid_part}/NAT/ATO/00001"


def fetch_ruling(url, rtype, year, num):
    """Fetch a ruling. Returns (content, title) or None if not found."""
    try:
        r = curl.get(url, impersonate="chrome120", headers=HEADERS, timeout=20, verify=False)
    except Exception as e:
        log.warning("Request error %s %s/%s: %s", rtype, year, num, e)
        return None

    title_m = re.search(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
    title = title_m.group(1) if title_m else ""

    if r.status_code != 200 or ("Error" in title and "Legal" in title):
        return None

    # Extract text content
    text = re.sub(r'<[^>]+>', ' ', r.text)
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) < 100:
        return None

    return (text, title)


def save_ruling(rtype, year, num, content, title):
    """Save ruling to disk."""
    if rtype == "PS LA":
        fname = f"PS_LA_{year}_{num}"
    elif year == 0 or year is None:
        fname = f"{rtype}_{num}"
    else:
        fname = f"{rtype}_{year}_{num}"

    txt_path = RULINGS_DIR / f"{fname}.txt"
    txt_path.write_text(content, encoding="utf-8")

    # Write or update meta
    meta_path = RULINGS_DIR / f"{fname}.txt.meta.json"
    meta = {
        "doc_type": "ruling",
        "ruling_type": rtype,
        "year": year,
        "num": num,
        "title": title,
        "status": "current",
        "scraped_at": datetime.now().isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    return fname


def scrape_year(rtype, cfg, year, progress):
    """Scrape all rulings for a given type + year. Returns stats."""
    progress_key = f"{rtype}_{year}"
    progress_data = load_progress()
    if progress_key in progress_data.get("completed", {}):
        log.info("  ⏭️  %s %s already completed", rtype, year)
        return {"found": 0, "skipped": 0, "errors": 0}

    stats = {"found": 0, "skipped": 0, "errors": 0}
    misses = 0
    num = 1

    while misses < MAX_MISSES:
        url = build_url(rtype, cfg, year, num)
        result = fetch_ruling(url, rtype, year, num)

        if result is None:
            misses += 1
            if misses == 1:
                log.info("    %s/%s — no more found after #%s", rtype, year, num - 1)
        else:
            content, title = result
            if save_ruling(rtype, year, num, content, title):
                stats["found"] += 1
                if stats["found"] <= 3 or stats["found"] % 20 == 0:
                    log.info("    %s/%s - #%s (%s, %s chars)", rtype, year, num, title[:60], len(content))
            else:
                stats["errors"] += 1
            misses = 0

        num += 1
        time.sleep(DELAY)

    log.info("  ✅ %s %s: %s found, %s errors", rtype, year, stats["found"], stats["errors"])

    # Mark completed
    p = load_progress()
    p.setdefault("completed", {})[progress_key] = {
        "found": stats["found"],
        "completed_at": datetime.now().isoformat(),
    }
    save_progress(p)

    return stats


def scrape_sequential(rtype, cfg, progress):
    """Scrape sequential rulings (no year, e.g. IT)."""
    progress_key = f"{rtype}_sequential"
    progress_data = load_progress()
    if progress_key in progress_data.get("completed", {}):
        log.info("  ⏭️  %s sequential already completed", rtype)
        return {"found": 0, "skipped": 0, "errors": 0}

    stats = {"found": 0, "skipped": 0, "errors": 0}
    misses = 0
    num = 1

    while misses < MAX_MISSES:
        # Check if already on disk
        fname = f"{rtype}_{num}"
        if (RULINGS_DIR / f"{fname}.txt").exists():
            misses = 0
            num += 1
            stats["skipped"] += 1
            continue

        url = build_url(rtype, cfg, 0, num)
        result = fetch_ruling(url, rtype, 0, num)

        if result is None:
            misses += 1
        else:
            content, title = result
            save_ruling(rtype, 0, num, content, title)
            stats["found"] += 1
            if stats["found"] % 10 == 0:
                log.info("    %s #%s — %s found so far", rtype, num, stats["found"])
            misses = 0

        num += 1
        time.sleep(DELAY)

    log.info("  ✅ %s sequential: %s new, %s skipped", rtype, stats["found"], stats["skipped"])

    p = load_progress()
    p.setdefault("completed", {})[progress_key] = {
        "found": stats["found"],
        "completed_at": datetime.now().isoformat(),
    }
    save_progress(p)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Bulk ATO ruling scraper")
    parser.add_argument("--types", help="Comma-separated types (TR,TD,PCG,... or ALL)", default="ALL")
    parser.add_argument("--start-year", type=int, help="Override start year")
    parser.add_argument("--end-year", type=int, help="Override end year")
    args = parser.parse_args()

    types_to_scrape = []
    if args.types == "ALL":
        types_to_scrape = list(TYPES.keys())
    else:
        for t in args.types.split(","):
            t = t.strip().upper()
            if t in TYPES:
                types_to_scrape.append(t)
            else:
                log.warning("Unknown type: %s (skipping)", t)

    total_found = 0
    total_skipped = 0
    total_errors = 0

    for rtype in types_to_scrape:
        cfg = TYPES[rtype]
        log.info("")

        if rtype in SEQUENTIAL_TYPES:
            log.info("Scraping %s (sequential)...", rtype)
            s = scrape_sequential(rtype, cfg, load_progress())
            total_found += s["found"]
            total_skipped += s.get("skipped", 0)
            total_errors += s["errors"]
            continue

        start_yr = args.start_year or cfg["start"]
        end_yr = args.end_year or cfg["end"]

        log.info("Scraping %s (%s → %s)...", rtype, start_yr, end_yr)
        for year in range(end_yr, start_yr - 1, -1):  # newest first
            s = scrape_year(rtype, cfg, year, load_progress())
            total_found += s["found"]
            total_skipped += s.get("skipped", 0)
            total_errors += s["errors"]

    log.info("")
    log.info("=" * 60)
    log.info("BULK SCRAPE COMPLETE")
    log.info("  New: %s  Skipped: %s  Errors: %s", total_found, total_skipped, total_errors)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
