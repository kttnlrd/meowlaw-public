#!/usr/bin/env python3
"""Build the vector embedding database (data/embeddings.db) for legislation-explorer.

Walks section markdown files under data/{act}/sections/ and commentary
markdown files under data/master-*-guide/sections/, chunks and embeds them
with BAAI/bge-small-en-v1.5, and writes the results to SQLite. Incremental:
unchanged text (by hash) is not re-embedded, and rows for files that no
longer exist are pruned.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sentence_transformers import SentenceTransformer  # noqa: E402

from backend.config import DATA_DIR  # noqa: E402
from backend.services.data_loader import (  # noqa: E402
    load_definitions,
    get_commentary_for_section,
    get_cases_for_section,
    get_smartlinks_for_item,
)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
EMBED_BATCH_SIZE = 64
MAX_CHARS = 1800
OUT_DB = DATA_DIR / "embeddings.db"

# Sections that are dictionaries of defined terms rather than prose, per act.
DICTIONARY_SECTIONS = {"995-1", "6", "195-1"}

# This pipeline produces LOCAL BAAI/bge-small-en-v1.5 vectors (384 dims). The API's vector
# search queries data/embeddings.db with text-embedding-3-small (1536 dims) via
# scripts/openai_embed.py. Two pipelines, one table, one column - and this INSERT never wrote
# the `model` column, so a run of this script against the production table left it holding two
# incompatible vector spaces that looked identical in the data. Refuse instead.
DIMS = 384


def assert_db_is_this_pipeline(conn: sqlite3.Connection) -> None:
    """Refuse to write unless every row in the table was made by this same model."""
    try:
        rows = conn.execute(
            "SELECT length(embedding), model, count(*) FROM embeddings GROUP BY 1, 2"
        ).fetchall()
    except sqlite3.OperationalError:
        return                      # table not created yet - nothing to collide with
    foreign = [(ln, m, n) for ln, m, n in rows if ln != DIMS * 4]
    if foreign:
        ln, m, n = foreign[0]
        raise SystemExit(
            f"refusing to write: this table already holds {n:,} embedding(s) of {ln // 4} "
            f"dims (model={m!r}), and this pipeline produces {DIMS} dims "
            f"(BAAI/bge-small-en-v1.5). Writing here would put two vector spaces in one "
            f"column, and the API's vector search queries this table with "
            f"text-embedding-3-small/1536 dims - use scripts/openai_embed.py for that table, "
            f"or point OUT_DB at a separate file.")
    if rows:
        print(f"table is compatible: {sum(n for _l, _m, n in rows):,} row(s) of {DIMS} dims")


# ---------------------------------------------------------------------------
# Parsing / text prep
# ---------------------------------------------------------------------------

def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split a markdown file into its YAML-ish frontmatter dict and body text."""
    if not content.startswith("---"):
        return {}, content
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if not m:
        return {}, content
    fm: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm, content[m.end():]


def strip_markdown(text: str) -> str:
    """Strip markdown/HTML formatting down to plain text."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\*Last updated:.*?\*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*|\*", "", text)
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split text into chunks of at most max_chars, breaking at sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start, n = 0, len(text)
    while start < n:
        if n - start <= max_chars:
            chunks.append(text[start:].strip())
            break
        window = text[start:start + max_chars]
        boundary = max(window.rfind(". "), window.rfind(".\n"), window.rfind("\n"), window.rfind(": "))
        if boundary < max_chars * 0.5:
            boundary = max_chars - 1
        end = start + boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return [c for c in chunks if c]


def chunk_dictionary(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Chunk a defined-terms dictionary section, grouping whole definitions per chunk."""
    entries = [e.strip() for e in re.split(r"\n\s*\n", text.strip()) if e.strip()]
    chunks, current, current_len = [], [], 0
    for entry in entries:
        entry_len = len(entry) + 2
        if entry_len > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            chunks.extend(chunk_text(entry, max_chars))
            continue
        if current and current_len + entry_len > max_chars:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(entry)
        current_len += entry_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def build_embedding_text(fm: dict, body: str, enrichment: str = "") -> str:
    header = (
        f"[Title]: {fm.get('section_title') or fm.get('title', '')}\n"
        f"[Act]: {fm.get('act', '')}\n"
        f"[Section]: {fm.get('section', '')}"
    )
    parts = [header, body.strip()]
    if enrichment.strip():
        parts.append(enrichment.strip())
    return "\n\n".join(p for p in parts if p)


def embed_batch(texts: list[str], model: SentenceTransformer):
    return model.encode(texts, batch_size=EMBED_BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False)


# ---------------------------------------------------------------------------
# Enrichment (defined terms + cross-references)
# ---------------------------------------------------------------------------

TERM_REF_RE = re.compile(r"\*([a-z][a-z\-]*(?:\s[a-z][a-z\-]*){0,4})")


