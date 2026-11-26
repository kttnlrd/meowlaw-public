"""ASIC regulatory guides: one guide per <path>/paragraphs/RG_<n>.json.

ponytail: counts the split guides, not rg_manifest.json (absent on dev).
"""
import json


def _files(path):
    return sorted((path / "paragraphs").glob("RG_*.json"))


def count(path) -> int:
    return len(_files(path))


def iter_docs(path):
    return (f.stem for f in _files(path))


def get(path, doc_id):
    f = next((f for f in _files(path) if f.stem == doc_id), None)
    return json.loads(f.read_text(encoding="utf-8")) if f else None
