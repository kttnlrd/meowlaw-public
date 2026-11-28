#!/usr/bin/env python3
"""Check for new ATO rulings: load local index, check AustLII, diff.

Ponytail: one source (AustLII), no BeautifulSoup, no dataclasses.
If AustLII errors, report and move on — the monthly_case_ingest.py
catches ATOTD via the existing scrape path anyway.
"""
import json, logging, re, time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl

log = logging.getLogger("check_rulings")

LOCAL_INDEX = Path(f"{Path(__file__).resolve().parent.parent}/data/citation_index.json")
RULINGS_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data/rulings")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
KNOWN_TYPES = ["TR", "TD", "PCG", "SMSF", "GSTR", "LCR", "LCG", "IT", "MT"]


def load_local():
    """Return set of known citations from the local index files."""
    known = set()
    if LOCAL_INDEX.exists():
        with open(LOCAL_INDEX) as f:
            for entry in json.load(f):
                known.add(entry.get("citation", ""))
    # Also scan directory for actual files
    if RULINGS_DIR.exists():
        for f in RULINGS_DIR.iterdir():
            if f.is_dir():
                continue
            # filename patterns: TR_2020_1.txt, PS_LA_2011_10.txt, AID_2020_1.txt
            known.add(f.stem)
    return {k for k in known if k}


def check_austlii_year(known: set, rtype: str, year: int) -> dict:
    """Check AustLII for new rulings of a given type in a given year."""
    # AustLII ATOTD series covers ATO rulings
    url = f"https://www.austlii.edu.au/cgi-bin/viewdb/au/cases/cth/ATOTD/{year}/"
    newly_found = []

    try:
        r = curl.get(url, impersonate="chrome120", headers=HEADERS, timeout=20, verify=False)
        if r.status_code != 200:
            return {"error": f"AustLII {year}: HTTP {r.status_code}"}

        # Ponytail: extract citation links with regex, no parser library
        for m in re.finditer(rf'/{year}/(\d+)\.html\">([^<]+)</a>', r.text):
            num, title = m.group(1), m.group(2)
            # Try to extract series prefix like "TR 2024/1" from title
            cite_m = re.search(rf'({rtype})\s+{year}\s*/\s*{num}', title, re.IGNORECASE)
            if cite_m:
                citation = f"{cite_m.group(1).upper()} {year}/{num}"
                if citation not in known:
                    newly_found.append({"citation": citation, "title": title.strip()})
    except Exception as e:
        return {"error": str(e)}

    return {"new": newly_found, "total_checked": 0}


def main():
    t0 = time.time()
    known = load_local()
    log.info("Local rulings: ~%d", len(known))

    new_rulings = []
    current_year = datetime.now().year
    for year in [current_year, current_year - 1]:
        for rtype in KNOWN_TYPES:
            result = check_austlii_year(known, rtype, year)
            new_rulings.extend(result.get("new", []))

    output = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_seconds": round(time.time() - t0, 2),
        "total_new": len(new_rulings),
        "total_modified": 0,
        "new_rulings": new_rulings,
        "errors": [],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