def build_enrichment(act: str, section: str, raw_body: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Returns (enrichment_text, cross_refs) where cross_refs is a list of
    (ref_type, ref_text, ref_target) tuples for the cross_references table."""
    refs: list[tuple[str, str, str]] = []

    defs = load_definitions(act)
    seen_terms: set[str] = set()
    for m in TERM_REF_RE.finditer(raw_body):
        term = m.group(1).strip()
        if term in seen_terms:
            continue
        info = defs.get(term)
        if info:
            seen_terms.add(term)
            refs.append(("defined_term", term, f"{act}#{info.get('section', '')}"))
        if len(seen_terms) >= 15:
            break

    for link in get_smartlinks_for_item("section", f"{act}#{section}"):
        refs.append(("smartlink", link.get("reason", ""), link.get("id", "")))

    for entry in get_commentary_for_section(act, section, limit=10):
        refs.append(("commentary", entry.get("heading_title", ""), entry.get("publication", "")))

    for case in get_cases_for_section(act, section, limit=10):
        refs.append(("case", case.get("title", ""), case.get("citation", "")))

    lines = []
    if seen_terms:
        lines.append("[Defined terms]: " + ", ".join(sorted(seen_terms)))
    smartlink_targets = [t for r, _, t in refs if r == "smartlink"][:10]
    if smartlink_targets:
        lines.append("[Related sections]: " + ", ".join(smartlink_targets))
    commentary_titles = [t for r, t, _ in refs if r == "commentary" and t][:5]
    if commentary_titles:
        lines.append("[Commentary]: " + ", ".join(commentary_titles))
    case_titles = [t for r, t, _ in refs if r == "case" and t][:5]
    if case_titles:
        lines.append("[Cases]: " + ", ".join(case_titles))

    return "\n".join(lines), refs


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
            UNIQUE(file_path, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_embeddings_act_section ON embeddings(act, section);
        CREATE INDEX IF NOT EXISTS idx_embeddings_file_path ON embeddings(file_path);

        CREATE TABLE IF NOT EXISTS cross_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            embedding_id INTEGER NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
            ref_type TEXT NOT NULL,
            ref_text TEXT,
            ref_target TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cross_references_embedding_id ON cross_references(embedding_id);
        """
    )
    conn.commit()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_section(
    act: str,
    section_path: Path,
    tree: dict,
    model: SentenceTransformer,
    conn: sqlite3.Connection,
    source_type: str = "section",
) -> int:
    """Embed one markdown file (section or commentary) and upsert its chunk rows.
    Returns the number of chunks (re-)embedded (0 if all were unchanged)."""
    content = section_path.read_text(encoding="utf-8")
    fm, raw_body = parse_frontmatter(content)
    section = fm.get("section", section_path.stem)
    section_title = fm.get("section_title") or fm.get("title", "")

    if source_type == "section":
        enrichment, refs = build_enrichment(act, section, raw_body)
    else:
        enrichment, refs = "", []

    body = strip_markdown(raw_body)
    if source_type == "section" and section in DICTIONARY_SECTIONS:
        chunks = chunk_dictionary(body)
    else:
        chunks = chunk_text(body)
    if not chunks:
        chunks = [""]

    file_path = str(section_path.relative_to(DATA_DIR))
    existing = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT chunk_index, text_hash, id FROM embeddings WHERE file_path = ?", (file_path,)
        ).fetchall()
    }

    to_embed_idx, to_embed_text, to_embed_hash = [], [], []
    for idx, chunk in enumerate(chunks):
        etext = build_embedding_text(fm, chunk, enrichment if idx == 0 else "")
        h = text_hash(etext)
        prev = existing.get(idx)
        if prev and prev[0] == h:
            continue
        to_embed_idx.append(idx)
        to_embed_text.append(etext)
        to_embed_hash.append(h)

    if to_embed_text:
        vectors = embed_batch(to_embed_text, model)
        for idx, etext, h, vec in zip(to_embed_idx, to_embed_text, to_embed_hash, vectors):
            conn.execute(
                """
                INSERT INTO embeddings (source_type, act, section, section_title, chunk_index,
                                         file_path, text_hash, embedding_text, embedding, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path, chunk_index) DO UPDATE SET
                    text_hash=excluded.text_hash,
                    embedding_text=excluded.embedding_text,
                    embedding=excluded.embedding,
                    section_title=excluded.section_title,
                    model=excluded.model
                """,
                (source_type, act, section, section_title, idx, file_path, h, etext,
                 vec.astype("float32").tobytes(), MODEL_NAME),
            )
            if idx == 0 and refs:
                row_id = conn.execute(
                    "SELECT id FROM embeddings WHERE file_path = ? AND chunk_index = 0", (file_path,)
                ).fetchone()[0]
                conn.execute("DELETE FROM cross_references WHERE embedding_id = ?", (row_id,))
                conn.executemany(
                    "INSERT INTO cross_references (embedding_id, ref_type, ref_text, ref_target) VALUES (?, ?, ?, ?)",
                    [(row_id, rt, txt, tgt) for rt, txt, tgt in refs],
                )
    conn.commit()
    return len(to_embed_text)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

