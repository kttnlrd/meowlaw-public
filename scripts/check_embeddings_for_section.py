#!/usr/bin/env python3
"""Check embeddings for section 52-10 and find similar cases/rulings."""
import sqlite3
from pathlib import Path

db = sqlite3.connect(f"{Path(__file__).resolve().parent.parent}/data/embeddings.db")

# Find section 52-10 embeddings
rows = db.execute("""
    SELECT id, source_type, act, section, section_title 
    FROM embeddings 
    WHERE act = 'itaa-1997' AND section = '52-10'
""").fetchall()
print(f"52-10 embeddings: {len(rows)}")
for r in rows:
    print(f"  id={r[0]} type={r[1]} title={r[4]}")

# Find similar neighbors via similarity_index
sec_ids = [r[0] for r in rows]
if sec_ids:
    neighbors = db.execute(f"""
        SELECT s.neighbor_id, e.source_type, e.act, e.section, e.section_title, s.similarity
        FROM similarity_index s
        JOIN embeddings e ON s.neighbor_id = e.id
        WHERE s.embedding_id IN ({','.join('?' * len(sec_ids))})
        AND e.source_type IN ('case', 'ruling')
        ORDER BY s.similarity DESC
        LIMIT 20
    """, sec_ids).fetchall()
    print(f"\nSimilar cases/rulings for 52-10: {len(neighbors)}")
    for n in neighbors[:10]:
        print(f"  [{n[5]:.4f}] {n[1]:10s} {n[2]}:{n[3]} - {n[4][:60]}")

# Also check what similar sections exist for any section
sec_sample = db.execute("""
    SELECT e.id, e.act, e.section, e.section_title
    FROM embeddings e
    WHERE e.source_type = 'section'
    LIMIT 10
""").fetchall()
print(f"\nSample sections in embeddings:")
for s in sec_sample:
    print(f"  id={s[0]} {s[1]}:{s[2]} - {s[3][:50]}")

# Count totals
for t in ['case', 'ruling', 'section', 'commentary']:
    count = db.execute("SELECT COUNT(*) FROM embeddings WHERE source_type = ?", (t,)).fetchone()[0]
    print(f"  {t}: {count}")