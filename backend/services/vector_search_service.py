"""Vector search over data/embeddings.db using OpenAI text-embedding-3-small."""
from __future__ import annotations

import logging
import os
import pickle
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from openai import OpenAI

from backend.config import BASE
from content.registry import content_type_for

logger = logging.getLogger(__name__)

EMBEDDINGS_DB = BASE / "data" / "embeddings.db"
MATRIX_FILE = BASE / "data" / "embeddings_matrix.npy"
IDS_FILE = BASE / "data" / "embeddings_ids.npy"
META_FILE = BASE / "data" / "embeddings_meta.pkl"
# Compact per-row store derived from META_FILE by build_compact().
ROWS_FILE = BASE / "data" / "embeddings_rows.npz"
TEXT_FILE = BASE / "data" / "embeddings_text.bin"
BUILD_SCRIPT = BASE / "scripts" / "build_vector_matrix.py"
MODEL = "text-embedding-3-small"
# The embedding endpoint is pluggable. The model and DIMS must stay exactly these when a
# gateway is used: stored vectors were produced by text-embedding-3-small at 1536 dims, so a
# different model would make every query incomparable with the corpus. A gateway only moves
# the request, it does not change the arithmetic.
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "").strip()
EMBED_MODEL = os.getenv("EMBED_MODEL", "").strip() or MODEL
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "").strip()
DIMS = 1536

_ids: np.ndarray | None = None
_matrix: np.ndarray | None = None
_loaded_signature: tuple[float, int] | None = None
# Per-row category codes (aligned with _ids/_matrix) into small fixed-width
# value arrays, so act/type/series filters can mask out non-matching rows before
# top-K selection instead of after it. Titles and snippets stay on disk in
# TEXT_FILE (mmapped) and only the rows actually returned are paged in.
_rows: dict[str, np.ndarray] | None = None
_text: np.ndarray | None = None
_none: dict[str, set[int]] = {}

# Load API key from .hermes/.env
_env_path = Path.home() / ".hermes" / ".env"
_api_key = os.environ.get("OPENAI_API_KEY")
if not _api_key and _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        if _line.startswith("OPENAI_API_KEY="):
            _api_key = _line.strip().split("=", 1)[1]
            break

# EMBED_API_KEY wins, then OPENAI_API_KEY. With EMBED_BASE_URL set the request goes to the
# gateway (OpenRouter), which proxies the same OpenAI model.
_client = (OpenAI(api_key=EMBED_API_KEY or _api_key, base_url=EMBED_BASE_URL)
           if EMBED_BASE_URL else OpenAI(api_key=EMBED_API_KEY or _api_key))


def _map_type(source_type: str | None, m_act: str | None) -> tuple[str, str | None]:
    """The (source_type, act) the API exposes for a stored row."""
    source_type = source_type or "section"
    if source_type == "case":
        return source_type, "tax-cases"
    if source_type == "ruling" and m_act == "private":
        return "private_ruling", "private-rulings"
    if source_type == "ruling":
        return source_type, "rulings"
    return source_type, m_act


def _codes(values: list[str]) -> tuple[np.ndarray, np.ndarray]:
    vals, codes = np.unique(np.array(values), return_inverse=True)
    dt = np.uint8 if len(vals) <= 0xFF else np.uint16 if len(vals) <= 0xFFFF else np.uint32
    return vals, codes.astype(dt)


