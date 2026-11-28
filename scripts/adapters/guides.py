"""Regulator guides: <dir>/texts/*.txt + a manifest -> one document per guide.

ASIC regulatory guides (rg_manifest.json, texts/RG_001.txt) and AFSA practice guidance
(afsa_manifest.json, texts/<slug>.txt).

    python scripts/ingest_collection.py data/regulatory-guides --collection regulatory-guides \
        --adapter guides --title "ASIC regulatory guides" --access signed-in
    python scripts/ingest_collection.py data/afsa --collection afsa-guides \
        --adapter guides --title "AFSA practice guidance" --access signed-in
"""
from __future__ import annotations

import json
from pathlib import Path

GROUP = "Regulator guidance"


def parts(src: Path) -> list[dict]:
    src = Path(src)
    manifest = next(iter(sorted(src.glob("*manifest.json"))), None)
    rows = json.loads(manifest.read_text(encoding="utf-8")) if manifest else []
    docs = []
    for r in rows:
        if "rg_number" in r:  # ASIC
            n = int(r["rg_number"])
            f, did, title, cit = src / "texts" / f"RG_{n:03d}.txt", f"rg-{n}", f"RG {n} {r.get('title', '')}".strip(), f"RG {n}"
        else:  # AFSA
            slug = str(r.get("slug", ""))
            f, did, title, cit = src / "texts" / f"{slug}.txt", slug, str(r.get("title") or slug), None
        if not f.is_file():
            continue
        url = r.get("page_url") or r.get("url")
        text = f.read_text(encoding="utf-8", errors="replace")
        docs.append({"id": did, "title": title, "citation": cit, "status": r.get("status"),
                     "text": (f"Source: {url}\n\n" if url else "") + text})
    return [{"title": "Guides", "docs": docs}]
