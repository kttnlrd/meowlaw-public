#!/usr/bin/env python3
"""Unified embedding pipeline — OpenAI text-embedding-3-small.

Usage:
  python3 scripts/openai_embed.py                  # re-embed existing (sections + commentary)
  python3 scripts/openai_embed.py --type rulings   # embed rulings from DB
  python3 scripts/openai_embed.py --type cases     # embed cases from flat JSON files
  python3 scripts/openai_embed.py --all            # embed everything
  python3 scripts/openai_embed.py --limit 10       # dry-run first 10 items per type
  python3 scripts/openai_embed.py --clean          # rebuild DB from scratch
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from array import array
from pathlib import Path

# Load OpenAI API key from .hermes/.env
_hermes_env = Path(os.environ.get("HERMES_ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
if _hermes_env.exists():
    for _line in _hermes_env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            k, v = _line.strip().split("=", 1)
            if k == "OPENAI_API_KEY":
                os.environ.setdefault(k, v)

from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import DATA_DIR  # noqa: E402

# The embedding endpoint is pluggable so a gateway can carry the traffic. The model and DIMS
# must not move: existing vectors are text-embedding-3-small at 1536 dims.
MODEL = os.getenv("EMBED_MODEL", "").strip() or "text-embedding-3-small"
DIMS = 1536
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "").strip()
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "").strip()
BATCH_SIZE = 100
MAX_CHARS = 1800
OUT_DB = DATA_DIR / "embeddings.db"

# Sections that are dictionaries of defined terms rather than prose, per act.
DICTIONARY_SECTIONS = {"995-1", "6", "195-1"}

client = (OpenAI(api_key=EMBED_API_KEY or os.environ.get("OPENAI_API_KEY"), base_url=EMBED_BASE_URL)
          if EMBED_BASE_URL else OpenAI(api_key=EMBED_API_KEY or os.environ.get("OPENAI_API_KEY")))

# ---------------------------------------------------------------------------
# Parsing / text prep (reused from embed_legislation.py)
# ---------------------------------------------------------------------------

def parse_frontmatter(content: str) -> tuple[dict, str]:
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


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def ensure_model_column(conn: sqlite3.Connection):
    """Add model column if it doesn't exist."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)").fetchall()}
    if "model" not in cols:
        conn.execute("ALTER TABLE embeddings ADD COLUMN model TEXT DEFAULT 'text-embedding-3-small'")
        conn.commit()


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


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via OpenAI API."""
    if not texts:
        return []
    resp = client.embeddings.create(
        model=MODEL,
        input=texts,
        dimensions=DIMS,
    )
    return [r.embedding for r in resp.data]


# ---------------------------------------------------------------------------
# Section processing (legislation + commentary)
# ---------------------------------------------------------------------------

TERM_REF_RE = re.compile(r"\*([a-z][a-z\-]*(?:\s[a-z][a-z\-]*){0,4})")

def build_section_embedding_text(fm: dict, body: str, enrichment: str = "") -> str:
    header = (
        f"[Title]: {fm.get('section_title') or fm.get('title', '')}\n"
        f"[Act]: {fm.get('act', '')}\n"
        f"[Section]: {fm.get('section', '')}"
    )
    parts = [header, body.strip()]
    if enrichment.strip():
        parts.append(enrichment.strip())
    return "\n\n".join(p for p in parts if p)


def build_commentary_embedding_text(fm: dict, body: str) -> str:
    title = fm.get("section_title") or fm.get("title", "")
    para = fm.get("paragraph_number", "")
    guide = fm.get("act", "")
    return f"[Guide]: {guide}\n[Paragraph]: {para}\n[Title]: {title}\n\n{body}"


def process_section_file(conn, act, path, source_type):
    """Process one legislation or commentary markdown file. Returns chunks count."""
    content = path.read_text(encoding="utf-8")
    fm, raw_body = parse_frontmatter(content)
    section = fm.get("section", path.stem)
    section_title = fm.get("section_title") or fm.get("title", "")
    body = strip_markdown(raw_body)

    if source_type == "section":
        # Simple enrichment for sections (defined terms from body only)
        seen_terms = set()
        for m in TERM_REF_RE.finditer(raw_body):
            term = m.group(1).strip()
            if term in seen_terms:
                continue
            seen_terms.add(term)
            if len(seen_terms) >= 15:
                break
        enrichment = f"[Defined terms]: {', '.join(sorted(seen_terms))}" if seen_terms else ""
    else:
        enrichment = ""

    if source_type == "section" and section in DICTIONARY_SECTIONS:
        chunks = chunk_dictionary(body)
    else:
        chunks = chunk_text(body)
    if not chunks:
        chunks = [""]

    file_path = str(path.relative_to(DATA_DIR))
    existing = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT chunk_index, text_hash FROM embeddings WHERE file_path = ?", (file_path,)
        ).fetchall()
    }

    to_embed, texts, hashes = [], [], []
    for idx, chunk in enumerate(chunks):
        if source_type == "section":
            etext = build_section_embedding_text(fm, chunk, enrichment if idx == 0 else "")
        else:
            etext = build_commentary_embedding_text(fm, chunk)
        h = text_hash(etext)
        prev = existing.get(idx)
        if prev and prev == h:
            continue
        to_embed.append(idx)
        texts.append(etext)
        hashes.append(h)

    if texts:
        vectors = embed_batch(texts)
        for idx, etext, h, vec in zip(to_embed, texts, hashes, vectors):
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
                (source_type, act, section, section_title, idx,
                 file_path, h, etext, array('f', vec).tobytes(), MODEL),
            )

    # Prune stale chunk indexes for this file (corpus cleanup can shrink a
    # file's chunk count; orphaned rows would linger with old noise/text).
    current_idxs = set(range(len(chunks)))
    if existing:
        stale = sorted(set(existing) - current_idxs)
        if stale:
            marks = ",".join("?" * len(stale))
            conn.execute(
                f"DELETE FROM embeddings WHERE file_path = ? AND chunk_index IN ({marks})",
                [file_path] + stale,
            )
    conn.commit()
    return len(to_embed)


# ---------------------------------------------------------------------------
# Ruling processing (from PostgreSQL)
# ---------------------------------------------------------------------------

def psql(query: str) -> list[dict]:
    """Run a query on cadena_knowledge via docker exec psql. Returns list of dicts."""
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
         "-d", "cadena_knowledge", "-tA", "-F", "\x01", "-c", query],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        print(f"psql error: {r.stderr[:200]}")
        return []
    lines = [ln for ln in r.stdout.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    # Parse the header row to get column names, then map
    # Unfortunately -tA with -F doesn't give headers easily, so we'll use JSON output
    return r.stdout.strip().split("\n")


def psql_json(query: str) -> list[dict]:
    """Run a query returning JSON rows."""
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
         "-d", "cadena_knowledge", "-tA", "-c", query],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        print(f"psql error: {r.stderr[:200]}")
        return []
    rows = []
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def process_rulings(conn, limit=None):
    """Embed rulings from PostgreSQL ai_summary + refs."""
    q = """SELECT json_build_object(
        'reference', d.reference,
        'title', d.title,
        'ai_summary', d.metadata->>'ai_summary',
        'related_provisions', COALESCE(r.related_provisions::text, '[]'),
        'cases_referenced', COALESCE(d.metadata->>'cases_referenced', '[]'),
        'legislation_referenced', COALESCE(d.metadata->>'legislation_referenced', '[]'),
        'related_rulings', COALESCE(d.metadata->>'related_rulings', '[]')
    )
    FROM documents d
    JOIN rulings r ON r.document_id = d.id
    WHERE d.doc_type = 'ruling' AND d.metadata ? 'ai_summary'
    ORDER BY d.reference"""
    if limit:
        q += f" LIMIT {limit}"
    rows = psql_json(q)
    print(f"  {len(rows)} rulings with ai_summary")

    # Ensure table has model column
    ensure_model_column(conn)

    count = 0
    for row in rows:
        ref = row.get("reference", "")
        title = row.get("title", ref)
        summary = row.get("ai_summary", "")
        if not summary:
            continue

        # Parse references
        try:
            provisions = json.loads(row.get("related_provisions", "[]"))
        except (json.JSONDecodeError, TypeError):
            provisions = []
        try:
            cases = json.loads(row.get("cases_referenced", "[]"))
        except (json.JSONDecodeError, TypeError):
            cases = []
        try:
            leg_refs = json.loads(row.get("legislation_referenced", "[]"))
        except (json.JSONDecodeError, TypeError):
            leg_refs = []
        try:
            related_rulings = json.loads(row.get("related_rulings", "[]"))
        except (json.JSONDecodeError, TypeError):
            related_rulings = []

        refs_str = ""
        all_refs = []
        if provisions:
            all_refs.extend(provisions[:5])
        if cases:
            all_refs.extend(cases[:5])
        if leg_refs:
            all_refs.extend(leg_refs[:5])
        if related_rulings:
            all_refs.extend(related_rulings[:5])
        if all_refs:
            refs_str = "\n[References] " + ", ".join(all_refs)

        etext = f"[Ruling] {ref}\n[Title] {title}\n[Summary] {summary}{refs_str}"
        h = text_hash(etext)
        fpath = f"rulings/{ref}.json"

        existing = conn.execute(
            "SELECT text_hash FROM embeddings WHERE source_type='ruling' AND file_path=? AND chunk_index=0",
            (fpath,)
        ).fetchone()
        if existing and existing[0] == h:
            continue

        vec = embed_batch([etext])[0]
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
                ("ruling", "ato", ref, title, 0,
                 fpath, h, etext, array('f', vec).tobytes(), MODEL),
        )
        count += 1
        if count % 200 == 0:
            conn.commit()
            print(f"  {count} rulings embedded")
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Case processing (from flat JSON summaries)
# ---------------------------------------------------------------------------

CASE_SUMMARY_DIR = PROJECT_ROOT / "scripts" / "cleaned" / "summaries"


def process_cases(conn, limit=None):
    """Embed case summaries from flat JSON files."""
    if not CASE_SUMMARY_DIR.exists():
        print(f"  Case summary dir not found: {CASE_SUMMARY_DIR}")
        return 0

    files = sorted(CASE_SUMMARY_DIR.glob("*.json"))
    if limit:
        files = files[:limit]
    print(f"  {len(files)} case summary files")

    ensure_model_column(conn)
    count = 0

    for fpath in files:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            continue

        citation = data.get("citation", fpath.stem)
        case_name = data.get("case_name", "")
        court = data.get("court", "")
        facts = (data.get("facts") or "")[:2000]
        held = (data.get("held") or "")[:1000]
        reasoning = (data.get("reasoning") or "")[:2000]
        outcome = (data.get("outcome") or "")[:500]
        cases_cited = data.get("cases_cited", [])
        legislation_cited = data.get("legislation_cited", [])

        refs_str = ""
        all_refs = []
        for c in (cases_cited or []):
            m = re.match(r"^\[([^\]]+)\]\s+([^\d]+)\s+(.*)", str(c)[:100])
            if m:
                all_refs.append(m.group(0)[:80])
            else:
                all_refs.append(str(c)[:80])
        for l in (legislation_cited or []):
            all_refs.append(str(l)[:80])
        if len(all_refs) > 20:
            all_refs = all_refs[:20]
        if all_refs:
            refs_str = "\n[References] " + ", ".join(all_refs)

        parts = [f"[Case] {citation}"]
        if case_name:
            parts.append(f"[Title] {case_name}")
        if court:
            parts.append(f"{{Court}} {court}")
        if facts:
            parts.append(f"{{Facts}} {facts}")
        if held:
            parts.append(f"{{Held}} {held}")
        if reasoning:
            parts.append(f"{{Reasoning}} {reasoning}")
        if outcome:
            parts.append(f"{{Outcome}} {outcome}")
        if refs_str:
            parts.append(refs_str)
        etext = "\n".join(parts)
        h = text_hash(etext)
        rel_path = f"summaries/{fpath.name}"

        existing = conn.execute(
            "SELECT text_hash FROM embeddings WHERE source_type='case' AND file_path=? AND chunk_index=0",
            (rel_path,)
        ).fetchone()
        if existing and existing[0] == h:
            continue

        vec = embed_batch([etext])[0]
        section_id = citation.replace("[", "").replace("]", "").replace(" ", "_")[:100]
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
            ("case", court or "unknown", section_id, case_name or citation, 0,
             rel_path, h, etext, array('f', vec).tobytes(), MODEL),
        )
        count += 1
        if count % 200 == 0:
            conn.commit()
            print(f"  {count} cases embedded")

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

from backend.doc_ids import STATE_TAX_ACTS  # noqa: E402

# Acts whose sections are embedded are DISCOVERED from the corpus, not listed. The old hardcoded
# list silently skipped 15 of 46 acts (12,802 sections, e.g. corporations-act-2001 and
# competition-consumer-2010), and vector search cannot return what was never embedded. The
# exclusions are the browse-only surfaces that are not legislation.
EMBED_EXCLUDED = {
    "db-snapshots", "map_reviews", "nz_rulings", "nz_rulings_pdf", "nz_rulings_list",
    "proposed-law", "spec", "maps", "rulings", "private_rulings", "regulatory_guides",
    "afsa_guides", "treaties", "cases", "definitions", "master-tax-guide",
    "master-gst-guide", "master-tax-examples", "nz-master-tax-guide",
}


def discover_legislation_acts() -> list[str]:
    """Every corpus directory that holds sections, i.e. everything search must be able to find."""
    found = []
    for tree in sorted(DATA_DIR.glob("*/sections")):
        act = tree.parent.name
        if act in EMBED_EXCLUDED or not any(tree.rglob("*.md")):
            continue
        found.append(act)
    return found


LEGISLATION_ACTS = discover_legislation_acts()
COMMENTARY_ACTS = []  # CCH commentary dropped in the MeowLaw fork


def walk_corpus_files(acts: list[str] | None = None) -> set[str]:
    """The file paths this pipeline owns, exactly as embed_sections_and_commentary sees them.

    Shared with --prune-stale so the deletion half and the embedding half can never disagree
    about what "still exists" means.
    """
    seen: set[str] = set()
    for act in acts or (LEGISLATION_ACTS + COMMENTARY_ACTS):
        sections_dir = DATA_DIR / act / "sections"
        if not sections_dir.exists():
            continue
        for path in sections_dir.rglob("*.md"):
            seen.add(str(path.relative_to(DATA_DIR)))
    return seen


def prune_stale_rows(conn, commit: bool, acts: list[str] | None = None) -> list[str]:
    """Delete section/commentary rows whose file no longer exists - this pipeline's own rows.

    Scoped twice, because the first cut of this sweep deleted 260,297 ruling and case rows by
    reading "not in my scope" as "gone": the candidate set is restricted to the acts this
    pipeline walks (`walk_corpus_files`) and the row set to source_type in
    ('section','commentary'). A row for another producer, or for an act that was not walked, is
    never a candidate.
    """
    owned_acts = set(acts or (LEGISLATION_ACTS + COMMENTARY_ACTS))
    seen_files = walk_corpus_files(acts)
    existing = {r[0] for r in conn.execute(
        "SELECT DISTINCT file_path FROM embeddings WHERE source_type IN ('section','commentary')")}
    stale = {f for f in existing - seen_files if f.split("/", 1)[0] in owned_acts}
    for f in sorted(stale):
        print(f"  stale: {f}")
    if commit and stale:
        conn.executemany("DELETE FROM embeddings WHERE file_path = ?", [(f,) for f in stale])
        conn.commit()
    return sorted(stale)


def embed_sections_and_commentary(conn, limit=None, acts: list[str] | None = None):
    """Embed all legislation sections and commentary files (or only `acts`)."""
    seen_files = set()
    total = 0

    for act in acts or LEGISLATION_ACTS:
        act_dir = DATA_DIR / act
        sections_dir = act_dir / "sections"
        if not sections_dir.exists():
            continue
        md_files = sorted(sections_dir.rglob("*.md"))
        if limit:
            md_files = md_files[:limit]
        print(f"  {act}: {len(md_files)} section files")
        for i, path in enumerate(md_files, 1):
            seen_files.add(str(path.relative_to(DATA_DIR)))
            total += process_section_file(conn, act, path, "section")
            if i % 500 == 0:
                print(f"    {i}/{len(md_files)} processed, {total} chunks so far")

    for act in ([] if acts else COMMENTARY_ACTS):
        sections_dir = DATA_DIR / act / "sections"
        if not sections_dir.exists():
            continue
        md_files = sorted(sections_dir.rglob("*.md"))
        if limit:
            md_files = md_files[:limit]
        print(f"  {act}: {len(md_files)} commentary files")
        for i, path in enumerate(md_files, 1):
            seen_files.add(str(path.relative_to(DATA_DIR)))
            total += process_section_file(conn, act, path, "commentary")
            if i % 500 == 0:
                print(f"    {i}/{len(md_files)} processed, {total} chunks so far")

    # Remove stale files
    stale = prune_stale_rows(conn, commit=True, acts=acts)
    if stale:
        print(f"  pruned {len(stale)} stale file(s)")

    return total


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Unified embedding pipeline")
    parser.add_argument("--type", choices=["sections", "commentary", "rulings", "cases", "all"])
    parser.add_argument("--limit", type=int, help="Limit items per type (for testing)")
    parser.add_argument("--clean", action="store_true", help="Rebuild DB from scratch")
    parser.add_argument("--prune", action="store_true",
                        help="Delete stale section/commentary rows only (no embedding)")
    parser.add_argument("--acts", help="comma list: embed (and prune) only these legislation acts")
    args = parser.parse_args()
    only = [a.strip() for a in args.acts.split(",") if a.strip()] if args.acts else None
    if only and set(only) - set(LEGISLATION_ACTS):
        raise SystemExit(f"--acts not in LEGISLATION_ACTS: {sorted(set(only) - set(LEGISLATION_ACTS))}")

    if args.clean and OUT_DB.exists():
        OUT_DB.unlink()
        print("Removed existing embeddings.db")

    conn = sqlite3.connect(str(OUT_DB))
    init_db(conn)

    if args.prune:
        stale = prune_stale_rows(conn, commit=True)
        print(f"pruned {len(stale)} stale section/commentary file(s)")
        conn.close()
        return

    types = ["sections", "commentary", "rulings", "cases"] if args.type == "all" or not args.type else [args.type]
    # If no --type, default: sections + commentary
    if not args.type:
        types = ["sections", "commentary"]

    for t in types:
        print(f"\n=== Processing: {t} ===")
        t0 = time.time()
        if t in ("sections", "commentary"):
            n = embed_sections_and_commentary(conn, args.limit, only)
        elif t == "rulings":
            n = process_rulings(conn, args.limit)
        elif t == "cases":
            n = process_cases(conn, args.limit)
        elapsed = time.time() - t0
        print(f"  Done: {n} items in {elapsed:.1f}s")

    total = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    print(f"\nTotal embeddings: {total}")
    by_type = conn.execute("SELECT source_type, COUNT(*) FROM embeddings GROUP BY source_type").fetchall()
    for t, c in by_type:
        print(f"  {t}: {c}")
    conn.close()


if __name__ == "__main__":
    main()
