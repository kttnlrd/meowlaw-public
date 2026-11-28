"""Cases: <path>/*.json via data_loader.load_cases (config.CASE_DIR == this source)."""
from backend.services.data_loader import load_cases


def count(path) -> int:
    return len(load_cases())


def iter_docs(path):
    return iter(load_cases())


def get(path, doc_id):
    return next((c for c in load_cases() if c["citation"] == doc_id), None)
