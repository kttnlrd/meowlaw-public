#!/usr/bin/env python3
"""Check similarity thresholds for cross-type pairs."""
import sqlite3
import numpy as np
from collections import defaultdict
from pathlib import Path

db = sqlite3.connect(f"{Path(__file__).resolve().parent.parent}/data/embeddings.db")

# Get sample embeddings to check cross-type similarity distribution
rows = db.execute("""
    SELECT e1.source_type AS s1, e2.source_type AS s2, s.similarity
    FROM similarity_index s
    JOIN embeddings e1 ON s.embedding_id = e1.id
    JOIN embeddings e2 ON s.neighbor_id = e2.id
    WHERE e1.source_type != e2.source_type
    LIMIT 10000
""").fetchall()

# Check if there are ANY section→case or section→ruling pairs for 109U
sec_109u = db.execute("""
    SELECT id FROM embeddings 
    WHERE act = 'itaa-1997' AND section = '109U' AND source_type = 'section'
""").fetchall()

if sec_109u:
    ids = [r[0] for r in sec_109u]
    placeholders = ','.join('?' * len(ids))
    neighbors = db.execute(f"""
        SELECT e2.source_type, e2.act, e2.section, e2.section_title, s.similarity
        FROM similarity_index s
        JOIN embeddings e2 ON s.neighbor_id = e2.id
        WHERE s.embedding_id IN ({placeholders})
        AND e2.source_type IN ('case', 'ruling')
        ORDER BY s.similarity DESC
        LIMIT 20
    """, ids).fetchall()
    print(f"109U similar cases/rulings in similarity_index: {len(neighbors)}")
else:
    print("109U NOT in embeddings DB at all!")

# How many sections have no section→case/ruling links?
sec_count = db.execute("SELECT COUNT(DISTINCT id) FROM embeddings WHERE source_type = 'section'").fetchone()[0]
print(f"\nTotal sections in embeddings: {sec_count}")

# Count sections with any case/ruling neighbor
sections_with_links = db.execute("""
    SELECT COUNT(DISTINCT s.embedding_id) 
    FROM similarity_index s
    JOIN embeddings e1 ON s.embedding_id = e1.id
    JOIN embeddings e2 ON s.neighbor_id = e2.id
    WHERE e1.source_type = 'section' AND e2.source_type IN ('case', 'ruling')
""").fetchone()[0]
print(f"Sections with case/ruling neighbors: {sections_with_links}")