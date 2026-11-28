#!/usr/bin/env python3
"""Discover the ATO lawservices search query parameter.

Slow, paced probe: warms up with a fresh cookie jar, then for each
candidate param name POSTs the search API with and without a distinctive
query term and compares the first-page docids. A param that changes the
result set is the search param.

Usage: python3 probe_ato_search.py
"""
import re, subprocess, time, sys

API = "https://www.ato.gov.au/API/v1/law/lawservices/result"
LAW = "https://www.ato.gov.au/law/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
JAR = "/tmp/ato_probe_ck.txt"
TERM = "zebraunicornnonsense"

def curl(args, timeout=25):
    try:
        p = subprocess.run(["curl", "-s", "-m", str(timeout)] + args,
                           capture_output=True, text=True, timeout=timeout + 5)
        return p.stdout
    except Exception:
        return ""

def warm_up():
    for attempt in range(30):  # up to ~45 min
        try:
            subprocess.run(["curl", "-s", "-m", "15", "-c", JAR, "-A", UA, LAW, "-o", "/dev/null"],
                           timeout=20, check=True)
        except Exception:
            pass
        body = curl(["-c", JAR, "-A", UA, LAW])
        if len(body) > 500:
            print(f"warm-up OK ({len(body)} bytes)", flush=True)
            return True
        print(f"warm-up blocked ({len(body)}b), waiting 90s", flush=True)
        time.sleep(90)
    return False

def post(params):
    args = ["-b", JAR, "-c", JAR, "-A", UA,
            "-H", "Referer: https://www.ato.gov.au/law/",
            "-X", "POST", API,
            "--data-urlencode", "src=qa",
            "--data-urlencode", "stype=find",
            "--data-urlencode", "pit=99991231235958",
            "--data-urlencode", "df=",
            "--data-urlencode", "pageSize=8",
            "--data-urlencode", "start=1"]
    for k, v in params:
        args += ["--data-urlencode", f"{k}={v}"]
    return curl(args)

def docids(body):
    return re.findall(r'docid=([A-Za-z0-9%/]+)', body)[:8]

def main():
    if not warm_up():
        print("FAIL: never warmed up", flush=True)
        return 1
    base = docids(post([]))
    print(f"baseline docids: {base}", flush=True)
    cands = ["query", "ft", "txt", "q", "search", "keyword", "dkt", "words",
             "freetext", "freeText", "searchText", "criteria", "srch", "text"]
    for p in cands:
        time.sleep(3.5)  # pacing ~0.28 rps
        body = post([(p, TERM)])
        got = docids(body)
        changed = got != base and len(got) > 0
        print(f"param {p!r:14s}: {'CHANGED ✓' if changed else 'same'} {got}", flush=True)
        if changed:
            print(f"RESULT: query param is {p!r}", flush=True)
            return 0
    print("RESULT: no single param changed the set (may need combos or different stype)", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
