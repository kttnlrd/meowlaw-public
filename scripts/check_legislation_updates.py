#!/usr/bin/env python3
"""Check for legislation changes on legislation.gov.au.

FIXED 2026-08-22: the old OData path (api.prod.legislation.gov.au/api/v1/odata)
returns an empty body and the Details-page scrape regex did not match the SPA's
embedded JSON, so every act reported "no changes" while compilations drifted.
Now uses the FRL series page at https://www.legislation.gov.au/{series_id}/latest
and parses the embedded "compilationNumber"/"registerId" JSON. OData v1 endpoint
is tried first, series page is the reliable path.

For each tracked act, compares the remote compilation number against local
tree.json and reports changes.

Outputs JSON to stdout.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from curl_cffi import requests as curl

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("check_legislation")

ODATA_V1 = "https://api.prod.legislation.gov.au/v1"

# Act slugs → FRL series ID (authoritative, from v1/Titles) + known FRBR URI
TRACKED_ACTS = {
    "itaa-1997": {
        "name": "Income Tax Assessment Act 1997",
        "series_id": "C2004A05138",
        "frbr_uri": "/au/leg/cth/consol_act/itaa1997332",
    },
    "itaa-1936": {
        "name": "Income Tax Assessment Act 1936",
        "series_id": "C1936A00027",
        "frbr_uri": "/au/leg/cth/consol_act/itaa1936322",
    },
    "taa-1953": {
        "name": "Taxation Administration Act 1953",
        "series_id": "C1953A00001",
        "frbr_uri": "/au/leg/cth/consol_act/taa1953236",
    },
    "gst-1999": {
        "name": "A New Tax System (Goods and Services Tax) Act 1999",
        "series_id": "C2004A00446",
        "frbr_uri": "/au/leg/cth/consol_act/antstgsata1999486",
    },
    "fbt-1986": {
        "name": "Fringe Benefits Tax Assessment Act 1986",
        "series_id": "C2004A03280",
        "frbr_uri": "/au/leg/cth/consol_act/fbtaa1986362",
    },
}

DATA_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


def load_local_compilation(act_slug: str) -> dict:
    """Return the compilation_no and compilation_date from local tree.json."""
    tree_path = DATA_DIR / act_slug / "tree.json"
    if not tree_path.exists():
        return {}
    try:
        with open(tree_path) as f:
            tree = json.load(f)
        return {
            "compilation_no": tree.get("compilation_no"),
            "compilation_date": tree.get("compilation_date"),
        }
    except Exception as e:
        log.warning("Failed to read %s: %s", tree_path, e)
        return {}


def _parse_remote_comp(html_or_json: str) -> dict | None:
    """Extract {compilation_no, compilation_date, register_id} from FRL page HTML or OData JSON."""
    # FRL Details/latest pages embed JSON like "compilationNumber":"266"
    m = re.search(r'"compilationNumber"\s*:\s*"?(\d+)"?', html_or_json)
    if not m:
        return None
    comp_no = m.group(1)
    reg = re.search(r'"registerId"\s*:\s*"(C20\d{2}C\d{5})"', html_or_json)
    date_m = re.search(r'"start"\s*:\s*"([^"]{10})', html_or_json)
    if not date_m:
        date_m = re.search(r'"effectiveDate"\s*:\s*"([^"]{10})"', html_or_json)
    # Also try the visible date-effective-start span
    if not date_m:
        date_m = re.search(r'date-effective-start">\s*([0-9]{1,2} [A-Z][a-z]+ \d{4})', html_or_json)
    comp_date = None
    if date_m:
        raw = date_m.group(1)
        if "-" in raw:  # ISO from JSON
            comp_date = raw[:10]
        else:
            try:
                comp_date = datetime.strptime(raw, "%d %B %Y").date().isoformat()
            except ValueError:
                comp_date = None
    return {
        "compilation_no": comp_no,
        "compilation_date": comp_date,
        "register_id": reg.group(1) if reg else None,
    }


def check_act_via_odata(act_slug: str, config: dict) -> dict:
    """Check compilation status via legislation.gov.au v1 OData API (best effort)."""
    frbr = config["frbr_uri"]
    result = {
        "source": f"legislation_{act_slug}",
        "act_name": config["name"],
        "has_changes": False,
        "local": load_local_compilation(act_slug),
        "remote": None,
        "amending_acts": [],
        "affected_sections": [],
        "error": None,
    }
    try:
        query_url = (
            f"{ODATA_V1}/Compilations"
            f"?$filter=FRBRUri eq '{frbr}'"
            f"&$orderby=CompilationStartDate desc"
            f"&$top=1"
            f"&$expand=Amendments($expand=AmendingAct)"
        )
        resp = curl.get(query_url, impersonate="chrome120", headers=HEADERS, timeout=30)
        if resp.status_code != 200 or not resp.text.strip():
            result["error"] = f"OData HTTP {resp.status_code} or empty body"
            return result
        data = resp.json()
        compilations = data.get("value", data.get("d", {}).get("results", []))
        if not compilations:
            result["error"] = "No compilations found in OData response"
            return result
        latest = compilations[0]
        remote_comp = latest.get("CompilationNumber") or latest.get("Number") or ""
        remote_date = latest.get("CompilationStartDate") or latest.get("Date") or ""
        if isinstance(remote_date, str):
            remote_date = remote_date[:10]
        result["remote"] = {"compilation_no": str(remote_comp), "compilation_date": remote_date}
        amendments = latest.get("Amendments") or []
        if isinstance(amendments, dict):
            amendments = amendments.get("results", [])
        for am in amendments:
            amending = am.get("AmendingAct", {})
            if isinstance(amending, dict) and amending:
                result["amending_acts"].append({
                    "name": amending.get("Title") or amending.get("Name") or "Unknown",
                    "frbr_uri": amending.get("FRBRUri") or amending.get("Id") or "",
                })
        local = result["local"]
        if str(local.get("compilation_no")) != str(remote_comp):
            result["has_changes"] = True
            log.info("%s: compilation changed local=%s remote=%s", act_slug, local.get("compilation_no"), remote_comp)
    except Exception as e:
        result["error"] = str(e)
        log.error("Error checking %s via OData: %s", act_slug, e)
    return result


def check_act_via_series_page(act_slug: str, config: dict) -> dict:
    """Primary path: fetch the FRL series /latest page and parse embedded JSON."""
    result = {
        "source": f"legislation_{act_slug}",
        "act_name": config["name"],
        "has_changes": False,
        "local": load_local_compilation(act_slug),
        "remote": None,
        "amending_acts": [],
        "affected_sections": [],
        "error": None,
    }
    series_id = config["series_id"]
    try:
        # /latest is the canonical "current compilation" URL (redirects to the
        # most recent compilation's Details page)
        url = f"https://www.legislation.gov.au/{series_id}/latest"
        resp = curl.get(url, impersonate="chrome120", headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            result["error"] = f"Series page HTTP {resp.status_code}"
            return result
        remote = _parse_remote_comp(resp.text)
        if remote is None:
            # Try the Details page (sometimes /latest serves a thin shell)
            url2 = f"https://www.legislation.gov.au/Details/{series_id}"
            resp2 = curl.get(url2, impersonate="chrome120", headers=HEADERS, timeout=30)
            if resp2.status_code == 200:
                remote = _parse_remote_comp(resp2.text)
        if remote is None:
            result["error"] = "No compilationNumber found on FRL page"
            return result
        result["remote"] = remote
        local = result["local"]
        if str(local.get("compilation_no")) != str(remote["compilation_no"]):
            result["has_changes"] = True
            log.info(
                "%s: compilation changed local=%s remote=%s (register %s)",
                act_slug, local.get("compilation_no"), remote["compilation_no"], remote.get("register_id"),
            )
    except Exception as e:
        result["error"] = str(e)
        log.error("Error checking %s via series page: %s", act_slug, e)
    return result


def main():
    start = time.time()
    results = []
    errors = []

    for slug, config in TRACKED_ACTS.items():
        r = check_act_via_series_page(slug, config)
        if r.get("error"):
            # Best-effort OData fallback
            log.warning("Series page failed for %s (%s), trying OData v1", slug, r["error"])
            r2 = check_act_via_odata(slug, config)
            if not r2.get("error") or r2.get("remote"):
                r = r2
        results.append(r)
        if r.get("error"):
            errors.append({"source": slug, "error": r["error"]})

    total_changed = sum(1 for r in results if r.get("has_changes"))
    output = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_seconds": round(time.time() - start, 2),
        "acts_checked": len(results),
        "acts_changed": total_changed,
        "results": results,
        "errors": errors,
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
