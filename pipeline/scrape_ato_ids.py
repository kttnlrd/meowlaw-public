#!/usr/bin/env python3
"""Scrape ATO ID (Interpretative Decision) citations from ATO legal database.

ATO ID format: AID {year}/{num}
URL: https://www.ato.gov.au/law/view/document?docid=AID/AID{year}{num}/00001
Range: 2001-2016

CRITICAL:
  - ATO is a Next.js SPA — always returns HTTP 200.
  - Plain curl gets rate-limited hard under concurrency; use curl_cffi chrome.
  - Detect existence via <title>: "ATO ID {year}/{num}" vs "Error | ATO Legal Database".
  - On rate-limit signals, back off and retry (don't count as 404).
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests  # Akamai TLS fingerprint bypass via chrome impersonate

RULINGS_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "rulings"))

# Verified upper bounds (exist at hi, next does not)
YEAR_RANGES = {
    2001: (1, 805),
    2002: (1, 1116),
    2003: (1, 1200),
    2004: (1, 982),
    2005: (1, 368),
    2006: (1, 341),
    2007: (1, 226),
    2008: (1, 166),
    2009: (1, 161),
    2010: (1, 228),
    2011: (1, 107),
    2012: (1, 100),
    2013: (1, 67),
    2014: (1, 44),
    2015: (1, 25),
    2016: (1, 1),
}

# Polite pacing. 1 req / 0.4s ≈ 2.5 rps. ~1800 remaining ≈ 12 min.
DELAY_S = 0.4
MAX_RETRIES = 4
BACKOFF_BASE_S = 5.0


def filename_for(year: int, num: int) -> Path:
    return RULINGS_DIR / f"AID_{year}_{num}.txt"


def write_placeholder(year: int, num: int) -> None:
    path = filename_for(year, num)
    path.write_text(f"ATO Interpretative Decision\nATO ID {year}/{num}\n", encoding="utf-8")


def classify(year: int, num: int) -> str:
    """Return 'exists', 'missing', or 'rate_limited'.

    exists   — title contains 'ATO ID {year}/{num}'
    missing  — title is 'Error | ATO Legal Database' (or similar real miss)
    rate_limited — empty/timeout/access denied/unavailable
    """
    url = f"https://www.ato.gov.au/law/view/document?docid=AID/AID{year}{num}/00001"
    try:
        r = requests.get(url, impersonate="chrome", timeout=20)
    except Exception as e:
        return f"rate_limited:exc:{type(e).__name__}"

    body_l = r.text[:2000].lower()
    if "access denied" in body_l or "legal database unavailable" in body_l:
        return "rate_limited:block"
    if r.status_code in (403, 429, 503):
        return f"rate_limited:http{r.status_code}"

    m = re.search(r"<title>(.*?)</title>", r.text, re.I | re.S)
    if not m:
        return "rate_limited:no_title"

    title = re.sub(r"\s+", " ", m.group(1)).strip()
    if re.search(rf"ATO ID {year}/{num}\b", title):
        return "exists"
    if title.lower().startswith("error") or "not found" in title.lower():
        return "missing"
    # Unexpected title — treat as rate/block rather than false 404
    return f"rate_limited:title:{title[:60]}"


def check_with_backoff(year: int, num: int) -> str:
    for attempt in range(MAX_RETRIES):
        result = classify(year, num)
        if not result.startswith("rate_limited"):
            return result
        wait = BACKOFF_BASE_S * (2 ** attempt)
        print(f"  rate-limit on {year}/{num} ({result}), backoff {wait:.0f}s", flush=True)
        time.sleep(wait)
    return "rate_limited"


def main() -> None:
    # Optional year filter: --from-year 2005
    from_year = 2005
    if "--from-year" in sys.argv:
        i = sys.argv.index("--from-year")
        from_year = int(sys.argv[i + 1])

    existing: set[tuple[int, int]] = set()
    for f in RULINGS_DIR.glob("AID_*.txt"):
        m = re.match(r"AID_(\d{4})_(\d+)\.txt$", f.name)
        if m:
            existing.add((int(m.group(1)), int(m.group(2))))

    print(f"Already have {len(existing)} ATO ID files", flush=True)
    print(f"Scraping from year {from_year}+ with curl_cffi chrome, delay={DELAY_S}s", flush=True)

    tasks: list[tuple[int, int]] = []
    for year, (lo, hi) in YEAR_RANGES.items():
        if year < from_year:
            continue
        for num in range(lo, hi + 1):
            if (year, num) not in existing:
                tasks.append((year, num))

    print(f"Need to check {len(tasks)} ATO IDs", flush=True)
    if not tasks:
        print("All done!")
        return

    found = not_found = rate_limited = 0
    t0 = time.time()

    for i, (year, num) in enumerate(tasks, 1):
        result = check_with_backoff(year, num)
        if result == "exists":
            write_placeholder(year, num)
            found += 1
            status = "OK"
        elif result == "missing":
            not_found += 1
            status = "miss"
        else:
            rate_limited += 1
            status = "RL"

        if i % 25 == 0 or i == len(tasks) or status == "OK":
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta_s = (len(tasks) - i) / rate if rate else 0
            print(
                f"[{i}/{len(tasks)}] {year}/{num} {status} | "
                f"found={found} miss={not_found} rl={rate_limited} | "
                f"{rate:.1f}/s eta={eta_s/60:.1f}m",
                flush=True,
            )

        time.sleep(DELAY_S)

    print(f"\nDone. found={found} missing={not_found} rate_limited={rate_limited}", flush=True)


if __name__ == "__main__":
    main()
