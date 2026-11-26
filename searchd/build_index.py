#!/usr/bin/env python3
"""Build searchd's index inputs from meowlaw's precomputed embeddings.

Reads the SAME corpus embeddings the production service uses — no re-embedding,
no OpenAI — and emits row-aligned artefacts for the Rust service:

    matrix.f32   rows x 1536 float32   usearch build input, and mmapped at query
                                       time for exact scans of a slicer slice
    ids.i64      rows                  embedding ids, row-aligned
    meta.json    columns t/a/s/h/x/id/series/outcome/year/court
                                       row-aligned metadata (id = embedding id,
                                       which drives the app's cross-reference
                                       lookup, so it must survive the round trip;
                                       series = public-ruling series, "" otherwise;
                                       outcome/year/court = slicer dimensions,
                                       "" wherever the dimension does not apply)

The type/act mapping deliberately mirrors
backend/services/vector_search_service.py, so a hit returned by searchd carries
exactly the `source_type` / `act` values the app's API already exposes — the
Python layer can pass them straight through.

Run from the searchd/ directory:  python3 build_index.py [SRC] [DST] [--meta-only]

--meta-only writes ids.i64 + meta.json only (no 2.4GB matrix.f32), e.g. to a temp
DST to regenerate metadata without touching a live index directory.
"""
import os
import json
import pickle
import re
import sys
from pathlib import Path

import numpy as np

