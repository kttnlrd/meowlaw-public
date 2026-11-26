#!/usr/bin/env python3
"""Second round: test edge cases for AID, SGR, PS LA, and discover numbering."""
import re, time
from curl_cffi import requests as curl

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def test(url, label):
    r = curl.get(url, impersonate="chrome120", headers=HEADERS, timeout=15, verify=False)
    title_m = re.search(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
    title = title_m.group(1) if title_m else ""
    ok = r.status_code == 200 and "Error" not in title
    status = "✅ OK" if ok else "❌ NOT FOUND"
    print(f"  {status}  {label:45s} HTTP {r.status_code} ({len(r.text):>6,} chars) {title[:60]}")
    return ok

print("=== AID — different DocID formats ===")
test("https://www.ato.gov.au/law/view/print?DocID=AID/AID2001100/NAT/ATO/00001", "AID 2001/100 (print view, uppercase)")
test("https://www.ato.gov.au/law/view/print?DocID=AID/AID20011/NAT/ATO/00001",  "AID 2001/1 (print view)")
test("https://www.ato.gov.au/law/view/document?docid=AID/AID2001100/00001",      "AID 2001/100 (document view, lower)")
test("https://www.ato.gov.au/law/view/document?DocID=AID/AID2001100/NAT/ATO/00001&PiT=99991231235958", "AID 2001/100 (doc view, PiT)")
test("https://www.ato.gov.au/law/view/document?DocID=AID/AID20011/NAT/ATO/00001&PiT=99991231235958",  "AID 2001/1 (doc view, PiT)")
time.sleep(1)

print("\n=== AID — sequential enumeration (2001) ===")
for num in [1, 2, 3, 100, 101, 200, 201, 300, 500, 1000]:
    test(f"https://www.ato.gov.au/law/view/document?docid=AID/AID2001{num}/00001", f"AID 2001/{num} (lower docid)")
    time.sleep(0.3)

print("\n=== SGR — try known numbers from existing files ===")
# We have SGR_2006_4, SGR_2005_4, SGR_2004_1, SGR_2003_1
for yr in [2003, 2004, 2005, 2006, 2010, 2015, 2020, 2024]:
    for num in [1, 4, 5, 10]:
        test(f"https://www.ato.gov.au/law/view/print?DocID=SGR/SGR{yr}{num}/NAT/ATO/00001", f"SGR {yr}/{num}")
        time.sleep(0.3)

print("\n=== PS LA — confirm PSR prefix ===")
for yr in [1998, 2005, 2011, 2018, 2022]:
    for num in [1, 10, 12]:
        test(f"https://www.ato.gov.au/law/view/print?DocID=PSR/PS{yr}{num}/NAT/ATO/00001", f"PS LA {yr}/{num}")
        time.sleep(0.3)

print("\n=== GSTR — sequential enumeration (2014) ===")
for num in [1, 2, 3, 4, 5, 6, 7, 8, 10, 15]:
    test(f"https://www.ato.gov.au/law/view/print?DocID=GST/GSTR2014{num}/NAT/ATO/00001", f"GSTR 2014/{num}")
    time.sleep(0.3)
