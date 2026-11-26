#!/usr/bin/env python3
"""Fetch full text for ATO ID placeholder files via the print view.

The ATO's server-rendered print view at /law/view/print returns actual
document content (no SPA, no JavaScript). URL format:

  https://www.ato.gov.au/law/view/print?DocID=AID/AID{year}{num}/00001

~0.5s per doc ≈ ~50 min for all 5,931. Resume-capable.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests  # Akamai TLS fingerprint bypass

RULINGS_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "rulings"))

DELAY_S = 0.4
MAX_RETRIES = 3
BACKOFF_BASE = 3.0

# Matches scrape_ato_ids.py
YEAR_MAX = {
    2001: 805, 2002: 1116, 2003: 1200, 2004: 982,
    2005: 368, 2006: 341, 2007: 226, 2008: 166,
    2009: 161, 2010: 228, 2011: 107, 2012: 100,
    2013: 67, 2014: 44, 2015: 25, 2016: 1,
}


def needs_content(path: Path) -> bool:
    if not path.exists():
        return True
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    # Placeholder is ~43 chars (2 lines). Content is 500+ chars.
    return len(text) < 200


def fetch_print_text(year: int, num: int) -> str | None:
    """Fetch ATO ID via print view, extract body text.
    Returns None if document doesn't exist or rate-limited."""
    url = f"https://www.ato.gov.au/law/view/print?DocID=AID/AID{year}{num}/00001"
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, impersonate="chrome", timeout=30)
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            return None

        if r.status_code != 200 or "page not found" in r.text.lower():
            return None

        # Extract body text from the HTML
        m = re.search(r"<body[^>]*>(.*?)</body>", r.text, re.DOTALL | re.I)
        if not m:
            return None

        body = m.group(1)
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 200:
            # Probably an error page with no real content
            return None

        return text

    return None


def main():
    from_year = 2001
    if "--from-year" in sys.argv:
        i = sys.argv.index("--from-year")
        from_year = int(sys.argv[i + 1])

    # Find all ATO IDs that need content
    tasks: list[tuple[int, int]] = []
    for year, mx in YEAR_MAX.items():
        if year < from_year:
            continue
        for num in range(1, mx + 1):
            path = RULINGS_DIR / f"AID_{year}_{num}.txt"
            if needs_content(path):
                tasks.append((year, num))

    print(f"Need to fetch content for {len(tasks)} ATO IDs (from {from_year})", flush=True)

    if not tasks:
        print("All ATO IDs already have content!", flush=True)
        return

    ok = skip = 0
    t0 = time.time()

    for i, (year, num) in enumerate(tasks, 1):
        path = RULINGS_DIR / f"AID_{year}_{num}.txt"
        text = fetch_print_text(year, num)

        if text:
            # Validate the text actually mentions this ATO ID
            if f"ATO ID {year}/{num}" not in text:
                # Could be a redirect. Check if it's the right doc before writing.
                skip += 1
                status = "MISMATCH"
            else:
                # Write full content with header preserved
                path.write_text(text, encoding="utf-8")
                ok += 1
                status = "OK"
        else:
            skip += 1
            status = "404"
            # Remove placeholder if it was for a non-existent doc
            if path.exists() and path.read_text().strip() == f"ATO Interpretative Decision\nATO ID {year}/{num}":
                path.unlink()  # removes the empty placeholder

        if i % 10 == 0 or i == len(tasks):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta_s = (len(tasks) - i) / rate if rate else 0
            print(
                f"[{i}/{len(tasks)}] {year}/{num} {status} | "
                f"ok={ok} skip={skip} | {rate:.1f}/s eta={eta_s/60:.1f}m",
                flush=True,
            )

        time.sleep(DELAY_S)

    print(f"\nDone. ok={ok} skip={skip} total={len(tasks)}", flush=True)


if __name__ == "__main__":
    main()
