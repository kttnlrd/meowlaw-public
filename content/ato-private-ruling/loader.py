"""ATO private rulings: <path>/<authnum>.json, listed by data/private_rulings_index.json."""
import json

from backend.routes.private_rulings import _load_index


def count(path) -> int:
    return len(_load_index())


def iter_docs(path):
    return ({"authnum": k, **v} for k, v in _load_index().items())


def get(path, doc_id):
    if not str(doc_id).isdigit():
        return None
    f = path / f"{doc_id}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
