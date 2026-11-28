"""ATO public rulings via data_loader.load_rulings (AU rows; NZ is content/nz-ruling)."""
from backend.services.data_loader import load_rulings


def _rows():
    return [r for r in load_rulings() if r.get("jurisdiction") != "NZ"]


def count(path) -> int:
    return len(_rows())


def iter_docs(path):
    return iter(_rows())


def get(path, doc_id):
    return next((r for r in _rows() if r["citation"] == doc_id), None)
