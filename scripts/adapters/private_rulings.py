"""ATO private rulings: a folder of <authorisation_number>.json -> one part per year.

    python scripts/ingest_collection.py <dir>/private_rulings/data/json --collection private-rulings \
        --adapter private_rulings --access signed-in
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TITLE = "ATO private rulings"
ISSUES: list[str] = []  # records skipped because the source holds no content
GROUP = "Rulings"


def _text(r: dict) -> str:
    out = []
    if r.get("subject"):
        out.append(f"**Subject:** {r['subject']}")
    for qa in r.get("qa_pairs") or []:
        out.append(f"**Question:** {qa.get('question', '')}\n\n**Answer:** {qa.get('answer', '')}")
    for key, label in (("applies_for_periods", "Period"), ("scheme_commenced", "Scheme commenced"),
                       ("facts", "Facts"), ("relevant_legislation", "Legislation"),
                       ("relevant_legislative_provisions", "Legislation"),
                       ("reasons_for_decision", "Reasons"), ("case_references", "Cases")):
        v = r.get(key)
        if isinstance(v, list):
            v = "\n".join(f"- {x}" for x in v)
        if v:
            out.append(f"## {label}\n\n{v}")
    if not any(x.startswith(("**Q", "## ")) for x in out) and r.get("formatted_text"):
        out.append(str(r["formatted_text"]))
    return "\n\n".join(out)


def parts(src: Path) -> list[dict]:
    ISSUES.clear()
    years: dict[str, list[dict]] = {}
    for f in sorted(Path(src).glob("*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        num = str(r.get("authorisation_number") or f.stem)
        text = _text(r)
        if not text.strip():
            ISSUES.append(f"{num}: no content in the source record (status {r.get('mop_status') or '?'}), skipped")
            continue
        date = str(r.get("date_of_advice") or "")
        m = re.search(r"\b(?:19|20)\d\d\b", date)
        year = m.group(0) if m else "Undated"
        years.setdefault(year, []).append(
            {"id": f"pr-{num}", "title": f"{num} {r.get('name') or r.get('subject') or ''}".strip(),
             "citation": f"Authorisation {num}", "date": date or None, "text": text})
    return [{"title": y, "docs": d} for y, d in sorted(years.items(), reverse=True)]
