"""Case law with the full judgment text: case metadata (adapters/cases.py) + extracted judgments.

The judgments come from scripts/austlii_extract.py: a folder of <year>_<COURT>_<n>.txt plus
index.jsonl. Each case keeps its metadata block (citation, court, catchwords, source) and gains
the reasons for judgment below it; judgments with no metadata entry are added from their own
header. Ids are the same as the metadata-only collection ("2015-hca-48"), so links keep working.

    JUDGMENTS_DIR=/path/to/judgments python scripts/ingest_collection.py data \\
        --collection case-law --adapter judgments --access signed-in

SOURCE is the data folder holding the case lists; without JUDGMENTS_DIR, <SOURCE>/judgments.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from adapters import cases

TITLE = "Case law"
GROUP = "Case law"
MAX_BYTES = 8 * 1024 * 1024
ORDER = ["HCA", "FCAFC", "FCA", "AATA", "ARTA"]


def _slug(citation: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", citation.lower()).strip("-")


def _body(path: Path) -> str:
    """The judgment below the extractor's header block (title, citation, court, date, source)."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
        return ""
    t = path.read_text(encoding="utf-8", errors="replace")
    i = t.find("\n---\n")
    return t[i + 5:].strip() if i >= 0 else t.strip()


def parts(src: Path) -> list[dict]:
    jdir = Path(os.environ.get("JUDGMENTS_DIR") or Path(src) / "judgments")
    index = {}
    idx = jdir / "index.jsonl"
    if idx.is_file():
        for line in idx.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("status") in ("ok", "short") and r.get("citation"):
                    index[_slug(r["citation"])] = r
    out = cases.parts(src)
    seen = set()
    for p in out:
        for d in p["docs"]:
            seen.add(d["id"])
            r = index.get(d["id"])
            if r:
                body = _body(jdir / f"{r['stem']}.txt")
                if body:
                    d["text"] = d["text"] + "\n\n---\n\n## Judgment\n\n" + body
    courts = {p["title"]: p for p in out}
    for sid, r in sorted(index.items()):
        if sid in seen:
            continue
        body = _body(jdir / f"{r['stem']}.txt")
        if not body:
            continue
        court = r.get("court") or "Other"
        title = re.sub(r"\s*\(\d{1,2} [A-Za-z]+ \d{4}\)\s*$", "", r.get("title") or r["citation"]).strip()
        head = f"**{title}**\n\nCourt: {court}" + (f"\n\nDate: {r['date']}" if r.get("date") else "")
        courts.setdefault(court, {"title": court, "docs": []})["docs"].append(
            {"id": sid, "title": title, "citation": r["citation"], "date": r.get("date"),
             "text": head + "\n\n---\n\n## Judgment\n\n" + body})
    # Each court splits by year, newest first, so a court opens to a short list of years.
    for p in courts.values():
        years: dict[str, list] = {}
        for d in p.pop("docs"):
            years.setdefault(_year(d), []).append(d)
        p["groups"] = [{"title": y, "docs": sorted(years[y], key=_num)}
                       for y in sorted(years, key=lambda y: (y == "Undated", y), reverse=True)]
        p["groups"].sort(key=lambda g: g["title"] == "Undated")
    return [courts[k] for k in sorted(courts, key=lambda k: ORDER.index(k) if k in ORDER else 99)]


def _year(d: dict) -> str:
    m = re.match(r"(\d{4})-", d.get("id") or "") or re.search(r"\[(\d{4})\]", d.get("citation") or "")
    return m.group(1) if m else ((d.get("date") or "")[:4] or "Undated")


def _num(d: dict) -> tuple:
    m = re.search(r"-(\d+)$", d.get("id") or "")
    return (int(m.group(1)) if m else 10**9, d.get("title") or "")
