"""Scrape catchwords for missing HCA cases from AustLII (pre-1998 format)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from curl_cffi.requests import AsyncSession

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "case_catchwords.json"

CITATION_RE = re.compile(r"\[(\d{4})\]\s+(\S+)\s+(\d+)")


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&#8211;", "–")
    text = text.replace("&#8212;", "—")
    text = text.replace("&#039;", "'")
    text = text.replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_austlii_url(citation: str) -> str | None:
    m = CITATION_RE.match(citation)
    if not m:
        return None
    year, court, number = m.groups()
    return f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/{court}/{year}/{number}.html"


def extract_catchwords(html: str) -> str | None:
    """Extract catchwords from old AustLII HCA format."""
    # Find CATCHWORDS heading
    idx = html.find("<b><u>CATCHWORDS</u></b>")
    if idx == -1:
        idx = html.find("<b><u> CATCHWORDS </u></b>")
    if idx == -1:
        return None

    # Extract section from CATCHWORDS until next major heading
    section = html[idx:idx + 4000]
    end_match = re.search(
        r'<b>\s*<u>\s*(?:HEARING|ORDER|DECISION|PROCEDURE|JUDGMENT|REASONS)',
        section,
        re.IGNORECASE,
    )
    if end_match:
        section = section[: end_match.start()]

    cleaned = clean_html(section)
    # Remove the "CATCHWORDS" prefix
    cleaned = re.sub(r"^CATCHWORDS\s*", "", cleaned, flags=re.IGNORECASE)
    if len(cleaned) > 30:
        return cleaned
    return None


async def fetch_catchwords(session: AsyncSession, citation: str) -> str | None:
    url = get_austlii_url(citation)
    if not url:
        return None
    try:
        resp = await session.get(url, impersonate="chrome", timeout=15)
        if resp.status_code != 200:
            return None
        return extract_catchwords(resp.text)
    except Exception:
        return None


async def main():
    results: dict[str, str] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            results = json.load(f)
    print(f"Loaded {len(results)} existing catchwords")

    with open(DATA_DIR / "hca_tax_cases.json") as f:
        hca = json.load(f)

    missing = [c for c in hca if c["citation"] not in results]
    print(f"Missing HCA catchwords: {len(missing)}")

    if not missing:
        print("Nothing to do.")
        return

    semaphore = asyncio.Semaphore(8)
    fetched = 0
    found = 0

    async with AsyncSession() as session:
        async def worker(citation: str):
            nonlocal fetched, found
            async with semaphore:
                catchwords = await fetch_catchwords(session, citation)
                fetched += 1
                if catchwords:
                    results[citation] = catchwords
                    found += 1
                if fetched % 50 == 0:
                    with open(OUTPUT_FILE, "w") as f:
                        json.dump(results, f, indent=2)
                    print(f"  Progress: {fetched}/{len(missing)} ({found} found, {len(results)} total)")

        await asyncio.gather(*[worker(c["citation"]) for c in missing])

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nDone. Total cases with catchwords: {len(results)}")


if __name__ == "__main__":
    asyncio.run(main())
