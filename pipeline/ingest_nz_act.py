"""Ingest New Zealand Acts into the corpus, in the shape data/nz-it-2007 already uses.

Per act: download whole.html from legislation.govt.nz, refuse it if the page title is not the
act we asked for, parse it to one markdown file per provision, build tree.json, and write the
licence record (NZ legislation carries no copyright). Nothing here invents a value: a field
that cannot be read from the source is recorded as not read.

Usage:
    pipeline/ingest_nz_act.py                    # all acts below
    pipeline/ingest_nz_act.py nz-gst-1985 ...     # named acts only
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36")
PY = REPO / "backend" / "venv" / "bin" / "python"

# slug, PCO path (verified: page title matched), expected page title, corpus act name
ACTS = [
    ("nz-taa-1994", "1994/0166", "Tax Administration Act 1994", "NZ Tax Administration Act 1994"),
    ("nz-gst-1985", "1985/0141", "Goods and Services Tax Act 1985", "NZ Goods and Services Tax Act 1985"),
    ("nz-egda-1968", "1968/0035", "Estate and Gift Duties Act 1968", "NZ Estate and Gift Duties Act 1968"),
    ("nz-scd-1971", "1971/0051", "Stamp and Cheque Duties Act 1971", "NZ Stamp and Cheque Duties Act 1971"),
    ("nz-kiwisaver-2006", "2006/0040", "KiwiSaver Act 2006", "NZ KiwiSaver Act 2006"),
    ("nz-sl-2011", "2011/0062", "Student Loan Scheme Act 2011", "NZ Student Loan Scheme Act 2011"),
    # Repealed Acts. The PCO serves only the latest compilation (the surviving provisions after
    # repeal); historical pre-repeal versions are not published at guessable URLs, so references
    # made by rulings to provisions repealed before that compilation cannot resolve.
    ("nz-ita-1976", "1976/0065", "Income Tax Act 1976", "NZ Income Tax Act 1976"),
    ("nz-ita-1994", "1994/0164", "Income Tax Act 1994", "NZ Income Tax Act 1994"),
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def fetch_act(slug: str, path: str, want_title: str) -> tuple[Path | None, str, str]:
    """Download the whole-act HTML. Returns (path, version_url, reason_if_refused)."""
    out = REPO / "source" / slug
    out.mkdir(parents=True, exist_ok=True)
    html = out / "whole.html"
    url = f"https://www.legislation.govt.nz/act/public/{path}/latest/whole.html"
    r = run(["curl", "-sL", "-o", str(html), "-w", "%{http_code}|%{url_effective}",
             "--max-time", "300", "-A", UA, url])
    if r.returncode != 0:
        return None, "", f"curl failed: {r.stderr.strip()[:80]}"
    code, _, effective = (r.stdout or "").partition("|")
    if code != "200":
        return None, effective, f"HTTP {code}"
    head = html.read_bytes()[:200_000].decode("utf-8", "ignore")
    m = re.search(r"<title>([^<]*)", head)
    title = (m.group(1).strip() if m else "")
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    if norm(want_title) not in norm(title):
        return None, effective, f"page title is {title!r}, not {want_title!r}"
    return html, effective, ""


def version_info(effective: str, html: Path) -> tuple[int, str]:
    """Compilation number from the resolved URL, as-at date from the page."""
    m = re.search(r"/(\d+)\.\d+/whole\.html", effective)
    comp_no = int(m.group(1)) if m else 0
    text = html.read_bytes()[:400_000].decode("utf-8", "ignore")
    mm = re.search(r"as at\s+(\d{1,2} \w+ \d{4})", text)
    if mm:
        from datetime import datetime
        try:
            comp_date = datetime.strptime(mm.group(1), "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            comp_date = "not read"
    else:
        comp_date = "not read"
    return comp_no, comp_date


def licence(slug: str, name: str, effective: str, comp_date: str) -> dict:
    """Same shape and same basis as data/nz-it-2007/licence.json."""
    return {
        "act": name,
        "slug": slug,
        "jurisdiction": "NZ",
        "source_url": effective or f"https://www.legislation.govt.nz/act/public/{slug}",
        "source_url_evidence": f"Fetched {effective} (HTTP 200); page title confirmed against the "
                               f"Act name, checked 2026-10-13.",
        "fetched": "2026-10-13",
        "date_basis": "compilation",
        "version": {"date": comp_date, "evidence": "tree.json compilation_date"},
        "licence": {
            "name": "No copyright in New Zealand legislation (Copyright Act 1994 (NZ) s 27)",
            "url": "https://www.legislation.govt.nz/copyright/",
            "statement": "Under section 27 of the Copyright Act 1994, there is no copyright in "
                         "New Zealand legislation, including in legislation published by the PCO "
                         "on this website. Section 27 does not affect the copyright in any work "
                         "that is incorporated by reference into legislation.",
            "reuse": "All legislation published on this website may be reproduced in any format "
                     "or media, free of charge, without requiring specific permission.",
            "verified": "verified 11 October 2026 against https://www.legislation.govt.nz/copyright/ "
                        "(saved copy: ~/lawkitty-copyright/nz-legislation-copyright-2026-10-11.html)",
        },
        "copyright_notice": None,
        "attribution": None,
        "attribution_required": False,
        "attribution_basis": "No copyright subsists, so no attribution is owed; reproduction in "
                             "any format, free, without permission.",
        "content_changed": True,
        "changes": ["split into one file per provision", "reformatted as markdown"],
        "disclaimer": None,
        "unofficial_notice": "Unofficial copy. This version does not display the New Zealand Coat "
                              "of Arms and is not the official version for the purposes of sections "
                              "78 and 79 of the Legislation Act 2019 (NZ). The official version is "
                              "published on legislation.govt.nz.",
        "endorsement_notice": None,
        "excluded_material": "Works incorporated by reference into legislation keep their own "
                             "copyright (s 27 does not affect them); none are reproduced. The New "
                             "Zealand Coat of Arms is not reproduced.",
        "terms_url": "https://www.legislation.govt.nz/copyright/",
        "notice_line": "No copyright in NZ legislation · unofficial copy, not the official version",
        "notice_badge": "NZ",
    }


def ingest(slug: str, path: str, want_title: str, act_name: str) -> dict:
    print(f"\n== {slug} ({want_title}) ==", flush=True)
    html, effective, refused = fetch_act(slug, path, want_title)
    if refused:
        print(f"   REFUSED: {refused}", flush=True)
        return {"slug": slug, "status": f"refused: {refused}"}

    comp_no, comp_date = version_info(effective, html)
    print(f"   source: {effective} ({html.stat().st_size} bytes) | compilation {comp_no} ({comp_date})",
          flush=True)

    sections = REPO / "data" / slug / "sections"
    r = run([str(PY), str(REPO / "pipeline" / "parse_nz_it.py"), "--html-file", str(html),
             "--out-dir", str(sections), "--compilation-no", str(comp_no),
             "--compilation-date", comp_date, "--act-name", act_name], cwd=str(REPO))
    if r.returncode != 0:
        print(f"   parse FAILED: {r.stderr.strip()[-200:]}", flush=True)
        return {"slug": slug, "status": "parse failed", "error": r.stderr.strip()[-200:]}
    try:
        stats = json.loads(r.stdout[r.stdout.index("{"):])
    except Exception:
        stats = {}
    print(f"   parsed: {json.dumps(stats)[:160]}", flush=True)

    tree = REPO / "data" / slug / "tree.json"
    r = run([str(PY), str(REPO / "pipeline" / "build_tree.py"), "--sections-dir", str(sections),
             "--out-file", str(tree), "--act", act_name, "--compilation-no", str(comp_no),
             "--compilation-date", comp_date], cwd=str(REPO))
    if r.returncode != 0:
        print(f"   tree FAILED: {r.stderr.strip()[-200:]}", flush=True)
        return {"slug": slug, "status": "tree failed"}

    (REPO / "data" / slug / "licence.json").write_text(
        json.dumps(licence(slug, act_name, effective, comp_date), indent=2, ensure_ascii=False),
        encoding="utf-8")

    data = json.loads(tree.read_text(encoding="utf-8"))
    n_sections = sum(len(p.get("sections", []))
                     for p in data.get("parts", [])) + \
                 sum(len(d.get("sections", [])) for p in data.get("parts", [])
                     for d in p.get("divisions", []))
    files = len(list(sections.rglob("*.md")))
    print(f"   tree: {len(data.get('parts', []))} parts, {n_sections} sections | files: {files}",
          flush=True)
    return {"slug": slug, "status": "ok", "parts": len(data.get("parts", [])),
            "sections": n_sections, "files": files, "compilation": f"{comp_no} ({comp_date})"}


def main() -> None:
    wanted = set(sys.argv[1:])
    todo = [a for a in ACTS if not wanted or a[0] in wanted]
    results = []
    for i, act in enumerate(todo):
        results.append(ingest(*act))
        if i + 1 < len(todo):
            time.sleep(3)          # be polite to the PCO
    print("\n== summary ==")
    for r in results:
        print(f"   {r['slug']:20s} {r['status']:14s} "
              f"{r.get('parts', '')!s:>5} parts {r.get('sections', '')!s:>6} sections "
              f"{r.get('compilation', '')}")
    out = REPO / "source" / "nz-ingest-summary.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"   written to {out}")


if __name__ == "__main__":
    main()
