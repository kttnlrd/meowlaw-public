#!/usr/bin/env python3
"""Comprehensive test of all ATO ruling DocID formats."""
import re, time, json
from curl_cffi import requests as curl
from pathlib import Path

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Formats to test: (label, code, rtype_prefix, year_format)
# year_format: "4digit" = 2024, "2digit" = 24, "both" = test both
FORMATS = [
    # Standard binding rulings
    ("TR",  "TXR/TR",     "4digit"),
    ("TD",  "TXD/TD",     "4digit"),
    ("PCG", "COG/PCG",    "4digit"),
    ("LCG", "COG/LCG",    "4digit"),

    # GST rulings
    ("GSTR","GST/GSTR",   "4digit"),

    # Practice statements — two possible prefixes
    ("PS LA (ATOPSLA)",  "ATOPSLA/PS",  "4digit"),
    ("PS LA (PSR)",      "PSR/PS",      "4digit"),

    # ATO IDs
    ("AID", "AID/AID",    "4digit"),

    # Income Tax (sequential numbering, no year)
    # IT uses ITR/IT{num} format — test a known one

    # Taxpayer Alerts
    ("TA",  "TPA/TA",     "4digit"),

    # Miscellaneous Tax
    ("MT",  "MXR/MT",     "4digit"),

    # Super Guarantee Rulings
    ("SGR", "SGR/SGR",    "4digit"),

    # Types from bulk_ingest_rulings.py not in _ato_doc_map
    ("CR",  "CLR/CR",     "4digit"),
    ("PR",  "PRR/PR",     "4digit"),
]

# Test items: (label, code_prefix, year, num, year_format)
# Using years where we know rulings definitely exist
TESTS = [
    # TRs through the decades
    ("TR",  "TXR/TR",  1998, 17, "both"),   # TR 98/17 known to exist
    ("TR",  "TXR/TR",  2001, 1,  "4digit"), # TR 2001/1 — confirmed working
    ("TR",  "TXR/TR",  2014, 1,  "4digit"), # TR 2014/1
    ("TR",  "TXR/TR",  2024, 1,  "4digit"), # TR 2024/1 — confirmed working

    # TDs through decades
    ("TD",  "TXD/TD",  1999, 1,  "both"),   # TD 99/1
    ("TD",  "TXD/TD",  2010, 1,  "4digit"), # TD 2010/1
    ("TD",  "TXD/TD",  2024, 1,  "4digit"), # TD 2024/1

    # PCGs
    ("PCG", "COG/PCG", 2017, 1,  "4digit"), # PCG 2017/1
    ("PCG", "COG/PCG", 2020, 3,  "4digit"), # PCG 2020/3

    # LCGs
    ("LCG", "COG/LCG", 2016, 1,  "4digit"), # confirmed working
    ("LCG", "COG/LCG", 2021, 1,  "4digit"),

    # GSTRs
    ("GSTR","GST/GSTR", 2000, 1, "both"),   # GSTR 2000/1
    ("GSTR","GST/GSTR", 2014, 1, "4digit"), # confirmed working
    ("GSTR","GST/GSTR", 2020, 1, "4digit"),

    # PS LAs
    ("PS LA", "ATOPSLA/PS", 2005, 10, "4digit"), # PS LA 2005/10
    ("PS LA", "PSR/PS",     2005, 10, "4digit"), # same with PSR prefix

    # AIDs
    ("AID", "AID/AID", 2001, 100,  "4digit"), # AID 2001/100
    ("AID", "AID/AID", 2015, 1,    "4digit"),
    ("AID", "AID/AID", 2024, 1,    "4digit"),

    # IT (sequential, no year)
    # IT uses ITR/IT{num} — no year in the DocID
    ("IT",  "ITR/IT",  0, 1,   "noseq"),
    ("IT",  "ITR/IT",  0, 262, "noseq"),

    # TAs
    ("TA",  "TPA/TA",  2010, 1,  "4digit"),
    ("TA",  "TPA/TA",  2020, 1,  "4digit"),

    # MTs
    ("MT",  "MXR/MT",  2000, 1,  "both"),
    ("MT",  "MXR/MT",  2020, 1,  "4digit"),

    # SGRs
    ("SGR", "SGR/SGR", 2006, 1,  "4digit"),
    ("SGR", "SGR/SGR", 2020, 1,  "4digit"),

    # CR and PR (test if they exist)
    ("CR",  "CLR/CR",  2020, 1,  "4digit"),
    ("PR",  "PRR/PR",  2020, 1,  "4digit"),
]

