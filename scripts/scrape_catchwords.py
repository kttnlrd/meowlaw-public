"""Scrape catchwords for all tax cases from official court sources."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi.requests import AsyncSession

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "case_catchwords.json"

COURT_FILES = {
    "hca": DATA_DIR / "hca_tax_cases.json",
    "fca": DATA_DIR / "fca_tax_cases.json",
    "fcafc": DATA_DIR / "fcafc_tax_cases.json",
}

CITATION_RE = re.compile(r"\[(\d{4})\]\s+(\S+)\s+(\d+)")

# Regex patterns
HCA_CATCHWORDS_RE = re.compile(
    r'<div[^>]*class="[^"]*field--name-field-hca-catchwords[^"]*"[^>]*>.*?<div[^>]*class="[^"]*field__item[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.DOTALL,
)
FCA_CATCHWORDS_RE = re.compile(
    r'<meta[^>]*name="Catchwords"[^>]*content="([^"]*)"',
    re.IGNORECASE,
)


def clean_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&#039;", "'")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_fetch_url(court: str, case: dict) -> str | None:
    """Determine the best URL to fetch catchwords from."""
    if court == "hca":
        href = case.get("href", "")
        if href:
            return f"https://www.hcourt.gov.au{href}"
    elif court in ("fca", "fcafc"):
        raw_url = case.get("url", "")
        if raw_url:
            clean = raw_url.replace("&amp;", "&")
            parsed = urlparse(clean)
            qs = parse_qs(parsed.query)
            target = qs.get("url", [""])[0]
            if target:
                return target
    return None


async def fetch_catchwords(session: AsyncSession, court: str, case: dict) -> tuple[str, str | None]:
    """Fetch catchwords for a single case. Returns (citation, catchwords_or_None)."""
    citation = case.get("citation", "")
    url = get_fetch_url(court, case)
    if not url:
        return citation, None

    try:
        resp = await session.get(url, impersonate="chrome", timeout=15)
        if resp.status_code != 200:
            return citation, None

        text = resp.text

        if court == "hca":
            m = HCA_CATCHWORDS_RE.search(text)
            if m:
                return citation, clean_html(m.group(1))
        elif court in ("fca", "fcafc"):
            m = FCA_CATCHWORDS_RE.search(text)
            if m:
                return citation, clean_html(m.group(1))

        return citation, None
    except Exception as e:
        return citation, None


async def scrape_all():
    """Main scraping routine."""
    # Load existing results to resume
    results: dict[str, str] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            results = json.load(f)
        print(f"Loaded {len(results)} existing catchwords, resuming...")

    # Build work list
    tasks = []
    async with AsyncSession() as session:
        for court, path in COURT_FILES.items():
            if not path.exists():
                continue
            with open(path) as f:
                cases = json.load(f)
            for case in cases:
                citation = case.get("citation", "")
                if not citation or citation in results:
                    continue
                tasks.append((court, case))

        total = len(tasks)
        print(f"Cases to fetch: {total}")

        if not total:
            print("Nothing to do.")
            return

        # Process with concurrency limit
        semaphore = asyncio.Semaphore(10)
        fetched = 0
        found = 0

        async def worker(court: str, case: dict):
            nonlocal fetched, found
            async with semaphore:
                citation, catchwords = await fetch_catchwords(session, court, case)
                fetched += 1
                if catchwords:
                    results[citation] = catchwords
                    found += 1
                # Save progress every 50 cases
                if fetched % 50 == 0:
                    with open(OUTPUT_FILE, "w") as f:
                        json.dump(results, f, indent=2)
                    print(f"  Progress: {fetched}/{total} ({found} found, {len(results)} total saved)")

        # Run all tasks
        await asyncio.gather(*[worker(c, case) for c, case in tasks])

    # Final save
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nDone. Total cases with catchwords: {len(results)}")


if __name__ == "__main__":
    asyncio.run(scrape_all())
