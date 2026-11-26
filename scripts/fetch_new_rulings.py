#!/usr/bin/env python3
"""Fetch the 20 newly-detected final rulings + verify drafts already saved.
Writes to data/rulings/ with sidecar meta. Detection source: /tmp/ruling_check_finals.json."""
import curl_cffi
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

RULINGS_DIR = f"{Path(__file__).resolve().parent.parent}/data/rulings"
DELAY = 1.5

def rtype_filename(rtype, year, num):
    return f"{rtype}_{year}_{num}"

def build_url(rtype, year, num):
    codes = {
        "CR": "CLR/CR", "LCG": "COG/LCG", "PR": "PRR/PR",
        "TR": "TXR/TR", "TD": "TXD/TD", "PCG": "COG/PCG", "GSTR": "GST/GSTR",
        "PS_LA": "PSR/PS", "TA": "TPA/TA", "MT": "MXR/MT", "SGR": "SGR/SGR",
    }
    code = codes[rtype]
    return f"https://www.ato.gov.au/law/view/print?DocID={code}{year}{num}/NAT/ATO/00001"

def normalize(html):
    t = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()

def extract_title(html):
    m = re.search(r"<h2[^>]*>\s*(CR|LCG|PR)\s+(\d{4})/(\d+)\s*</h2>", html, re.I)
    if m:
        return f"{m.group(1)} {m.group(2)}/{m.group(3)}"
    m2 = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    return m2.group(1).strip()[:200] if m2 else ""

def main():
    rep = json.load(open("/tmp/ruling_check_finals.json"))
    session = curl_cffi.requests.Session(impersonate="chrome120")
    saved = []
    for res in rep["results"]:
        rtype = res["type"]
        for item in res.get("new", []):
            year, num = item["year"], item["num"]
            fname = rtype_filename(rtype, year, num)
            txt_path = os.path.join(RULINGS_DIR, f"{fname}.txt")
            if os.path.exists(txt_path):
                saved.append((fname, "already", 0))
                continue
            url = build_url(rtype, year, num)
            try:
                r = session.get(url, timeout=30)
                if r.status_code != 200:
                    saved.append((fname, f"HTTP {r.status_code}", 0))
                    continue
                text = normalize(r.text)
                if len(text) < 100:
                    saved.append((fname, "SHORT", len(text)))
                    continue
                open(txt_path, "w").write(text)
                meta = {
                    "citation": f"{rtype} {year}/{num}",
                    "type": rtype,
                    "title": item.get("title", ""),
                    "fetched_at": datetime.now().isoformat(),
                    "source_url": url,
                }
                open(f"{txt_path}.meta.json", "w").write(json.dumps(meta, indent=2))
                saved.append((fname, "saved", len(text)))
                time.sleep(DELAY)
            except Exception as e:
                saved.append((fname, f"ERR {e}", 0))

    for fname, status, size in saved:
        print(f"  {status:10s} {size:6d} {fname}")
    print(f"\nTotal: {len(saved)} (saved={sum(1 for s in saved if s[1]=='saved')})")

if __name__ == "__main__":
    main()
