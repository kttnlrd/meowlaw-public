#!/usr/bin/env python3
"""Embed ATO private rulings (mop_up json_llm output) into embeddings.db.

One chunk per Q&A pair + one per reasons paragraph. Stored as
source_type='ruling' / act='private' / section=<authnum> so they flow
through the existing rulings tab + vector search without UI changes.

Idempotent: (file_path, chunk_index) rows with matching text_hash are skipped,
so re-running after mop_up finishes picks up only the new files.

Usage:
  python3 scripts/embed_private_rulings.py                     # full run
  python3 scripts/embed_private_rulings.py --limit 100         # first 100 files
  python3 scripts/embed_private_rulings.py --json-dir DIR      # override source
"""
from __future__ import annotations
import os

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import openai

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.openai_embed import (  # noqa: E402
    MODEL, DIMS, BATCH_SIZE, MAX_CHARS, DATA_DIR,
    chunk_text, text_hash, embed_batch, ensure_model_column,
)

# OpenAI TPM limit for text-embedding-3-small is 1M/min; ~400 tokens per chunk
# means 100-chunk batches (~40K tokens) can trip it. Halve the batch and retry
# with backoff on 429 so the run survives rate limits.
BATCH_SIZE = 50


def embed_batch_with_retry(texts: list[str], attempts: int = 8) -> list[list[float]]:
    """embed_batch with exponential backoff on 429 / 5xx."""
    delay = 2.0
    for i in range(attempts):
        try:
            return embed_batch(texts)
        except openai.RateLimitError as e:
            wait = delay * (2 ** i)
            print(f"  429 rate limit — retrying in {wait:.0f}s ({e})", flush=True)
            time.sleep(wait)
        except openai.APIError as e:
            wait = delay * (2 ** i)
            print(f"  API error — retrying in {wait:.0f}s ({e})", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"embed_batch failed after {attempts} attempts")

DEFAULT_JSON_DIR = Path(os.path.join(os.path.expanduser(os.environ.get("HERMES_DIR", "~/.hermes")), "private_rulings", "data", "json_llm"))
OUT_DB = DATA_DIR / "embeddings.db"


def norm_text(v) -> str:
    """Normalise a str or list-of-strings field into plain text."""
    if v is None:
        return ""
    if isinstance(v, list):
        return "\n\n".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip()


