"""Case law: the court case lists (*_all_cases.json, aata_tax_cases.json) -> one part per court.

Each case is a document holding its metadata, catchwords and source link.

    python scripts/ingest_collection.py data --collection case-law --adapter cases --access signed-in
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TITLE = "Case law"
GROUP = "Case law"
FILES = ("hca_all_cases.json", "fcafc_all_cases.json", "fca_all_cases.json", "aata_tax_cases.json")
ORDER = ["HCA", "FCAFC", "FCA", "AATA"]


def parts(src: Path) -> list[dict]:
    courts: dict[str, list[dict]] = {}
    seen = set()
    for name in FILES:
        f = Path(src) / name
        if not f.is_file():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("cases", [])
        for c in rows:
            cit = str(c.get("citation") or "").strip()
            if not cit or cit in seen:
                continue
            seen.add(cit)
            court = str(c.get("court") or name.split("_")[0]).upper()
            lines = [f"**{c.get('case_name') or cit}** {cit}"]
            for k, label in (("date", "Date"), ("court", "Court"), ("parallel_citations", "Also reported"),
                             ("catchwords", "Catchwords"), ("subject", "Subject")):
                v = c.get(k)
                if isinstance(v, list):
                    v = "; ".join(map(str, v))
                if v:
                    lines.append(f"{label}: {v}")
            url = c.get("austlii_url") or c.get("court_url")
            if url:
                lines.append(f"Source: {url}")
            courts.setdefault(court, []).append(
                {"id": re.sub(r"[^a-z0-9]+", "-", cit.lower()).strip("-"),
                 "title": f"{c['case_name']} {cit}" if c.get("case_name") else cit, "citation": cit,
                 "date": c.get("date"), "text": "\n\n".join(lines)})
    return [{"title": k, "docs": courts[k]}
            for k in sorted(courts, key=lambda k: ORDER.index(k) if k in ORDER else 99)]
