#!/usr/bin/env python3
"""Check similarity_index content structure."""
import sqlite3
from pathlib import Path

db = sqlite3.connect(f"{Path(__file__).resolve().parent.parent}/data/embeddings.db")

# Check what types of source → neighbor relationships exist
rows = db.execute("""
    SELECT e1.source_type AS src_type, e2.source_type AS dst_type, COUNT(*) as cnt
    FROM similarity_index s
    JOIN embeddings e1 ON s.embedding_id = e1.id
    JOIN embeddings e2 ON s.neighbor_id = e2.id
    GROUP BY e1.source_type, e2.source_type
    ORDER BY cnt DESC
""").fetchall()

print("Similarity relationships by type pair:")
for r in rows:
    print(f"  {r[0]:12s} → {r[1]:12s}: {r[2]:>8,}")

# For section 52-10, check if there are ANY neighbors (not just cases/rulings)
sec_ids = db.execute("""
    SELECT id FROM embeddings 
    WHERE act = 'itaa-1997' AND section = '52-10' AND source_type = 'section'
""").fetchall()
sec_ids = [r[0] for r in sec_ids]
if sec_ids:
    placeholder = ','.join('?' * len(sec_ids))
    neighbors = db.execute(f"""
        SELECT e2.source_type, e2.act, e2.section, e2.section_title, s.similarity
        FROM similarity_index s
        JOIN embeddings e2 ON s.neighbor_id = e2.id
        WHERE s.embedding_id IN ({placeholder})
        ORDER BY s.similarity DESC
        LIMIT 10
    """, sec_ids).fetchall()
    print(f"\nNeighbors for 52-10 ({len(neighbors)} found):")
    for n in neighbors:
        print(f"  [{n[4]:.4f}] {n[0]:12s} {n[1]}:{n[2]} {n[3][:50]}")
else:
    print("\nNo section embeddings for 52-10")