#!/usr/bin/env python3.12
"""Live-data smoke for a deployed LawKitty host. Prints each route's status plus a
payload preview you can eyeball; exits non-zero if any REQUIRED check fails.

    COOKIE=$(/opt/lawkitty/venv/bin/python scripts/mint_session.py)   # on prod
    python3.12 scripts/prod_smoke.py https://lawkitty.app "$COOKIE"
"""
import json
import sys
import urllib.error
import urllib.request

REQUIRED = [
    ("gate intact (no cookie -> 401)", "/api/acts", 401, False),
    ("info", "/api/info", 200, True),
    ("data version", "/api/data-version", 200, True),
    ("acts list", "/api/acts", 200, True),
    ("section 8-1 ITAA 1997", "/api/section/itaa-1997/8-1", 200, True),
    ("search 'deduction'", "/api/search?q=deduction", 200, True),
]
INFO = [
    ("rulings list", "/api/rulings-list"),
    ("private rulings", "/api/private-rulings?limit=5"),
    ("regulatory guides", "/api/regulatory-guides"),
    ("treaties", "/api/treaties"),
    ("cases", "/api/cases"),
    ("afsa guides", "/api/afsa-guides"),
    ("tax case 2025 FCAFC 116", "/api/tax-cases/case/2025_FCAFC_116"),
    ("hall of fame (public)", "/api/mcp-hall-of-fame"),
]


def fetch(url, cookie):
    # A UA is required: urllib's default UA trips the WAF (403).
    req = urllib.request.Request(url, headers={
        "User-Agent": "lawkitty-smoke/1.0",
        "Cookie": cookie,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def preview(body, cap=500):
    text = body.decode("utf-8", "replace")
    if len(body) < 20000:  # skip pretty-print on huge bodies (rulings list is ~5 MB)
        try:
            text = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        except Exception:
            pass
    return text[:cap] + ("\n…" if len(text) > cap else "")


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: prod_smoke.py <base-url> <session-cookie>")
    base, cookie = sys.argv[1].rstrip("/"), sys.argv[2]
    fails = 0
    for name, path, want, send in REQUIRED:
        code, body = fetch(base + path, cookie if send else "")
        ok = code == want
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {code}  {len(body):>9}B  {name}")
        if not ok:
            print("  " + preview(body, 1500).replace("\n", "\n  "))
    for name, path in INFO:
        code, body = fetch(base + path, cookie)
        print(f"INFO  {code}  {len(body):>9}B  {name}")
        print("  " + preview(body).replace("\n", "\n  "))
    print(f"\n{fails} required failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