LEGISLATION_ACTS = ["itaa-1997", "itaa-1936", "gst-1999", "taa-1953", "fbt-1986", "sis-1993",
                    "nz-it-2007",
                    # Ingested 2026-10-13; without an entry here their sections never reach
                    # the embeddings table and cannot be found by search.
                    "nz-taa-1994", "nz-gst-1985", "nz-egda-1968", "nz-scd-1971",
                    "nz-kiwisaver-2006", "nz-sl-2011"]
COMMENTARY_ACTS = ["master-tax-guide", "master-gst-guide"]


def _stale_rows(
    existing_files: set[str], seen_files: set[str], walked_roots: set[str]
) -> tuple[set[str], set[str]]:
    """Which stored row-file(s) this run may delete, and which were in scope at all.

    A file_path outside the directories walked by this run belongs to another producer (rulings,
    cases) and says nothing about whether this run saw it.  The original rule was
    `existing_files - seen_files`, which deleted every ruling and case row in the table - 260,297
    rows - as "stale" purely because those paths are not under data/*/sections.

    With nothing walked, nothing is in scope, so nothing can be deleted.
    """
    in_scope = {f for f in existing_files if any(f.startswith(root) for root in walked_roots)}
    return in_scope - seen_files, in_scope


def main() -> None:
    conn = sqlite3.connect(OUT_DB)
    init_db(conn)
    assert_db_is_this_pipeline(conn)

    print(f"Loading {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME)

    seen_files: set[str] = set()
    walked_roots: set[str] = set()
    total_embedded = 0

    for act in LEGISLATION_ACTS:
        act_dir = DATA_DIR / act
        sections_dir = act_dir / "sections"
        if not sections_dir.exists():
            continue
        tree = {}
        tree_path = act_dir / "tree.json"
        if tree_path.exists():
            import json
            tree = json.loads(tree_path.read_text(encoding="utf-8"))
        md_files = sorted(sections_dir.rglob("*.md"))
        print(f"{act}: {len(md_files)} section files")
        walked_roots.add(str(sections_dir.relative_to(DATA_DIR)) + "/")
        for i, path in enumerate(md_files, 1):
            seen_files.add(str(path.relative_to(DATA_DIR)))
            total_embedded += process_section(act, path, tree, model, conn, source_type="section")
            if i % 500 == 0:
                print(f"  {act}: {i}/{len(md_files)} processed")

    for act in COMMENTARY_ACTS:
        sections_dir = DATA_DIR / act / "sections"
        if not sections_dir.exists():
            continue
        md_files = sorted(sections_dir.rglob("*.md"))
        print(f"{act}: {len(md_files)} commentary files")
        walked_roots.add(str(sections_dir.relative_to(DATA_DIR)) + "/")
        for i, path in enumerate(md_files, 1):
            seen_files.add(str(path.relative_to(DATA_DIR)))
            total_embedded += process_section(act, path, {}, model, conn, source_type="commentary")
            if i % 500 == 0:
                print(f"  {act}: {i}/{len(md_files)} processed")

    # Only rows this run was actually responsible for may be deleted: a file_path outside the
    # directories walked above belongs to another producer (rulings, cases) and has nothing to do
    # with whether this run saw it.  The unscoped rule was `existing_files - seen_files`, which
    # deleted every ruling and case row in the table - 260,297 rows - as "stale", because those
    # paths simply are not under data/*/sections.  A producer must never delete another
    # producer's rows, and "not in my scope" is not "gone from disk".
    existing_files = {r[0] for r in conn.execute("SELECT DISTINCT file_path FROM embeddings").fetchall()}
    stale, in_scope = _stale_rows(existing_files, seen_files, walked_roots)
    if stale:
        print(f"removing {len(stale)} row-file(s) under {sorted(walked_roots)} whose file no longer exists")
        sample = sorted(stale)[:3]
        for s in sample:
            print(f"  e.g. {s}")
        conn.executemany("DELETE FROM embeddings WHERE file_path = ?", [(f,) for f in stale])
        conn.commit()
    out_of_scope = len(existing_files) - len(in_scope)
    if out_of_scope:
        print(f"left {out_of_scope:,} row-file(s) outside this pipeline's directories untouched "
              f"(other producers: rulings, cases)")

    row_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    print(f"Done. {total_embedded} chunks (re-)embedded, {len(stale)} stale files removed, {row_count} total rows.")
    conn.close()


if __name__ == "__main__":
    main()
