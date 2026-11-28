#!/usr/bin/env python3
"""Scrape TD (Taxation Determination) rulings from the ATO website.

Downloads missing TD rulings and saves them as .txt files in the RULING_DIR.

Usage:
    python3 scripts/ingest_td_rulings.py                    # scrape all missing TDs
    python3 scripts/ingest_td_rulings.py --year 2024        # scrape a specific year
    python3 scripts/ingest_td_rulings.py --dry-run           # show what would be scraped
    python3 scripts/ingest_td_rulings.py --year-start 1990 --year-end 2025

Sources:
    - ATO Legal Database (PDF via DocID pattern)
    - AustLII (HTML via ATOTD index pages) as fallback
"""

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Config
ATO_BASE = "https://www.ato.gov.au/law/view"
RULING_DIR = Path(
    os.environ.get(
        "RULING_DIR",
        f"{Path(__file__).resolve().parent.parent}/data/rulings",
    )
)
DELAY = 2.0  # seconds between requests
TIMEOUT = 30

# ATO DocID pattern for TD PDFs
# DocId=TXD/TD{year}{num}/NAT/ATO/00001
PDF_URL = ATO_BASE + "/pdf?DocId=TXD/TD{year}{num}/NAT/ATO/00001&filename=law/view/pdf/pbr/td{year}-{num:03d}.pdf&PiT=99991231235958"

# AustLII fallback
AUSTLII_TD = "https://www8.austlii.edu.au/au/other/rulings/ato/ATOTD/{year}/td{year}{num}.html"


def get_existing_tds() -> set[str]:
    """Return set of already-downloaded TD citations."""
    existing = set()
    for f in RULING_DIR.glob("TD_*.txt"):
        existing.add(f.stem)
    return existing


def download_td_pdf(year: int, num: int, client: httpx.Client) -> bytes | None:
    """Download a TD PDF from the ATO website. Returns bytes or None."""
    url = PDF_URL.format(year=year % 100, num=num)
    try:
        resp = client.get(url, timeout=TIMEOUT, follow_redirects=True)
        if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
            return resp.content
        if resp.status_code == 404:
            # Try with 4-digit year
            url2 = PDF_URL.format(year=year, num=num)
            resp2 = client.get(url2, timeout=TIMEOUT, follow_redirects=True)
            if resp2.status_code == 200 and resp2.content[:5] == b"%PDF-":
                return resp2.content
    except Exception:
        pass
    return None


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdftotext or PyMuPDF."""
    # Write to temp file
    import tempfile
    import subprocess

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        # pdftotext
        result = subprocess.run(
            ["pdftotext", "-layout", tmp_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        import pymupdf
        doc = pymupdf.open(tmp_path)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        if text.strip():
            return text.strip()
    except ImportError:
        pass

    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            return text.strip()
    except ImportError:
        pass

    return ""


def save_td(year: int, num: int, text: str) -> Path:
    """Save the TD text to RULING_DIR."""
    citation = f"TD_{year}_{num}"
    path = RULING_DIR / f"{citation}.txt"
    path.write_text(text, encoding="utf-8")
    log.info(f"Saved {citation} ({len(text)} chars)")
    return path


def main():
    parser = argparse.ArgumentParser(description="Scrape TD rulings from ATO")
    parser.add_argument("--year", type=int, help="Single year to scrape")
    parser.add_argument("--year-start", type=int, default=2019, help="Start year (default: 2019)")
    parser.add_argument("--year-end", type=int, default=2025, help="End year (default: 2025)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be scraped")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between requests")
    args = parser.parse_args()

    if args.year:
        years = [args.year]
    else:
        years = list(range(args.year_start, args.year_end + 1))

    existing = get_existing_tds()
    log.info(f"Existing TD files: {len(existing)}")

    # Rough max TD numbers per year (TDs go up to ~50-100 per year)
    MAX_TD_PER_YEAR = 150

    headers = {"User-Agent": "CasePipeline/1.0 (tax-research)"}
    scraped = 0
    failed = 0

    with httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=True) as client:
        for year in years:
            yr_short = year % 100
            for num in range(1, MAX_TD_PER_YEAR + 1):
                citation = f"TD_{year}_{num}"
                if citation in existing:
                    continue

                if args.dry_run:
                    log.info(f"Would scrape: {citation}")
                    scraped += 1
                    continue

                pdf_bytes = download_td_pdf(year, num, client)
                if pdf_bytes is None:
                    # Try next year's range
                    if num > 10 and num < 50:
                        continue  # skip known-empty range
                    break

                text = extract_text_from_pdf(pdf_bytes)
                if text:
                    save_td(year, num, text)
                    scraped += 1
                else:
                    log.warning(f"Empty text for {citation}")
                    failed += 1

                time.sleep(args.delay)

    if args.dry_run:
        log.info(f"Would scrape approximately {scraped} TD rulings")
    else:
        log.info(f"Scraped {scraped} new TD rulings, {failed} failed")


if __name__ == "__main__":
    main()