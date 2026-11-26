"""Legislation: one act per <path>/<act>/tree.json.

ponytail: wraps data_loader, which reads config.DATA_DIR; the manifest's
legislation path is that same directory.
"""
from fastapi import HTTPException

from backend.services.data_loader import load_acts_meta, load_tree


def count(path) -> int:
    return len(load_acts_meta())


def iter_docs(path):
    return iter(load_acts_meta())


def get(path, doc_id):
    try:
        return load_tree(doc_id)
    except HTTPException:
        return None