def test_url(url, label):
    try:
        r = curl.get(url, impersonate="chrome120", headers=HEADERS, timeout=15, verify=False)
    except Exception as e:
        return {"status": "ERROR", "detail": str(e), "size": 0}

    title_m = re.search(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
    title = title_m.group(1) if title_m else ""

    if r.status_code == 200 and "Error" not in title:
        return {"status": "✅ OK", "title": title[:80], "size": len(r.text)}
    elif "Error" in title:
        return {"status": "❌ NOT FOUND", "title": title[:60], "size": len(r.text)}
    else:
        return {"status": f"❌ HTTP {r.status_code}", "title": title[:60], "size": len(r.text)}

results = []
for rtype, prefix, year, num, year_fmt in TESTS:
    if year_fmt == "both":
        # Test 4-digit year first
        doc_id = f"{prefix}{year}{num}"
        url = f"https://www.ato.gov.au/law/view/print?DocID={doc_id}/NAT/ATO/00001"
        label = f"{rtype} {year}/{num} (4-digit)"
        r = test_url(url, label)
        results.append({"label": label, "url": url, **r})
        time.sleep(0.5)

        # Test 2-digit year
        yr2 = year % 100
        doc_id2 = f"{prefix}{yr2}{num}"
        url2 = f"https://www.ato.gov.au/law/view/print?DocID={doc_id2}/NAT/ATO/00001"
        label2 = f"{rtype} {yr2}/{num} (2-digit)"
        r2 = test_url(url2, label2)
        results.append({"label": label2, "url": url2, **r2})
        time.sleep(0.5)

    elif year_fmt == "noseq":
        # No year in format (IT rulings)
        doc_id = f"{prefix}{num}"
        url = f"https://www.ato.gov.au/law/view/print?DocID={doc_id}/NAT/ATO/00001"
        label = f"{rtype} #{num} (no year)"
        r = test_url(url, label)
        results.append({"label": label, "url": url, **r})
        time.sleep(0.5)

    else:
        doc_id = f"{prefix}{year}{num}"
        url = f"https://www.ato.gov.au/law/view/print?DocID={doc_id}/NAT/ATO/00001"
        label = f"{rtype} {year}/{num}"
        r = test_url(url, label)
        results.append({"label": label, "url": url, **r})
        time.sleep(0.5)

# Print results grouped by type
print(f"\n{'='*80}")
print(f"ATO RULING PRINT VIEW — COMPREHENSIVE TEST RESULTS")
print(f"{'='*80}\n")

by_type = {}
for r in results:
    label = r["label"]
    rtype = label.split(" ")[0]
    if rtype not in by_type:
        by_type[rtype] = []
    by_type[rtype].append(r)

for rtype in sorted(by_type.keys()):
    print(f"\n── {rtype} ──")
    for r in by_type[rtype]:
        status = r["status"]
        label = r["label"]
        size = r["size"]
        if "✅" in status:
            print(f"  {status}  {label:40s} ({size:>6,} chars)")
        else:
            print(f"  {status}  {label:40s} ({size:>6,} chars)")
    time.sleep(0.2)

# Summary
ok = sum(1 for r in results if "✅" in r["status"])
fail = sum(1 for r in results if "❌" in r["status"])
err = sum(1 for r in results if "ERROR" in r["status"])
print(f"\n{'='*80}")
print(f"Total: {ok} OK, {fail} failed, {err} errors out of {len(results)} tests")
print(f"{'='*80}")

# Working formats summary
print(f"\nWORKING DOCID FORMATS (print view):")
seen = set()
for r in results:
    if "✅" in r["status"]:
        url = r["url"]
        # Extract the pattern
        m = re.search(r'DocID=(.+?)/NAT/ATO', url)
        if m:
            pat = m.group(1)
            base = re.sub(r'\d+$', '{year}{num}', pat)
            if base not in seen:
                print(f"  /law/view/print?DocID={base}/NAT/ATO/00001")
                seen.add(base)
