#!/usr/bin/env python3
"""Build cross-type similarity links: section → case, section → ruling.

Adds missing section→case and section→ruling edges to similarity_index."""
import sqlite3
import numpy as np
from collections import defaultdict
from pathlib import Path

DB = f"{Path(__file__).resolve().parent.parent}/data/embeddings.db"
conn = sqlite3.connect(DB)

# Load all embeddings
rows = conn.execute("SELECT id, source_type, act, section, embedding FROM embeddings").fetchall()
print(f"Total embeddings: {len(rows)}")

ids = np.array([r[0] for r in rows], dtype=np.int64)
source_types = [r[1] for r in rows]
acts = [r[2] for r in rows]
secs = [r[3] for r in rows]
vectors = np.frombuffer(
    b"".join(r[4] for r in rows), dtype=np.float32
).reshape(len(rows), -1)

# L2 normalize
norms = np.linalg.norm(vectors, axis=1, keepdims=True)
norms[norms == 0] = 1
vectors = vectors / norms

# Index section, case, ruling positions
sec_indices = [i for i, t in enumerate(source_types) if t == "section"]
case_indices = [i for i, t in enumerate(source_types) if t == "case"]
ruling_indices = [i for i, t in enumerate(source_types) if t == "ruling"]

print(f"Sections: {len(sec_indices)}, Cases: {len(case_indices)}, Rulings: {len(ruling_indices)}")

# Compute section→case similarity
sec_vecs = vectors[sec_indices]
case_vecs = vectors[case_indices]
ruling_vecs = vectors[ruling_indices]

batch_size = 1000
inserted = 0

print("\nComputing section → case similarity...")
for b_start in range(0, len(sec_indices), batch_size):
    b_end = min(b_start + batch_size, len(sec_indices))
    batch_sec_ids = [ids[sec_indices[i]] for i in range(b_start, b_end)]
    batch_sim = vectors[sec_indices[b_start:b_end]] @ case_vecs.T
    
    conn.execute("BEGIN")
    for i in range(b_end - b_start):
        sim_row = batch_sim[i]
        top_k = np.argsort(-sim_row)[:5]
        for j in top_k:
            score = float(sim_row[j])
            if score >= 0.3:
                neighbor_id = int(ids[case_indices[j]])
                # Skip if already exists
                existing = conn.execute(
                    "SELECT 1 FROM similarity_index WHERE embedding_id=? AND neighbor_id=?",
                    (int(batch_sec_ids[i]), neighbor_id)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO similarity_index VALUES (?, ?, ?)",
                        (int(batch_sec_ids[i]), neighbor_id, score)
                    )
                    inserted += 1
    conn.commit()
    print(f"  Batch [{b_start}:{b_end}]: {inserted} section→case edges so far")

print(f"\nComputing section → ruling similarity...")
for b_start in range(0, len(sec_indices), batch_size):
    b_end = min(b_start + batch_size, len(sec_indices))
    batch_sec_ids = [ids[sec_indices[i]] for i in range(b_start, b_end)]
    batch_sim = vectors[sec_indices[b_start:b_end]] @ ruling_vecs.T
    
    conn.execute("BEGIN")
    for i in range(b_end - b_start):
        sim_row = batch_sim[i]
        top_k = np.argsort(-sim_row)[:5]
        for j in top_k:
            score = float(sim_row[j])
            if score >= 0.3:
                neighbor_id = int(ids[ruling_indices[j]])
                existing = conn.execute(
                    "SELECT 1 FROM similarity_index WHERE embedding_id=? AND neighbor_id=?",
                    (int(batch_sec_ids[i]), neighbor_id)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO similarity_index VALUES (?, ?, ?)",
                        (int(batch_sec_ids[i]), neighbor_id, score)
                    )
                    inserted += 1
    conn.commit()
    print(f"  Batch [{b_start}:{b_end}]: {inserted} section→ruling edges so far")

conn.close()
print(f"\nDone. Inserted {inserted} new cross-type edges.")