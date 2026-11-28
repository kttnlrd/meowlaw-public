"""AFSA practice guidance: listed by <path>/afsa_manifest.json."""
import json


def _manifest(path):
    f = path / "afsa_manifest.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []


def count(path) -> int:
    return len(_manifest(path))


def iter_docs(path):
    return iter(_manifest(path))


def get(path, doc_id):
    return next((d for d in _manifest(path) if d.get("slug") == doc_id), None)
