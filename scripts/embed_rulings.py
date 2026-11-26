#!/usr/bin/env python3
"""Embed ATO ruling paragraphs into data/embeddings.db.

One embedding per numbered paragraph (source_type='ruling'), using
text-embedding-3-small. Mirrors scripts/embed_rgs.py (same DB, DIMS=1536,
MAX_PARA_CHARS=12000, idempotent by text_hash). Run
`scripts/build_vector_matrix.py` afterwards.

Input : data/rulings/paragraphs/<STEM>.json  (from split_ruling_paragraphs.py)
Rows  : source_type='ruling', act='ato', section=<ruling reference>,
        section_title=<ruling title>, file_path='rulings/paragraphs/<STEM>.json'

`section` holds the RULING REFERENCE (e.g. 'TR 2025/1'), not the bare paragraph
number: the serving layer merges search results on (act, section) and the
frontend builds the ruling link from `section` (/rulings/<slug>), so a bare
number would both collide across rulings and break the link. The retrieval unit
is still one paragraph — each paragraph gets its own row and embedding — and the
paragraph number is carried in the embedded text ('[Paragraph]: 14') and so
appears in the result snippet.

Usage:
  python3 scripts/embed_rulings.py [--limit N] [--series TR]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from array import array
from pathlib import Path

_hermes_env = Path(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
if _hermes_env.exists():
    for _line in _hermes_env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            k, v = _line.strip().split("=", 1)
            if k == "OPENAI_API_KEY":
                os.environ.setdefault(k, v)

from openai import OpenAI  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import DATA_DIR  # noqa: E402

MODEL = "text-embedding-3-small"
DIMS = 1536
BATCH_SIZE = 100
# Cap paragraph text well under the 8192-token API limit (mirrors embed_rgs.py).
MAX_PARA_CHARS = 12000
OUT_DB = DATA_DIR / "embeddings.db"
PARA_DIR = DATA_DIR / "rulings" / "paragraphs"

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            act TEXT NOT NULL,
            section TEXT NOT NULL,
            section_title TEXT,
            chunk_index INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            embedding_text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            model TEXT DEFAULT 'text-embedding-3-small',
            UNIQUE(file_path, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_embeddings_source ON embeddings(source_type);
        CREATE INDEX IF NOT EXISTS idx_embeddings_file ON embeddings(file_path);
    """)
    conn.commit()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch, retrying with backoff on 429 / transient API errors.

    text-embedding-3-small has a 1,000,000 TPM ceiling; a long corpus WILL hit
    it. The upstream embed_rgs.py does not retry, so a 429 silently drops the
    whole file's paragraphs. We retry here (honouring the API's 'try again in
    Ns' hint when present) so no paragraphs are lost.
    """
    if not texts:
        return []
    delay = 2.0
    last = None
    for attempt in range(8):
        try:
            resp = client.embeddings.create(model=MODEL, input=texts, dimensions=DIMS)
            return [r.embedding for r in resp.data]
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == 7:
                break
            wait = delay
            m = re.search(r'try again in ([\d.]+)s', str(e))
            if m:
                wait = max(wait, float(m.group(1)) + 0.5)
            print(f"  embed batch retry {attempt + 1}/8 in {wait:.1f}s: {str(e)[:120]}")
            time.sleep(wait)
            delay = min(delay * 2, 20.0)
    raise last


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="limit paragraphs (testing)")
    ap.add_argument("--series", help="only this series prefix (e.g. TR)")
    args = ap.parse_args()

    files = sorted(PARA_DIR.glob("*.json")) if PARA_DIR.exists() else []
    files = [f for f in files if f.name != "_split_stats.json"]
    if args.series:
        files = [f for f in files if f.stem.split("_")[0] == args.series]
    print(f"{len(files)} ruling paragraph files")

    conn = sqlite3.connect(str(OUT_DB))
    init_db(conn)

    total = added = skipped = 0
    for fpath in files:
        try:
            d = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        title = str(d.get("title") or "").strip()
        ref = str(d.get("ruling") or fpath.stem.replace("_", " ")).strip()
        rel_path = f"rulings/paragraphs/{fpath.name}"
        existing = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT chunk_index, text_hash FROM embeddings WHERE file_path = ?",
                (rel_path,),
            ).fetchall()
        }

        pending = []  # (chunk_index, num, etext, hash)
        for idx, p in enumerate(d.get("paragraphs", []) or []):
            num = str(p.get("num") or "").strip()
            text = str(p.get("text") or "").strip()
            if not text:
                continue
            if len(text) > MAX_PARA_CHARS:
                text = text[:MAX_PARA_CHARS].rsplit("\n", 1)[0].strip()
            etext = f"[Ruling]: {ref} — {title}\n[Paragraph]: {num}\n\n{text}"
            h = text_hash(etext)
            total += 1
            if args.limit and total > args.limit:
                break
            if existing.get(idx) == h:
                skipped += 1
                continue
            pending.append((idx, num, etext, h))

        for start in range(0, len(pending), BATCH_SIZE):
            batch = pending[start:start + BATCH_SIZE]
            try:
                vectors = embed_batch([b[2] for b in batch])
            except Exception as e:  # noqa: BLE001
                print(f"  embed error on {fpath.name}: {e}")
                break
            for (idx, num, etext, h), vec in zip(batch, vectors):
                conn.execute(
                    """INSERT INTO embeddings
                       (source_type, act, section, section_title, chunk_index,
                        file_path, text_hash, embedding_text, embedding, model)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(file_path, chunk_index) DO UPDATE SET
                           section=excluded.section,
                           section_title=excluded.section_title,
                           text_hash=excluded.text_hash,
                           embedding_text=excluded.embedding_text,
                           embedding=excluded.embedding,
                           model=excluded.model""",
                    ("ruling", "ato", ref, title or ref, idx,
                     rel_path, h, etext, array('f', vec).tobytes(), MODEL),
                )
                added += 1
        conn.commit()

    conn.close()
    print(f"\nRuling paragraph embeddings: {total} paragraphs, "
          f"{added} written, {skipped} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