def build_chunks(d: dict) -> list[str]:
    """Return embedding texts for one ruling: QA pairs + reasons."""
    auth = str(d.get("authorisation_number", "")).strip()
    subject = norm_text(d.get("subject"))
    header = f"[Ruling] Private {auth}\n[Title] {subject}\n"

    refs = []
    for r in (d.get("legislation_refs_llm") or [])[:10]:
        refs.append(str(r))
    for r in (d.get("case_refs_llm") or [])[:10]:
        refs.append(str(r))
    refs_str = f"\n[References] {', '.join(refs)}" if refs else ""

    chunks: list[str] = []
    seen: set[str] = set()
    for i, pair in enumerate(d.get("qa_pairs") or [], start=1):
        q = norm_text(pair.get("question"))
        a = norm_text(pair.get("answer"))
        if not q and not a:
            continue
        body = f"[Question {i}] {q}\n[Answer {i}] {a}"
        for piece in chunk_text(body, MAX_CHARS):
            ctext = f"{header}{piece}{refs_str}"
            if ctext in seen:
                continue  # ruling quotes the same passage twice — embed once
            seen.add(ctext)
            chunks.append(ctext)
    reasons = norm_text(d.get("reasons_for_decision"))
    if reasons:
        for piece in chunk_text(f"[Reasons] {reasons}", MAX_CHARS):
            ctext = f"{header}{piece}{refs_str}"
            if ctext in seen:
                continue
            seen.add(ctext)
            chunks.append(ctext)
    return chunks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="process at most N files (0=all)")
    ap.add_argument("--json-dir", type=Path, default=DEFAULT_JSON_DIR)
    ap.add_argument("--dry-run", action="store_true", help="count chunks without embedding")
    args = ap.parse_args()

    files = sorted(args.json_dir.glob("*.json"))
    print(f"json_llm files: {len(files)}", flush=True)

    conn = sqlite3.connect(OUT_DB)
    ensure_model_column(conn)
    conn.execute("PRAGMA journal_mode=WAL")

    total_chunks = 0
    total_chars = 0
    skipped = 0
    embedded = 0
    n_files = 0
    t0 = time.time()
    pending: list[tuple] = []  # (fpath, auth, title, idx, etext, h)


    def flush(pending: list[tuple]) -> None:
        nonlocal embedded
        for b in range(0, len(pending), BATCH_SIZE):
            batch = pending[b:b + BATCH_SIZE]
            vecs = embed_batch_with_retry([p[4] for p in batch])
            for p, vec in zip(batch, vecs):
                from array import array
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
                    ("ruling", "private", p[1], p[2], p[3],
                     p[0], p[5], p[4], array("f", vec).tobytes(), MODEL),
                )
            embedded += len(batch)
        conn.commit()
        pending.clear()

    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("mop_status") != "ok":
            continue
        chunks = build_chunks(d)
        if not chunks:
            continue
        n_files += 1
        fpath = f"private_rulings/{f.stem}.json"
        auth = str(d.get("authorisation_number", "")).strip()
        subject = norm_text(d.get("subject"))
        title = f"Private ruling {auth} — {subject}" if subject else f"Private ruling {auth}"

        existing = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT chunk_index, text_hash FROM embeddings WHERE file_path = ?", (fpath,)
            ).fetchall()
        }
        to_embed, texts, hashes, idxs = [], [], [], []
        for idx, ctext in enumerate(chunks):
            h = text_hash(ctext)
            if existing.get(idx) == h:
                skipped += 1
                continue
            to_embed.append(idx)
            texts.append(ctext)
            hashes.append(h)
            idxs.append(idx)
            total_chars += len(ctext)
        total_chunks += len(chunks)

        # Stale-row cleanup: drop only chunk indexes that no longer exist for
        # this file (mop_up revisions can shrink a ruling's Q&A/reasons).
        # IMPORTANT: unchanged files must NOT be touched — the previous version
        # deleted all rows whenever every chunk was skipped (idxs empty), which
        # wiped the whole DB on rerun. Only indexes absent from the current
        # chunk set are stale.
        current_idxs = set(range(len(chunks)))
        if existing:
            stale = sorted(set(existing) - current_idxs)
            if stale:
                marks = ",".join("?" * len(stale))
                conn.execute(
                    f"DELETE FROM embeddings WHERE file_path = ? AND chunk_index IN ({marks})",
                    [fpath] + stale,
                )

        if args.dry_run:
            embedded += len(to_embed)
        else:
            for idx, etext, h in zip(idxs, texts, hashes):
                pending.append((fpath, auth, title, idx, etext, h))
            if len(pending) >= BATCH_SIZE:
                flush(pending)

        if args.limit and n_files >= args.limit:
            break
        if n_files % 500 == 0:
            el = time.time() - t0
            rate = embedded / el * 3600 if el > 0 else 0
            print(f"  {n_files} files | {embedded} chunks embedded | {rate:.0f}/hr | {el:.0f}s", flush=True)

    if pending:
        flush(pending)
    conn.close()
    el = time.time() - t0
    est_tokens = total_chars / 4
    print(f"\nfiles processed: {n_files}")
    print(f"chunks: {total_chunks} (skipped {skipped}, embedded {embedded})")
    print(f"chars: {total_chars:,} (~{est_tokens:,.0f} tokens, ~${est_tokens * 0.02 / 1e6:.2f} at 3-small)")
    print(f"elapsed: {el:.0f}s")


if __name__ == "__main__":
    main()