def build_compact() -> None:
    """Write ROWS_FILE + TEXT_FILE from the pickled metadata.

    Run in a subprocess by load(): unpickling ~385K tuples in the app process
    leaves ~300MB of anonymous heap behind even after it is dropped. Titles and
    snippets go into one UTF-8 blob (title_i, snippet_i interleaved) with an
    offsets array; search() decodes exactly the bytes that were encoded, so its
    output is identical to reading the tuples (scripts/check_vector_compact.py
    proves it row by row).
    """
    ids = np.load(IDS_FILE)
    with open(META_FILE, "rb") as f:
        meta = pickle.load(f)
    n = ids.shape[0]
    types, acts, secs = [], [], []
    none: dict[str, list[int]] = {"act": [], "section": [], "title": []}
    off = np.empty(2 * n + 1, dtype=np.int64)
    off[0] = pos = 0
    tmp_text = TEXT_FILE.with_suffix(".tmp")
    with open(tmp_text, "wb") as out:
        for i, eid in enumerate(ids):
            source_type, m_act, m_section, m_title, m_text = meta[int(eid)]
            source_type, act = _map_type(source_type, m_act)
            for k, v in (("act", act), ("section", m_section), ("title", m_title)):
                if v is None:
                    none[k].append(i)
            types.append(source_type)
            acts.append(act or "")
            secs.append(m_section or "")
            for j, t in enumerate((m_title or "", (m_text or "")[:300])):
                b = t.encode("utf-8", "surrogatepass")
                out.write(b)
                pos += len(b)
                off[2 * i + j + 1] = pos
    del meta
    arrays = {"off": off}
    for k, vals in (("type", types), ("act", acts), ("section", secs)):
        arrays[f"{k}_vals"], arrays[f"{k}_codes"] = _codes(vals)
    for k, rows in none.items():
        arrays[f"none_{k}"] = np.array(rows, dtype=np.int64)
    tmp_rows = ROWS_FILE.with_suffix(".tmp")
    with open(tmp_rows, "wb") as f:
        np.savez(f, **arrays)
    os.replace(tmp_text, TEXT_FILE)
    os.replace(tmp_rows, ROWS_FILE)  # written last: its mtime marks the pair fresh


def _row_text(i: int, j: int) -> str:
    """Field j (0 = title, 1 = snippet) of row i, read from the mmapped blob."""
    off = _rows["off"]
    return bytes(_text[off[2 * i + j]:off[2 * i + j + 1]]).decode("utf-8", "surrogatepass")


def load() -> None:
    """Load the vector matrix (memory-mapped) + compact metadata.

    The matrix snapshot is built by scripts/build_vector_matrix.py; loading
    via mmap keeps RSS flat regardless of corpus size (274K+ embeddings would
    otherwise need ~1.7GB just for the matrix and OOM the 1.5GB cgroup).
    If the DB has grown past the snapshot, rebuild it first (self-heal).
    """
    global _ids, _matrix, _loaded_signature, _rows, _text, _none

    def _build() -> None:
        logger.info("Vector matrix snapshot stale — rebuilding via %s", BUILD_SCRIPT.name)
        t0 = time.time()
        subprocess.run(
            [sys.executable, str(BUILD_SCRIPT)],
            check=True, capture_output=True, timeout=1800,
        )
        logger.info("Matrix rebuild took %.0fs", time.time() - t0)

    conn = sqlite3.connect(str(EMBEDDINGS_DB))
    try:
        db_rows = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    except sqlite3.OperationalError:
        logger.warning(
            "embeddings.db has no 'embeddings' table (empty/uninitialised) — "
            "vector search disabled until %s is run", BUILD_SCRIPT.name,
        )
        return
    finally:
        conn.close()

    if not MATRIX_FILE.exists():
        _build()
    else:
        try:
            snapshot_rows = int(np.load(IDS_FILE).shape[0])
        except Exception:
            snapshot_rows = -1
        if snapshot_rows != db_rows:
            logger.info("Matrix snapshot %s rows vs DB %s — rebuilding", snapshot_rows, db_rows)
            _build()

    _ids = np.load(IDS_FILE)
    _matrix = np.load(MATRIX_FILE, mmap_mode="r")

    src_mtime = max(META_FILE.stat().st_mtime, IDS_FILE.stat().st_mtime)
    if not ROWS_FILE.exists() or ROWS_FILE.stat().st_mtime < src_mtime:
        logger.info("Building compact row store from %s", META_FILE.name)
        subprocess.run(
            [sys.executable, "-c",
             "from backend.services.vector_search_service import build_compact; build_compact()"],
            cwd=str(BASE), check=True, capture_output=True, timeout=1800,
        )
    with np.load(ROWS_FILE) as z:
        rows = {k: z[k] for k in z.files}
    if rows["off"].shape[0] != 2 * _ids.shape[0] + 1:
        raise RuntimeError(f"{ROWS_FILE.name} does not match {IDS_FILE.name}; delete it to rebuild")
    _rows = rows
    _text = np.memmap(TEXT_FILE, dtype=np.uint8, mode="r")
    _none = {k: set(rows[f"none_{k}"].tolist()) for k in ("act", "section", "title")}

    _loaded_signature = _current_signature()
    logger.info("Vector search loaded: %d embeddings (1536-dim, mmap)", _ids.shape[0])


