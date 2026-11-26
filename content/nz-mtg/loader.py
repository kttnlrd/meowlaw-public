"""NZ Master Tax Guide: <path>/section_index.json + <path>/sections/<id>.md.

Optional type: data is only reachable when data_sources.local.yaml sets
allow_optional_sources: true.
"""
import json
from pathlib import Path


def _index(path):
    f = Path(path) / "section_index.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []


def count(path) -> int:
    return len(_index(path))


def iter_docs(path):
    return iter(_index(path))


def get(path, doc_id):
    sid = str(doc_id)
    if not sid or "/" in sid or sid.startswith("."):
        return None
    f = Path(path) / "sections" / f"{sid}.md"
    return f.read_text(encoding="utf-8") if f.is_file() else None
