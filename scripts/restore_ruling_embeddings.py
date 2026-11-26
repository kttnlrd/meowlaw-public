#!/usr/bin/env python3.12
"""Restore the ruling + case embeddings the local pipeline deleted.

scripts/embed_legislation.py computed its "stale" set as

    existing_files - seen_files      # seen_files = only the directories it walks

so every row whose file_path lay outside data/*/sections - i.e. every ruling and case row - was
treated as "the file is gone" and deleted, even though nothing was missing.  It removed 260,297
rows: 248,792 ruling, 8,468 case, 2,244 section, 793 commentary.

The ruling and case rows are restored here from the 2026-08-30 backup, which holds them at the
same model and dimension (text-embedding-3-small / 1536), so no API call and no cost.  The
section/commentary rows in that backup are NOT restored: their file_path values no longer exist
in the corpus (the layout moved), so they would be dangling; the live section/commentary rows were
re-embedded from the current corpus instead.
"""
from __future__ import annotations
import os

import sqlite3
import sys
from pathlib import Path

BACKUP = Path(os.path.expanduser(os.environ.get("EMBEDDINGS_BACKUP_DB", "~/backups/legislation-explorer-v3.1-20260830/embeddings.db")))
LIVE = Path(f"{Path(__file__).resolve().parent.parent}/data/embeddings.db")
COLS = ("source_type", "act", "section", "section_title", "chunk_index",
        "file_path", "text_hash", "embedding_text", "embedding", "model")
RESTORE_TYPES = ("ruling", "case")


def main() -> int:
    if not BACKUP.exists():
        print(f"no backup at {BACKUP}")
        return 2

    con = sqlite3.connect(LIVE)
    before = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    live_cols = [r[1] for r in con.execute("PRAGMA table_info(embeddings)")]
    con.execute(f"ATTACH DATABASE ? AS b", (str(BACKUP),))
    bcols = [r[1] for r in con.execute("PRAGMA b.table_info(embeddings)")]
    if live_cols != bcols:
        print(f"schema mismatch:\n  live  {live_cols}\n  backup{bcols}")
        return 2
    print(f"  rows before: {before:,}")

    ph = ",".join("?" * len(RESTORE_TYPES))
    collist = ", ".join(COLS)
    cur = con.execute(
        f"INSERT OR IGNORE INTO embeddings ({collist}) "
        f"SELECT {collist} FROM b.embeddings WHERE source_type IN ({ph})",
        RESTORE_TYPES,
    )
    con.commit()
    print(f"  inserted: {cur.rowcount:,}")

    after = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    per = dict(con.execute("SELECT source_type, COUNT(*) FROM embeddings GROUP BY 1").fetchall())
    dims = {len(r[0]) // 4 for r in con.execute("SELECT embedding FROM embeddings")}
    models = dict(con.execute("SELECT model, COUNT(*) FROM embeddings GROUP BY 1").fetchall())
    con.execute("DETACH DATABASE b")
    con.commit()
    con.close()

    print(f"  rows after : {after:,}")
    print(f"  by type    : {per}")
    print(f"  dims       : {dims}  (1536 only = one vector space)")
    print(f"  models     : {models}")
    ok = (len(dims) == 1 and dims == {1536} and len(models) == 1
          and per.get("ruling", 0) > 200000 and per.get("case", 0) > 8000)
    print("  RESTORED CONSISTENTLY:" , ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
