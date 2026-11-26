#!/usr/bin/env python3.12
"""Which regulatory guides are served from our disk, and which quietly redirect off-site.

The download route falls back to RedirectResponse(pdf_url) when the local file is missing, so a
missing copy is invisible: the user gets a 200 with someone else's file. This reports the gap and,
with --fetch, pulls the missing PDFs local so the route serves our own copy again.

Usage
  rg_pdf_gap.py --report
  rg_pdf_gap.py --fetch [--limit N] [--root /path/to/app]
"""

from __future__ import annotations
import os

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(os.environ["LEGISLATION_EXPLORER_DIR"])


def load_index(root: pathlib.Path) -> list[dict]:
    # rg_manifest.json is the authority: it carries rg_number, has_pdf and pdf_url per guide.
    manifest = root / "data" / "regulatory-guides" / "rg_manifest.json"
    if manifest.exists():
        data = json.loads(manifest.read_text())
        if isinstance(data, list):
            return [g for g in data if isinstance(g, dict)]
    for name in ("regulatory-guides/index.json", "regulatory_guides.json",
                 "regulatory-guides/summaries/index.json"):
        p = root / "data" / name
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                data = data.get("guides") or list(data.values())
            return [g for g in data if isinstance(g, dict)]
    # fall back to the summaries themselves
    out = []
    for p in sorted((root / "data" / "regulatory-guides" / "summaries").glob("*.json")):
        try:
            d = json.loads(p.read_text())
            if isinstance(d, dict):
                out.append(d)
        except Exception:
            continue
    return out


def rg_number(g: dict) -> int | None:
    for key in ("rg_number", "number", "rg"):
        v = g.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            digits = "".join(ch for ch in v if ch.isdigit())
            if digits:
                return int(digits)
    return None


def _pdf_from_page(page_url: str | None) -> str | None:
    """Current PDF link on an ASIC guide page, or None."""
    import re
    if not page_url:
        return None
    try:
        req = urllib.request.Request(page_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", "ignore")
        links = sorted(set(re.findall(r'href="(https://download\.asic\.gov\.au/[^"]+\.pdf[^"]*)"', html)))
        return links[0] if links else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--report", action="store_true", help="report only (default when no --fetch)")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    pdf_dir = root / "data" / "regulatory-guides" / "pdfs"
    guides = load_index(root)
    if not guides:
        print("  no regulatory guide index found")
        return 1

    have, missing_with_url, missing_no_url = [], [], []
    for g in guides:
        n = rg_number(g)
        if n is None:
            continue
        path = pdf_dir / f"RG_{n:03d}.pdf"
        url = g.get("pdf_url") or ((g.get("documents") or {}).get("pdf") if isinstance(
            g.get("documents"), dict) else None)
        if path.exists() and path.stat().st_size > 10_000:
            have.append((n, path.stat().st_size))
        elif url:
            missing_with_url.append((n, url))
        else:
            missing_no_url.append(n)

    print(f"  guides indexed: {len(guides)}")
    print(f"  served from disk: {len(have)}")
    print(f"  MISSING, would redirect off-site: {len(missing_with_url)}")
    print(f"  missing, no url at all (404): {len(missing_no_url)} {missing_no_url[:10]}")
    if missing_with_url:
        print("  first few redirecting:", [n for n, _u in missing_with_url[:12]])
    if not args.fetch:
        return 0

    fetched, failed = [], []
    for n, url in missing_with_url[:args.limit]:
        dest = pdf_dir / f"RG_{n:03d}.pdf"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lawkitty-rg-sync/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read()
            if not body.startswith(b"%PDF"):
                failed.append((n, f"not a PDF ({body[:8]!r})"))
                continue
            dest.write_bytes(body)
            fetched.append((n, len(body)))
            print(f"   fetched RG {n}: {len(body)} bytes")
        except urllib.error.HTTPError as e:
            # ASIC republishes guides on new media paths, so a manifest pdf_url goes stale and
            # 404s. Read the guide's own page and take the current link rather than giving up.
            if e.code == 404:
                page = next((g.get("page_url") for g in guides
                             if rg_number(g) == n and g.get("page_url")), None)
                fresh = _pdf_from_page(page)
                if fresh:
                    try:
                        req2 = urllib.request.Request(fresh, headers={"User-Agent": "lawkitty-rg-sync/1.0"})
                        with urllib.request.urlopen(req2, timeout=120) as r2:
                            body2 = r2.read()
                        if body2.startswith(b"%PDF"):
                            dest.write_bytes(body2)
                            fetched.append((n, len(body2)))
                            print(f"   fetched RG {n} via page_url: {len(body2)} bytes (manifest url was stale)")
                            continue
                    except Exception as e2:
                        failed.append((n, f"page_url pdf failed: {str(e2)[:60]}"))
                        continue
            failed.append((n, f"HTTP {e.code}"))
        except Exception as e:
            failed.append((n, str(e)[:80]))
    print(f"\n  fetched {len(fetched)}, failed {len(failed)}")
    if failed:
        print("  failures:", failed[:10])
    json.dump({"fetched": fetched, "failed": failed},
              open(pathlib.Path("/tmp/rg_fetch_result.json"), "w"))
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
