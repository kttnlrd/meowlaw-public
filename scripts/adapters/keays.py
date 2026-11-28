"""Keays insolvency: ch-tree.json + chapters/*.md -> one document per chapter.

    python scripts/ingest_collection.py data/insolvency-keays --collection insolvency-keays \\
        --adapter keays --access restricted
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TITLE = "Keays Insolvency"
GROUP = "Commentary"


def _body(text: str) -> str:
    """Chapter text without its own front matter and leading "# Chapter N" heading."""
    if text.startswith("---"):
        end = re.search(r"\n---\s*\n", text)
        if end:
            text = text[end.end():]
    return re.sub(r"\A\s*# [^\n]*\n", "", text)


def parts(src: Path) -> list[dict]:
    src = Path(src)
    chapters = json.loads((src / "ch-tree.json").read_text(encoding="utf-8")).get("chapters", [])
    base = (src / "chapters").resolve()
    docs = []
    for c in sorted(chapters, key=lambda c: int(c["chapter"])):
        f = (src / str(c["file"])).resolve()
        if not f.is_relative_to(base) or not f.is_file():
            raise SystemExit(f"chapter {c['chapter']}: {c['file']!r} is not a file under chapters/")
        n = int(c["chapter"])
        docs.append({"id": f"ch-{n}", "title": f"Chapter {n}: {c.get('title') or ''}".rstrip(": "),
                     "text": _body(f.read_text(encoding="utf-8"))})
    return [{"title": "Chapters", "docs": docs}]
