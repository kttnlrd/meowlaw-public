#!/usr/bin/env python3
"""Build k-NN similarity index from embeddings.db.

For each embedding node, find the top-20 most similar nodes by cosine similarity.
Uses numpy matrix multiplication for batch computation.

Usage:
  python3 scripts/build_similarity_index.py
  python3 scripts/build_similarity_index.py --k 50   # top-50 neighbours
  python3 scripts/build_similarity_index.py --threshold 0.3  # lower threshold
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import DATA_DIR

DB = DATA_DIR / "embeddings.db"


def build_index(k: int = 20, threshold: float = 0.4):
    """Build k-NN index from all embeddings."""
    conn = sqlite3.connect(str(DB))
    rows = conn.execute("SELECT id, embedding FROM embeddings").fetchall()
    print(f"Loading {len(rows)} embeddings...")

    ids = np.array([r[0] for r in rows], dtype=np.int64)
    vectors = np.frombuffer(
        b"".join(r[1] for r in rows), dtype=np.float32
    ).reshape(len(rows), -1)

    dims = vectors.shape[1]
    print(f"Shape: ({len(rows)}, {dims})")

    # L2 normalise for cosine similarity via dot product
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    vectors = vectors / norms

    # Create similarity_index table
    conn.executescript("""
        DROP TABLE IF EXISTS similarity_index;
        CREATE TABLE similarity_index (
            embedding_id INTEGER NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
            neighbor_id INTEGER NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
            similarity REAL NOT NULL,
            PRIMARY KEY (embedding_id, neighbor_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sim_embedding ON similarity_index(embedding_id);
    """)
    conn.commit()

    # Batch compute similarity via matrix multiply
    # (N, D) @ (D, N) -> (N, N)  — but can OOM for large N
    # Process in batches to manage memory
    batch_size = 5000
    total_inserted = 0

    for batch_start in range(0, len(ids), batch_size):
        batch_end = min(batch_start + batch_size, len(ids))
        batch_ids = ids[batch_start:batch_end]
        batch_vecs = vectors[batch_start:batch_end]

        # Compute similarity for this batch against all vectors
        sim = batch_vecs @ vectors.T  # (B, N)

        conn.execute("BEGIN")
        for i in range(len(batch_ids)):
            order = np.argsort(-sim[i])
            order = order[order != (batch_start + i)]  # exclude self
            order = order[:k]
            for j in order:
                score = float(sim[i, j])
                if score >= threshold:
                    conn.execute(
                        "INSERT INTO similarity_index VALUES (?, ?, ?)",
                        (int(batch_ids[i]), int(ids[j]), score),
                    )
                    total_inserted += 1
        conn.commit()

        print(f"  Batch [{batch_start}:{batch_end}]: {total_inserted} edges so far")

    print(f"\nDone. {total_inserted} edges in similarity_index")
    by_node = conn.execute(
        "SELECT COUNT(DISTINCT embedding_id) FROM similarity_index"
    ).fetchone()[0]
    print(f"  Nodes with neighbours: {by_node}")
    conn.close()


if __name__ == "__main__":
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=20, help="Top-K neighbours per node")
    parser.add_argument("--threshold", type=float, default=0.4, help="Minimum similarity threshold")
    args = parser.parse_args()
    build_index(k=args.k, threshold=args.threshold)