def _current_signature() -> tuple[float, int]:
    """Identify the state of the matrix snapshot + the DB it must agree with.

    (matrix file mtime, embeddings row count) - cheap enough to check per request, and it changes
    whenever the snapshot is rebuilt, which is the only thing that can make an in-memory copy
    wrong.
    """
    try:
        mtime = MATRIX_FILE.stat().st_mtime
    except OSError:
        mtime = -1.0
    try:
        conn = sqlite3.connect(f"file:{EMBEDDINGS_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        rows = -1
    return (mtime, rows)


def _ensure_loaded() -> None:
    """(Re)load when the snapshot on disk no longer matches what this process holds.

    Loading once and never re-checking meant a long-running worker kept serving whatever matrix it
    read at startup: a full re-embed plus a restored set of 257,260 ruling and case rows stayed
    invisible to the API until the process was restarted, while every read of the file looked
    correct.
    """
    global _loaded_signature
    sig = _current_signature()
    if _ids is None or sig != _loaded_signature:
        load()
        _loaded_signature = sig


EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "8"))


def embed_query(query: str) -> np.ndarray:
    """Embed a single query via OpenAI API.

    Bounded on purpose. The SDK default is a 600s timeout with 2 retries, so a
    stalled upstream would otherwise hang a user-facing search with no error and
    no log line -- the shape of the rare multi-second spike seen in latency
    sampling. One retry, then raise.
    """
    last: Exception | None = None
    for attempt in range(2):
        try:
            resp = _client.embeddings.create(
                model=EMBED_MODEL,
                input=[query],
                dimensions=DIMS,
                timeout=EMBED_TIMEOUT,
            )
            return np.array(resp.data[0].embedding, dtype=np.float32)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 0:
                time.sleep(0.5)
    logger.warning("embed_query failed after retry: %s", last)
    raise last if last else RuntimeError("embed_query failed")


def get_cross_references(embedding_id: int) -> list[dict]:
    conn = sqlite3.connect(str(EMBEDDINGS_DB))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ref_type, ref_text, ref_target FROM cross_references WHERE embedding_id = ?",
            (embedding_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # cross_references table may not exist (new embedding pipeline)
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def search(query: str, limit: int = 50, acts: set[str] | None = None,
           source_types: set[str] | None = None,
           series: set[str] | None = None) -> list[dict]:
    """Embed the query and return the top-K nearest chunks by cosine similarity.

    Scope filters (acts, source_types, series) are applied as a mask
    BEFORE the top-K cut, not after — a scoped query masks out non-matching
    rows first, so it isn't left choosing from whatever happened to land in
    an unfiltered top 50.
    """
    _ensure_loaded()
    if _ids is None or _matrix is None:
        return []
    query_vec = embed_query(query)
    scores = _matrix @ query_vec

    r = _rows
    if acts or source_types or series:
        # each test runs on the small value array, then fans out via the codes
        mask = np.ones(scores.shape[0], dtype=bool)
        if acts:
            mask &= np.isin(r["act_vals"], list(acts))[r["act_codes"]]
        if source_types:
            mask &= np.isin(r["type_vals"], list(source_types))[r["type_codes"]]
        if series:
            # Exact series match, the same rule searchd applies (see
            # searchd_client.ruling_series) — not a prefix: `GST` != `GSTR`.
            if "series_vals" not in r:
                from backend.services.searchd_client import ruling_series
                r["series_vals"] = np.array([ruling_series(str(v)) for v in r["section_vals"]], dtype=object)
            is_ruling = (r["type_vals"] == "ruling")[r["type_codes"]]
            series_hit = np.isin(r["series_vals"], list(series))
            mask &= (~is_ruling) | series_hit[r["section_codes"]]
        scores = np.where(mask, scores, -np.inf)

    top_idx = np.argsort(-scores)[:limit]
    top_idx = top_idx[np.isfinite(scores[top_idx])]

    results = []
    for idx in top_idx:
        results.append(_row_result(int(idx), float(scores[idx])))
    return results


def _row_result(i: int, score: float) -> dict:
    """Row i in the API's shape, from the compact store."""
    r = _rows
    source_type = str(r["type_vals"][r["type_codes"][i]])
    return {
        "embedding_id": int(_ids[i]),
        "source_type": source_type,
        # rows store _map_type's source_type; the registry maps it to a content type
        "content_type": content_type_for(source_type),
        "act": None if i in _none["act"] else str(r["act_vals"][r["act_codes"][i]]),
        "section": None if i in _none["section"] else str(r["section_vals"][r["section_codes"][i]]),
        "title": None if i in _none["title"] else _row_text(i, 0),
        "score": score,
        "snippet": _row_text(i, 1),
    }
