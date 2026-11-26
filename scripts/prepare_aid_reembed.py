#!/usr/bin/env python3
"""Prepare AID for re-embedding as whole documents.

Three jobs, in order, all idempotent:

1. Split oversized AID chunks in data/rulings/paragraphs/AID_*.json. The embedder
   truncates text at MAX_PARA_CHARS = 12000, and 75 AID documents exceed it (max
   25,906 chars) — truncation would silently drop the tail of those decisions, so
   they are split on sentence boundaries into chunks that each fit.

2. Write a key-level backup of every AID row about to be deleted, to
   scratch/aid_rows_deleted_backup.json. Embeddings are regenerable from text, so
   keys are enough to audit the delete; the vectors are not copied.

3. Delete the two stale AID rowsets from data/embeddings.db:
     * file_path LIKE 'rulings/AID_%'            — 5,931 metadata STUBS: their
       embedding_text is ~160 chars of "[Ruling] AID_2001_1 [Title] ... [Summary]
       true [References] ..." with NO document body, so ATOID searches have been
       matching a title stub rather than the decision.
     * file_path LIKE 'rulings/paragraphs/AID_%' — tonight's 944 marker-based
       paragraphs, superseded by the whole-document chunks.
   Both would otherwise duplicate every ATOID alongside the new one-chunk-per-document rows.

Run: /usr/bin/python3.12 scripts/prepare_aid_reembed.py [--dry-run]
"""
from __future__ import annotations
import os

import json
import re
import sqlite3
import sys
from pathlib import Path

MEOW = Path(__file__).resolve().parent.parent
PARA = MEOW / "data" / "rulings" / "paragraphs"
DB = MEOW / "data" / "embeddings.db"
BACKUP = Path(os.path.join(os.path.expanduser(os.environ.get("HERMES_DIR", "~/.hermes")), "cache", "scratch", "aid_rows_deleted_backup.json"))
CAP = 12000
TARGET = 11000  # split target, leaving room below the cap


def split_long_text(text: str, target: int = TARGET) -> list[str]:
    """Split on sentence boundaries so each piece is <= target where possible."""
    if len(text) <= CAP:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    cur = ""
    for s in sentences:
        # a single sentence longer than the target: hard-wrap it
        while len(s) > target:
            head, s = s[:target], s[target:]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
        if len(cur) + len(s) + 1 > target:
            if cur:
                out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def main() -> None:
    dry = "--dry-run" in sys.argv

    # ---------- 1. split oversized chunks ----------
    changed = []
    for f in sorted(PARA.glob("AID_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        ps = d.get("paragraphs") or []
        if len(ps) != 1 or len(ps[0].get("text") or "") <= CAP:
            continue
        pieces = split_long_text(ps[0]["text"])
        over = [len(p) for p in pieces if len(p) > CAP]
        if over:
            print(f"  WARNING {f.stem}: pieces still over cap: {over}")
        d["paragraphs"] = [
            {"num": str(i + 1), "label": "Document", "text": p} for i, p in enumerate(pieces)
        ]
        d["chunked_from_single"] = True
        if not dry:
            f.write_text(json.dumps(d, indent=1), encoding="utf-8")
        changed.append((f.stem, len(ps[0]["text"]), len(pieces)))
    print(f"1. oversized chunks split: {len(changed)} documents -> {sum(c[2] for c in changed)} chunks")
    for stem, before, parts in sorted(changed, key=lambda x: -x[1])[:5]:
        print(f"     {stem:16} {before:,} chars -> {parts} chunks")

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, file_path, section, chunk_index, text_hash FROM embeddings "
        "WHERE file_path LIKE 'rulings/AID_%' OR file_path LIKE 'rulings/paragraphs/AID_%'"
    ).fetchall()
    print(f"\n2. stale AID rows to delete: {len(rows):,}")
    stubs = sum(1 for r in rows if r[1].startswith("rulings/AID_") and "/paragraphs/" not in r[1])
    marker = len(rows) - stubs
    print(f"     metadata stubs (rulings/AID_*)          : {stubs:,}")
    print(f"     marker-based paragraphs (paragraphs/AID*): {marker:,}")

    if dry:
        print("\ndry run — nothing written")
        return

    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(
        json.dumps(
            [{"id": r[0], "file_path": r[1], "section": r[2], "chunk_index": r[3], "text_hash": r[4]} for r in rows]
        ),
        encoding="utf-8",
    )
    print(f"\n3. backup written: {BACKUP} ({BACKUP.stat().st_size:,} bytes)")

    cur = conn.execute(
        "DELETE FROM embeddings WHERE file_path LIKE 'rulings/AID_%' "
        "OR file_path LIKE 'rulings/paragraphs/AID_%'"
    )
    conn.commit()
    print(f"     deleted {cur.rowcount:,} rows")

    left = conn.execute(
        "SELECT count(*) FROM embeddings WHERE file_path LIKE '%AID_%'"
    ).fetchone()[0]
    total = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    print(f"\nAFTER: AID rows remaining {left:,} (expect 0 before re-embedding), DB total {total:,}")
    conn.close()


if __name__ == "__main__":
    main()
