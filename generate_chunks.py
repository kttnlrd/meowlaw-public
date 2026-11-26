#!/usr/bin/env python3
"""
Generate OpenAI embeddings for documents missing from the chunks table.
Processes rulings and cases, chunks content by ~1000 chars with overlap,
generates text-embedding-3-small embeddings, and batch-inserts into chunks table.
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import execute_values
from openai import OpenAI

# --- Configuration ---
DB_HOST = "127.0.0.1"
DB_PORT = 5432
DB_USER = "postgres"
DB_PASSWORD = "postgres"
DB_NAME = "cadena_knowledge"

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
BATCH_SIZE = 100
EMBEDDING_MODEL = "text-embedding-3-small"

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_openai_key():
    """Load OpenAI API key from .env file."""
    env_path = os.path.expanduser(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    if not os.path.exists(env_path):
        log.error(".env file not found at %s", env_path)
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                return key
    log.error("OPENAI_API_KEY not found in .env")
    sys.exit(1)


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping chunks of approximately chunk_size characters."""
    if not text:
        return []
    chunks = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunks.append(text[start:end])
        if end >= text_len:
            break
        start += chunk_size - overlap
    return chunks


def get_embeddings(client, texts, model=EMBEDDING_MODEL):
    """Generate embeddings for a list of texts via OpenAI API."""
    if not texts:
        return []
    # Rate limit handling with retry
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(input=texts, model=model)
            # Return as list of lists: [[0.001, 0.002, ...], ...]
            return [item.embedding for item in response.data]
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning("OpenAI API error: %s. Retrying in %ds...", e, wait)
                time.sleep(wait)
            else:
                log.error("OpenAI API failed after %d attempts: %s", max_retries, e)
                raise


def format_embedding_vector(embedding_list):
    """Format embedding list as pgvector-compatible string '[0.1,0.2,...]'."""
    return '[' + ','.join(str(v) for v in embedding_list) + ']'


def main():
    # Load API key and init client
    api_key = load_openai_key()
    client = OpenAI(api_key=api_key)

    # Connect to database
    log.info("Connecting to PostgreSQL at %s:%s/%s...", DB_HOST, DB_PORT, DB_NAME)
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
    )
    conn.autocommit = False
    cur = conn.cursor()
    log.info("Connected.")

    # Step 1: Find documents needing chunking
    log.info("Finding documents missing from chunks table...")
    cur.execute("""
        SELECT id, content, doc_type, reference, title
        FROM documents
        WHERE (doc_type = 'ruling' OR doc_type = 'case')
          AND id NOT IN (SELECT document_id FROM chunks)
        ORDER BY doc_type, id
    """)
    rows = cur.fetchall()
    log.info("Found %d documents to process.", len(rows))

    if not rows:
        log.info("No documents need chunking. Exiting.")
        cur.close()
        conn.close()
        return

    # Step 2: Process documents
    total_chunks_inserted = 0
    total_docs_processed = 0
    total_api_calls = 0
    start_time = time.time()

    # Batch buffer: accumulate chunks up to BATCH_SIZE before inserting
    insert_buffer = []

    for doc_id, content, doc_type, reference, title in rows:
        total_docs_processed += 1
        chunks = chunk_text(content)

        if not chunks:
            log.debug("Document %s (%s) has empty content, skipping.", doc_id, reference or 'N/A')
            continue

        # Generate embeddings for this document's chunks
        try:
            embeddings = get_embeddings(client, chunks)
            total_api_calls += 1
        except Exception as e:
            log.error("Failed to generate embeddings for document %s: %s", doc_id, e)
            continue

        embedding_vecs = embeddings if embeddings else []
        for idx, (chunk_content, embedding_vec) in enumerate(zip(chunks, embedding_vecs)):
            embedding_str = format_embedding_vector(embedding_vec)
            insert_buffer.append((
                doc_id,          # document_id
                idx,             # chunk_index
                chunk_content,   # content
                embedding_str,   # embedding (pgvector string)
                None,            # metadata JSONB
                datetime.now(timezone.utc),  # created_at
            ))

        # Flush buffer when it reaches batch size
        if len(insert_buffer) >= BATCH_SIZE:
            _flush_batch(cur, insert_buffer)
            conn.commit()
            total_chunks_inserted += len(insert_buffer)
            log.info(
                "Progress: %d docs processed (%d/%d rulings+cases), %d chunks inserted so far. "
                "Elapsed: %.1fs",
                total_docs_processed, total_docs_processed, len(rows),
                total_chunks_inserted, time.time() - start_time,
            )
            insert_buffer = []

        # Log per-document progress less frequently
        if total_docs_processed % 100 == 0 and not insert_buffer:
            log.info(
                "Docs processed: %d/%d, Chunks inserted: %d, API calls: %d, Elapsed: %.1fs",
                total_docs_processed, len(rows),
                total_chunks_inserted, total_api_calls,
                time.time() - start_time,
            )

    # Flush remaining buffer
    if insert_buffer:
        _flush_batch(cur, insert_buffer)
        conn.commit()
        total_chunks_inserted += len(insert_buffer)

    elapsed = time.time() - start_time
    log.info(
        "=== DONE === Processed %d documents, inserted %d chunks in %.1fs (%d API calls).",
        total_docs_processed, total_chunks_inserted, elapsed, total_api_calls,
    )

    # Step 3: Verify
    log.info("Verifying chunk counts...")
    cur.execute("""
        SELECT doc_type, COUNT(*)
        FROM documents
        WHERE (doc_type = 'ruling' OR doc_type = 'case')
          AND id IN (SELECT document_id FROM chunks)
        GROUP BY doc_type
    """)
    verified = cur.fetchall()
    for dt, cnt in verified:
        log.info("  %s: %d documents have chunks", dt, cnt)

    cur.execute("""
        SELECT COUNT(*) FROM documents
        WHERE (doc_type = 'ruling' OR doc_type = 'case')
          AND id NOT IN (SELECT document_id FROM chunks)
    """)
    remaining = cur.fetchone()[0]
    log.info("  Documents still missing chunks: %d", remaining)

    cur.execute("""
        SELECT COUNT(*) FROM chunks
    """)
    total_chunk_count = cur.fetchone()[0]
    log.info("  Total chunks in table: %d", total_chunk_count)

    cur.close()
    conn.close()
    log.info("Done.")


def _flush_batch(cur, buffer):
    """Flush a batch of chunk rows into the chunks table using execute_values."""
    # Build rows: for each buffer entry, we need 7 values matching the 7 %s in template
    values_list = []
    for doc_id, chunk_idx, content, embedding_str, metadata, created_at in buffer:
        values_list.append((
            doc_id,
            chunk_idx,
            content,
            embedding_str,
            metadata,
            created_at,
            content,  # for to_tsvector('english', %s)
        ))
    execute_values(
        cur,
        """
        INSERT INTO chunks (id, document_id, chunk_index, content, embedding, metadata, created_at, search_vector)
        VALUES %s
        """,
        values_list,
        template="(gen_random_uuid(), %s, %s, %s, %s::vector, %s, %s, to_tsvector('english', %s))",
    )


if __name__ == "__main__":
    main()