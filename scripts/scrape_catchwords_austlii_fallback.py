"""Fallback: scrape catchwords from AustLII for cases missing official catchwords."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from curl_cffi.requests import AsyncSession

DATA_DIR = Path(__file__).parent.parent / "data"
OFFICIAL_FILE = DATA_DIR / "case_catchwords.json"
OUTPUT_FILE = DATA_DIR / "case_catchwords.json"

COURT_FILES = {
    "hca": DATA_DIR / "hca_tax_cases.json",
    "fca": DATA_DIR / "fca_tax_cases.json",
    "fcafc": DATA_DIR / "fcafc_tax_cases.json",
}

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


def extract_austlii_catchwords(html: str) -> str | None:
    """Try multiple patterns to extract catchwords from AustLII HTML."""

    # Pattern 1: HCA with <br> tags - CATCHWORDS, case name, then catchwords
    # <p><b>CATCHWORDS</b><br><br><br><br><b>Case Name</b><br><br><br><br>catchwords...
    m = re.search(
        r'<b>CATCHWORDS</b>(?:<br>\s*)+<b>[^<]+</b>(?:<br>\s*)+(.*?)(?=<p><b>|<center>|</body>)',
        html, re.DOTALL | re.IGNORECASE
    )
    if m:
        cleaned = clean_html(m.group(1))
        if len(cleaned) > 30:
            return cleaned

    # Pattern 2: HCA/older with <p> tags - CATCHWORDS then content before next bold heading
    m = re.search(
        r'<p>\s*<b>CATCHWORDS</b>\s*</p>\s*<p>(.*?)</p>\s*(?:<p>\s*<p>\s*<b>|<p>\s*<b>[A-Z])',
        html, re.DOTALL | re.IGNORECASE
    )
    if m:
        cleaned = clean_html(m.group(1))
        if len(cleaned) > 30:
            return cleaned

    # Pattern 3: HCA/older - CATCHWORDS then content before next <p><b>XXX</b></p>
    m = re.search(
        r'<p>\s*<b>CATCHWORDS</b>\s*</p>\s*(.*?)(?=<p>\s*<b>(?:HEARING|ORDER|DECISION|JUDGMENT|PROCEDURE|PRACTICE))',
        html, re.DOTALL | re.IGNORECASE
    )
    if m:
        cleaned = clean_html(m.group(1))
        if len(cleaned) > 30:
            return cleaned

    # Pattern 4: FCA/FCAFC table style with <a name="Catchwords">
    m = re.search(
        r'<div>Catchwords:</div>\s*</td>\s*<td[^>]*>\s*<div>\s*<a\s+name="Catchwords"></a>\s*(.*?)</div>\s*</td>',
        html, re.DOTALL | re.IGNORECASE
    )
    if m:
        cleaned = clean_html(m.group(1))
        if len(cleaned) > 30:
            return cleaned

    # Pattern 5: Broader FCA table style
    m = re.search(
        r'Catchwords[\s:]*</div>\s*</td>\s*<td[^>]*>\s*<div[^>]*>(.*?)</div>\s*</td>',
        html, re.DOTALL | re.IGNORECASE
    )
    if m:
        cleaned = clean_html(m.group(1))
        if len(cleaned) > 30:
            return cleaned

    return None


async def fetch_austlii_catchwords(session: AsyncSession, citation: str) -> str | None:
    url = get_austlii_url(citation)
    if not url:
        return None
    try:
        resp = await session.get(url, impersonate="chrome", timeout=15)
        if resp.status_code != 200:
            return None
        return extract_austlii_catchwords(resp.text)
    except Exception:
        return None


async def main():
    results: dict[str, str] = {}
    if OFFICIAL_FILE.exists():
        with open(OFFICIAL_FILE) as f:
            results = json.load(f)
    print(f"Loaded {len(results)} existing catchwords")

    missing = []
    for court, path in COURT_FILES.items():
        with open(path) as f:
            cases = json.load(f)
        for case in cases:
            citation = case.get("citation", "")
            if citation and citation not in results:
                missing.append(citation)

    print(f"Missing catchwords: {len(missing)}")

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
                catchwords = await fetch_austlii_catchwords(session, citation)
                fetched += 1
                if catchwords:
                    results[citation] = catchwords
                    found += 1
                if fetched % 50 == 0:
                    with open(OUTPUT_FILE, "w") as f:
                        json.dump(results, f, indent=2)
                    print(f"  Progress: {fetched}/{len(missing)} ({found} found, {len(results)} total)")

        await asyncio.gather(*[worker(c) for c in missing])

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nDone. Total cases with catchwords: {len(results)}")


if __name__ == "__main__":
    asyncio.run(main())
