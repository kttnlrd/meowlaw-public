"""Tax treaties: data/treaties/<country>/tree.json + articles/*.md -> one part per country.

    python scripts/ingest_collection.py data/treaties --collection treaties --adapter treaties --access signed-in
"""
from __future__ import annotations

import json
from pathlib import Path

TITLE = "Tax treaties"
GROUP = "Treaties"


def parts(src: Path) -> list[dict]:
    out = []
    for tree_path in sorted(Path(src).glob("*/tree.json")):
        country = tree_path.parent
        t = json.loads(tree_path.read_text(encoding="utf-8"))
        base = country.resolve()
        docs = []
        for a in t.get("articles", []):
            f = (country / str(a.get("file", ""))).resolve()
            if not f.is_relative_to(base) or not f.is_file():
                continue
            docs.append({"id": f"{country.name}-{a.get('slug') or a.get('article')}",
                         "title": str(a.get("title") or f"Article {a.get('article')}"),
                         "text": f.read_text(encoding="utf-8", errors="replace")})
        if docs:
            out.append({"title": str(t.get("treaty") or country.name.title()), "docs": docs})
    return out
