#!/usr/bin/env python3
"""Build the vector-search matrix snapshot from embeddings.db.

Writes data/embeddings_matrix.npy (float32 [N,1536]), data/embeddings_ids.npy
(int64 [N]) and data/embeddings_meta.pkl (compact per-id search metadata).
The service mmaps the matrix at startup instead of loading every blob into
RAM — that was OOM-killing it once private rulings pushed the corpus past
~270K chunks (1.5GB cgroup limit, 1.7GB matrix).

Memory-flat: fills the .npy through numpy open_memmap in batches.

Usage:
  backend/venv/bin/python scripts/build_vector_matrix.py
"""
from __future__ import annotations

import pickle
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import BASE  # noqa: E402

DB = BASE / "data" / "embeddings.db"
DIMS = 1536
BATCH = 10000
MATRIX = BASE / "data" / "embeddings_matrix.npy"
IDS = BASE / "data" / "embeddings_ids.npy"
META = BASE / "data" / "embeddings_meta.pkl"


def main() -> int:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    # Single read transaction: consistent snapshot even while the embed
    # run appends rows — otherwise count(*) and the batch reads drift and
    # the loop overruns the array (IndexError on the last rows).
    conn.execute("BEGIN")
    total = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    print(f"embeddings rows: {total:,}", flush=True)

    # ids first (small)
    ids = np.empty(total, dtype=np.int64)
    # matrix via memmap so peak RAM stays flat
    mat = np.lib.format.open_memmap(str(MATRIX), mode="w+", dtype=np.float32, shape=(total, DIMS))
    meta: dict[int, tuple] = {}

    t0 = time.time()
    done = 0
    last_id = 0
    while done < total:
        rows = conn.execute(
            "SELECT id, embedding, source_type, act, section, section_title, embedding_text "
            "FROM embeddings WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, BATCH),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            if done >= total:
                break  # snapshot boundary reached
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if vec.shape[0] != DIMS:
                print(f"SKIP id={row['id']} dim={vec.shape[0]} (mixed-dim legacy row)", flush=True)
                continue
            ids[done] = row["id"]
            mat[done] = vec
            meta[row["id"]] = (
                row["source_type"],
                row["act"],
                row["section"],
                row["section_title"],
                (row["embedding_text"] or "")[:300],
            )
            done += 1
            last_id = row["id"]
        if done % BATCH == 0 or done >= total:
            print(f"  {done:,}/{total:,} ({time.time()-t0:.0f}s)", flush=True)
        # Defensive: stop exactly at the snapshot count (never overrun arrays)
        if done >= total:
            break

    conn.commit()
    conn.close()
    mat.flush()
    del mat
    np.save(IDS, ids[:done])
    with open(META, "wb") as f:
        pickle.dump(meta, f, protocol=4)
    print(f"wrote {MATRIX} ({done:,}x{DIMS}), {IDS}, {META} in {time.time()-t0:.0f}s", flush=True)
    print(f"matrix file size: {MATRIX.stat().st_size/1e9:.2f} GB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
