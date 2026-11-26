"""Prove the compact row store reproduces the pickled metadata exactly.

For every row, the dict vector_search_service builds from the compact store must
equal (values, types and key order) what search() built from the pickled tuples.

    /usr/bin/python3.12 scripts/check_vector_compact.py
"""
import os
import pickle
import sys

sys.path.insert(0, os.getcwd())
from backend.services import vector_search_service as v  # noqa: E402

v.load()
with open(v.META_FILE, "rb") as f:
    meta = pickle.load(f)
bad = 0
for i, eid in enumerate(v._ids):
    source_type, m_act, m_section, m_title, m_text = meta[int(eid)]
    source_type, act = v._map_type(source_type, m_act)
    want = {"embedding_id": int(eid), "source_type": source_type, "act": act, "section": m_section,
            "title": m_title, "score": 0.5, "snippet": (m_text or "")[:300]}
    got = v._row_result(i, 0.5)
    if list(got.items()) != list(want.items()) or any(type(got[k]) is not type(want[k]) for k in want):
        bad += 1
        if bad <= 5:
            print("MISMATCH row", i, got, want)
print(f"{len(v._ids) - bad}/{len(v._ids)} rows identical")
sys.exit(1 if bad else 0)
