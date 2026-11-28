#!/usr/bin/env python3
"""Embed ASIC Regulatory Guide paragraphs into data/embeddings.db.

One embedding per paragraph (source_type='regulatory_guide'), using
text-embedding-3-small. Idempotent: skips paragraphs whose text_hash already
matches. Run `scripts/build_vector_matrix.py` afterwards.

Usage:
  python3 scripts/embed_rgs.py [--limit N]
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

_home_env = Path(os.environ.get("LAWKITTY_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
if _home_env.exists():
    for _line in _home_env.read_text().splitlines():
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
# Cap paragraph text well under the 8192-token API limit; the long tail is
# glossary/table appendices (e.g. RG 244.100 = 263K chars) where the leading
# ~12K chars already carry the substantive definitions.
MAX_PARA_CHARS = 12000
OUT_DB = DATA_DIR / "embeddings.db"
PARA_DIR = DATA_DIR / "regulatory-guides" / "paragraphs"

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

    text-embedding-3-small has a 1,000,000 TPM ceiling that a full guide corpus
    will hit. Without a retry the caller's failure path skips the file's
    remaining batches, which silently leaves real paragraphs unembedded (a
    24,133-paragraph run lost 7,507 that way).
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
    args = ap.parse_args()

    files = sorted(PARA_DIR.glob("RG_*.json")) if PARA_DIR.exists() else []
    print(f"{len(files)} RG paragraph files")

    conn = sqlite3.connect(str(OUT_DB))
    init_db(conn)

    total = added = skipped = 0
    for fpath in files:
        try:
            d = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        title = str(d.get("title") or "").strip()
        rg_label = str(d.get("rg") or fpath.stem).replace("_", " ")
        rel_path = f"regulatory-guides/paragraphs/{fpath.name}"
        existing = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT chunk_index, text_hash FROM embeddings WHERE file_path = ?",
                (rel_path,),
            ).fetchall()
        }

        # Build the pending work for this file.
        pending = []  # (chunk_index, num, etext, hash)
        for idx, p in enumerate(d.get("paragraphs", []) or []):
            num = str(p.get("num") or "").strip()
            text = str(p.get("text") or "").strip()
            if not text:
                continue
            if len(text) > MAX_PARA_CHARS:
                text = text[:MAX_PARA_CHARS].rsplit("\n", 1)[0].strip()
            etext = f"[Guide]: {rg_label} — {title}\n[Paragraph]: {num}\n\n{text}"
            h = text_hash(etext)
            total += 1
            if args.limit and total > args.limit:
                break
            if existing.get(idx) == h:
                skipped += 1
                continue
            pending.append((idx, num, etext, h))

        # Embed in batches and upsert.
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
                           text_hash=excluded.text_hash,
                           embedding_text=excluded.embedding_text,
                           embedding=excluded.embedding,
                           model=excluded.model""",
                    ("regulatory_guide", "regulatory-guides", num, title, idx,
                     rel_path, h, etext, array('f', vec).tobytes(), MODEL),
                )
                added += 1
        conn.commit()

    conn.close()
    print(f"\nRG embeddings: {total} paragraphs, {added} written, {skipped} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