META_ONLY = "--meta-only" in sys.argv
_args = [a for a in sys.argv[1:] if a != "--meta-only"]
SRC = Path(_args[0] if _args else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DST = Path(_args[1]) if len(_args) > 1 else Path(__file__).resolve().parent
DST.mkdir(parents=True, exist_ok=True)

print(f"reading {SRC}")
matrix = np.load(SRC / "embeddings_matrix.npy", mmap_mode="r" if META_ONLY else None)
ids = np.load(SRC / "embeddings_ids.npy").astype(np.int64)
with open(SRC / "embeddings_meta.pkl", "rb") as f:
    meta = pickle.load(f)

if META_ONLY:
    pass
elif matrix.dtype != np.float32:
    matrix = matrix.astype(np.float32)
if matrix.shape[0] != ids.shape[0]:
    sys.exit(f"row mismatch: matrix {matrix.shape} vs ids {ids.shape}")

# cosine == dot product only on unit vectors; verify rather than assume
# (--meta-only never pages the matrix in: only its shape is used)
norms = None if META_ONLY else np.linalg.norm(matrix[: min(20000, len(matrix))], axis=1)
if META_ONLY:
    print("--meta-only: matrix.f32 not written")
elif abs(norms.mean() - 1.0) > 1e-3:
    print("matrix is not unit-normalised — normalising")
    denom = np.linalg.norm(matrix, axis=1, keepdims=True)
    np.divide(matrix, denom, out=matrix, where=denom != 0)
else:
    print(f"matrix already unit-normalised (mean norm {norms.mean():.6f})")

dst_matrix = DST / "matrix.f32"
if META_ONLY:
    pass
elif dst_matrix.exists() and dst_matrix.stat().st_size == matrix.nbytes:
    print("  matrix.f32 already current — skipping rewrite")
else:
    print(f"writing matrix.f32 ({matrix.nbytes / 1e9:.2f} GB)")
    matrix.tofile(dst_matrix)
ids.tofile(DST / "ids.i64")

_SERIES_RE = re.compile(r"[A-Z]+(?: [A-Z]+)*")


def ruling_series(ref: str) -> str:
    """'TR 2025/1' -> 'TR', 'PS LA 2010/1' / 'PSLA_2010_1' -> 'PS LA', 'AID_2001_1' -> 'AID'.

    The leading letter run of the reference, which is what the app's rtype
    filter (vector_search_service.search, rtype_prefixes) prefix-matches on.
    """
    m = _SERIES_RE.match((ref or "").strip().upper())
    if not m:
        return ""
    return "PS LA" if m.group(0) == "PSLA" else m.group(0)


assert [ruling_series(r) for r in ("TR 2025/1", "GSTR 2001/1", "PS LA 2010/1", "PSLA_2010_1",
        "TD 2023/1", "IT 2346", "AID_2001_1", "")] == ["TR", "GSTR", "PS LA", "PS LA", "TD", "IT", "AID", ""]

# 'TR 2010/1' / 'AID_2001_1' / 'TR 98/17' -> '2010' / '2001' / '1998'. Needs the
# '/' or '_' after the year so 'IT 2346' (a number, not a year) stays undated.
_RULING_YEAR_RE = re.compile(r"^[A-Za-z ]+[ _]((?:19|20)?\d\d)[/_]")
# '2010_HCA_57' (also '1996_FCA_1657;_(1997)_149_ALR_102') -> ('2010', 'HCA')
_CASE_RE = re.compile(r"^((?:18|19|20)\d\d)_([A-Z]+)_")


def ruling_year(ref: str) -> str:
    m = _RULING_YEAR_RE.match((ref or "").strip())
    if not m:
        return ""
    y = m.group(1)
    return y if len(y) == 4 else ("19" if y >= "50" else "20") + y


def case_year_court(ref: str) -> tuple:
    m = _CASE_RE.match((ref or "").strip())
    return (m.group(1), m.group(2)) if m else ("", "")


assert [ruling_year(r) for r in ("TR 2010/1", "PS LA 2011/12", "AID_2001_1", "TR 98/17", "IT 2346", "IT 1", "")] == \
    ["2010", "2011", "2001", "1998", "", "", ""]
assert case_year_court("2010_HCA_57") == ("2010", "HCA")
assert case_year_court("1996_FCA_1657;_(1997)_149_ALR_102") == ("1996", "FCA")
assert case_year_court("2024_AATA_N") == ("2024", "AATA")

# private-ruling outcome + date of advice, keyed by authnum (== meta 's' for private rulings)
_outcomes_path = SRC / "private_rulings_outcomes.json"
with open(_outcomes_path) as f:
    OUTCOMES = json.load(f)
print(f"outcomes: {len(OUTCOMES)} authnums from {_outcomes_path}")

cols = {"t": [], "a": [], "s": [], "h": [], "x": [], "id": [], "series": [],
        "outcome": [], "year": [], "court": []}
missing = 0
for eid in ids:
    key = int(eid)
    cols["id"].append(key)
    if key not in meta:
        missing += 1
        cols["t"].append("section"); cols["a"].append(""); cols["s"].append("")
        cols["h"].append(""); cols["x"].append(""); cols["series"].append("")
        cols["outcome"].append(""); cols["year"].append(""); cols["court"].append("")
        continue
    source_type, m_act, m_section, m_title, m_text = meta[key]
    source_type = source_type or "section"
    # identical mapping to vector_search_service.py
    if source_type == "case":
        act = "tax-cases"
    elif source_type == "ruling" and m_act == "private":
        source_type = "private_ruling"
        act = "private-rulings"
    elif source_type == "ruling":
        act = "rulings"
    else:
        act = m_act
    cols["t"].append(source_type)
    cols["a"].append(act or "")
    cols["s"].append(m_section or "")
    cols["h"].append(m_title or "")
    cols["x"].append((m_text or "")[:300])
    # public rulings only: the app's rtype mask leaves private rulings untouched
    cols["series"].append(ruling_series(m_section) if source_type == "ruling" else "")
    # slicer dimensions; "" = does not apply / unknown, and is never counted or matched
    outcome = year = court = ""
    if source_type == "ruling":
        year = ruling_year(m_section)
    elif source_type == "case":
        year, court = case_year_court(m_section)
    elif source_type == "private_ruling":
        o = OUTCOMES.get((m_section or "").strip()) or {}
        outcome = (o.get("outcome") or "").strip().lower()
        y = (o.get("date_of_advice") or "")[:4]
        year = y if y.isdigit() else ""
    cols["outcome"].append(outcome)
    cols["year"].append(year)
    cols["court"].append(court)

with open(DST / "meta.json", "w") as f:
    json.dump(cols, f, separators=(",", ":"))

from collections import Counter
print(f"rows {len(cols['t'])}" + (f"  ({missing} ids missing from meta)" if missing else ""))
print("types:", dict(Counter(cols["t"]).most_common()))
print("top acts:", Counter(cols["a"]).most_common(8))
for c in ("series", "outcome", "court"):
    print(f"{c}:", dict(Counter(v for v in cols[c] if v).most_common()))
print("year: rows with a year", sum(1 for v in cols["year"] if v), "/", len(cols["year"]),
      "| by type without:", dict(Counter(t for t, y in zip(cols["t"], cols["year"]) if not y)))
print(f"-> {DST}")
