"""Tax treaties: one country per <path>/<country>/tree.json."""
import json


def _countries(path):
    return sorted(d.name for d in path.iterdir() if (d / "tree.json").is_file())


def count(path) -> int:
    return len(_countries(path))


def iter_docs(path):
    return iter(_countries(path))


def get(path, doc_id):
    if doc_id not in _countries(path):
        return None
    return json.loads((path / doc_id / "tree.json").read_text(encoding="utf-8"))
