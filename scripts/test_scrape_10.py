#!/usr/bin/env python3
"""Test scraping 10 ATO rulings via print view + curl_cffi."""
import os
import re, time, sys
from pathlib import Path
from curl_cffi import requests as curl

RULINGS_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "rulings"))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

# DocID prefix map (from data_loader.py _ato_doc_map)
DOC_PREFIX = {
    "TR": "TXR/TR", "TD": "TXD/TD", "PCG": "COG/PCG", "LCG": "COG/LCG",
    "GSTR": "GST/GSTR", "PS": "PSR/PS", "SGR": "SGR/SGR",
    "AID": "AID/AID", "IT": "ITR/IT", "MT": "MXR/MT", "TA": "TPA/TA",
}

candidates = [
    ("TR", 2001, 1),
    ("TR", 2008, 1),
    ("TD", 2018, 1),
    ("PCG", 2020, 2),
    ("LCG", 2016, 1),
    ("GSTR", 2022, 1),
    ("PS", 2018, 1),   # PS LA 2018/1
    ("TR", 1996, 1),
    ("TD", 2014, 1),
    ("PCG", 2018, 1),
]

# Check what already exists
existing = set(f.stem for f in RULINGS_DIR.glob("*.txt"))

results = {"ok": 0, "skip": 0, "fail": 0, "errors": []}

for rtype, year, num in candidates:
    if rtype == "PS":
        fname = f"PS_LA_{year}_{num}"
    else:
        fname = f"{rtype}_{year}_{num}"

    if fname in existing:
        print(f"⏭️  {fname} — already exists")
        results["skip"] += 1
        continue

    prefix = DOC_PREFIX.get(rtype, rtype)
    doc_id = f"{prefix}{year}{num}"
    url = f"https://www.ato.gov.au/law/view/print?DocID={doc_id}/NAT/ATO/00001"

    print(f"⬇️  {fname}  ({url})", end="", flush=True)

    try:
        r = curl.get(url, impersonate="chrome120", headers=HEADERS, timeout=20, verify=False)
    except Exception as e:
        print(f" ❌ REQUEST ERROR: {e}")
        results["fail"] += 1
        results["errors"].append(f"{fname}: {e}")
        time.sleep(2)
        continue

    # Check for error page
    title_m = re.search(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
    title = title_m.group(1) if title_m else ""
    if r.status_code != 200 or "Error" in title:
        print(f" ❌ NOT FOUND (HTTP {r.status_code}, title: {title[:60]})")
        results["fail"] += 1
        results["errors"].append(f"{fname}: HTTP {r.status_code}")
        time.sleep(2)
        continue

    # Extract body text (strip HTML)
    text = re.sub(r'<[^>]+>', ' ', r.text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'  [0-9]+  ', '\n', text)  # page numbers on their own line

    # Save
    txt_path = RULINGS_DIR / f"{fname}.txt"
    txt_path.write_text(text, encoding="utf-8")

    size = len(text)
    print(f" ✅ ({size:,} chars)", flush=True)
    results["ok"] += 1
    time.sleep(2)  # rate limit

print(f"\n=== Results: {results['ok']} OK, {results['skip']} skipped, {results['fail']} failed ===")
if results["errors"]:
    for e in results["errors"]:
        print(f"  ❌ {e}")
