"""Keays insolvency: <path>/ch-tree.json + <path>/chapters/.

Optional type: data is only reachable when data_sources.local.yaml sets
allow_optional_sources: true.
"""
import json
from pathlib import Path


def _chapters(path):
    f = Path(path) / "ch-tree.json"
    return json.loads(f.read_text(encoding="utf-8")).get("chapters", []) if f.is_file() else []


def count(path) -> int:
    return len(_chapters(path))


def iter_docs(path):
    return iter(_chapters(path))


def get(path, doc_id):
    return next((c for c in _chapters(path) if str(c.get("chapter")) == str(doc_id)), None)
