"""CCH commentary: <path>/<publication>/section_index.json per publication.

Optional type: data is only reachable when data_sources.local.yaml sets
allow_optional_sources: true. <path> is the dir holding the publication dirs.
"""
import json
from pathlib import Path

PUBLICATIONS = ("master-tax-guide", "master-gst-guide", "master-tax-examples", "oecd-mtc-commentary")


def _index(path, pub):
    f = Path(path) / pub / "section_index.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []


def count(path) -> int:
    return sum(len(_index(path, p)) for p in PUBLICATIONS)


def iter_docs(path):
    for p in PUBLICATIONS:
        for d in _index(path, p):
            yield {**d, "publication": p}


def get(path, doc_id):
    """doc_id = '<publication>/<section-id>' -> markdown text or None."""
    pub, _, sid = str(doc_id).partition("/")
    if pub not in PUBLICATIONS or not sid or "/" in sid or sid.startswith("."):
        return None
    f = Path(path) / pub / "sections" / f"{sid}.md"
    return f.read_text(encoding="utf-8") if f.is_file() else None
