"""Rulings as text files named <SERIES>_<year>_<n>.txt -> one part per series.

ATO public rulings (data/rulings: TR, TD, CR, PR, AID, GSTR, PS_LA ...) and NZ IRD rulings
(data/nz_rulings: BRPUB, BRPRD, QB, IS, DET ...). The first line of a file is its title; the
citation is rebuilt from the file name.

    python scripts/ingest_collection.py data/rulings --collection public-rulings \
        --adapter rulings --title "ATO public rulings" --access signed-in
    python scripts/ingest_collection.py data/nz_rulings --collection nz-rulings \
        --adapter rulings --title "NZ IRD rulings" --access signed-in
"""
from __future__ import annotations

import re
from pathlib import Path

GROUP = "Rulings"
_NAME = re.compile(r"^(?P<series>[A-Za-z_]+?)_(?P<year>\d{4})_(?P<num>[A-Za-z0-9]+)$")


def parts(src: Path) -> list[dict]:
    by_series: dict[str, list[dict]] = {}
    for f in sorted(Path(src).glob("*.txt")):
        m = _NAME.match(f.stem)
        if not m:
            continue
        series = m["series"].replace("_", " ")
        cit = f"{series} {m['year']}/{m['num']}"
        text = f.read_text(encoding="utf-8", errors="replace")
        first = text.strip().split("\n", 1)[0].strip()
        title = first if first and len(first) < 200 else cit
        if cit.lower() not in title.lower():
            title = f"{cit} {title}"
        by_series.setdefault(series, []).append(
            {"id": f.stem.lower().replace("_", "-"), "title": title, "citation": cit,
             "date": m["year"], "text": text})
    return [{"title": s, "docs": d} for s, d in sorted(by_series.items(), key=lambda kv: -len(kv[1]))]
