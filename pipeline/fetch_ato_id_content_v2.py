#!/usr/bin/env python3
"""
Fetch full document text for ATO ID (Interpretative Decision) placeholders.

The ATO legal database SPA at /law/view/document returns full server-rendered
document content when accessed via curl_cffi with Chrome impersonation.
We extract the content from the HTML using BeautifulSoup.

~5,931 files across years 2001-2016. Sequential at ~2 rps.
Resume capability: skip files with content > 100 chars.
"""

from __future__ import annotations
import os

import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

RULINGS_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "rulings"))

# Verified upper bounds (matching scrape_ato_ids.py)
YEAR_MAX = {
    2001: 805, 2002: 1116, 2003: 1200, 2004: 982,
    2005: 368, 2006: 341, 2007: 226, 2008: 166,
    2009: 161, 2010: 228, 2011: 107, 2012: 100,
    2013: 67, 2014: 44, 2015: 25, 2016: 1,
}

DELAY_S = 0.5  # ~2 rps
MAX_RETRIES = 4
BACKOFF_BASE_S = 5.0


def has_content(path: Path) -> bool:
    """Check if file has actual content beyond just the placeholder header."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return len(text) > 100


def extract_document_text(html: str, year: int, num: int) -> str | None:
    """
    Extract the full ATO ID document text from the HTML page.

    Returns None if the page is an error page (doc doesn't exist).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Check for error page
    title_tag = soup.find("title")
    if title_tag and "Error" in title_tag.get_text():
        return None  # Document doesn't exist

    # Find the ATO ID heading - this marks the document content start
    h2 = soup.find("h2", class_="text-left")
    if not h2:
        # Fallback: find any h2 containing "ATO ID"
        for h in soup.find_all("h2"):
            if "ATO ID" in h.get_text():
                h2 = h
                break
    if not h2:
        return None

    # Collect all content from after h2 until 'Tools' h3 (sidebar nav)
    content_parts = []
    for sib in h2.find_next_siblings():
        if sib.name == "h3" and sib.get_text(strip=True) in ("Tools", "tools"):
            break
        # Skip sidebar/nav elements
        if sib.name == "nav" or (isinstance(sib.get("class"), list) and "side-nav" in sib.get("class", [])):
            continue
        content_parts.append(sib)

    # Reconstruct clean text
    doc_html = "".join(str(p) for p in content_parts)
    doc_soup = BeautifulSoup(doc_html, "html.parser")
    text = doc_soup.get_text()

    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" +\n", "\n", text)
    text = text.strip()

    if not text:
        return None

    return text


def format_output(text: str, year: int, num: int) -> str:
    """Format the final document content with header."""
    header = (
        f"ATO Interpretative Decision\n"
        f"ATO ID {year}/{num}\n"
        f"{'=' * 50}\n"
    )
    return f"{header}\n{text}\n"


def fetch_document(year: int, num: int) -> str | None:
    """Fetch ATO ID document content. Returns formatted text or None."""
    url = f"https://www.ato.gov.au/law/view/document?docid=AID/AID{year}{num}/00001"

    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, impersonate="chrome", timeout=30)
        except Exception as e:
            print(f"  req failed: {e}", flush=True)
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE_S * (2**attempt))
                continue
            return None

        body = r.text[:2000].lower()
        if "access denied" in body or "legal database unavailable" in body:
            print(f"  blocked by Akamai", flush=True)
            if attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE_S * (2**attempt)
                print(f"  backing off {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            return None

        if r.status_code in (403, 429, 503):
            if attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE_S * (2**attempt)
                print(f"  http {r.status_code}, backing off {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            return None

        # Extract document text
        text = extract_document_text(r.text, year, num)
        if text is None:
            return None

        return format_output(text, year, num)

    return None


def main() -> None:
    from_year = 2001
    if "--from-year" in sys.argv:
        i = sys.argv.index("--from-year")
        from_year = int(sys.argv[i + 1])

    # Count what needs doing
    total_existing = 0
    tasks: list[tuple[int, int]] = []
    for year, mx in YEAR_MAX.items():
        if year < from_year:
            continue
        for num in range(1, mx + 1):
            path = RULINGS_DIR / f"AID_{year}_{num}.txt"
            if path.exists():
                total_existing += 1
                if not has_content(path):
                    tasks.append((year, num))
            else:
                print(f"  Missing placeholder file: AID_{year}_{num}.txt", flush=True)

    print(f"Found {total_existing} existing placeholder files", flush=True)
    print(f"Need to fetch content for {len(tasks)} ATO IDs (from year {from_year})", flush=True)

    if not tasks:
        print("All ATO IDs already have full content!", flush=True)
        return

    ok = skip = err = 0
    t0 = time.time()

    for i, (year, num) in enumerate(tasks, 1):
        path = RULINGS_DIR / f"AID_{year}_{num}.txt"
        result = fetch_document(year, num)

        if result:
            path.write_text(result, encoding="utf-8")
            ok += 1
            status = "OK"
        else:
            skip += 1
            status = "SKIP"
            # Remove placeholder since doc doesn't exist
            if path.exists():
                path.unlink()

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

    elapsed = time.time() - t0
    print(
        f"\nDone. ok={ok} skip={skip} err={err} "
        f"total={len(tasks)} in {elapsed/60:.1f}m",
        flush=True,
    )


if __name__ == "__main__":
    main()
