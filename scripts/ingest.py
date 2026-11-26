#!/usr/bin/env python3
"""Unified monthly content ingestion — scrape, embed, rebuild similarity index.

Usage:
  python3 scripts/ingest.py --type cases           # embed new case summaries
  python3 scripts/ingest.py --type rulings         # embed new rulings from DB
  python3 scripts/ingest.py --type sections        # re-scan legislation files
  python3 scripts/ingest.py --type commentary      # re-scan commentary files
  python3 scripts/ingest.py --all                  # run all types
  python3 scripts/ingest.py --rebuild-index        # rebuild similarity index only
  python3 scripts/ingest.py --dry-run              # show what would be done without embedding
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import DATA_DIR

OUT_DB = DATA_DIR / "embeddings.db"


def psql_json(query: str) -> list[dict]:
    """Run a PostgreSQL query returning JSON rows."""
    import json
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
         "-d", "cadena_knowledge", "-tA", "-c", query],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        print(f"  psql error: {r.stderr[:200]}")
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


def check_ruling_updates(dry_run: bool) -> int:
    """Check for new rulings since last ingest."""
    conn = sqlite3.connect(str(OUT_DB))
    try:
        existing = {
            r[0] for r in conn.execute(
                "SELECT section FROM embeddings WHERE source_type='ruling'"
            ).fetchall()
        }
    finally:
        conn.close()

    rows = psql_json("""SELECT json_build_object(
        'reference', d.reference,
        'title', d.title,
        'ai_summary', d.metadata->>'ai_summary'
    )
    FROM documents d
    JOIN rulings r ON r.document_id = d.id
    WHERE d.doc_type='ruling' AND d.metadata ? 'ai_summary'
    ORDER BY d.reference""")

    new = [r for r in rows if r.get("reference") not in existing]
    if dry_run:
        print(f"  {len(new)} new rulings out of {len(rows)} total")
        return len(new)

    if new:
        print(f"  {len(new)} new rulings to embed")
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "openai_embed.py"), "--type", "rulings"],
            cwd=str(PROJECT_ROOT), timeout=600,
        )
    return len(new)


def check_case_updates(dry_run: bool) -> int:
    """Check for new case summary files."""
    conn = sqlite3.connect(str(OUT_DB))
    try:
        existing = {
            r[0] for r in conn.execute(
                "SELECT file_path FROM embeddings WHERE source_type='case'"
            ).fetchall()
        }
    finally:
        conn.close()

    summary_dir = PROJECT_ROOT / "scripts" / "cleaned" / "summaries"
    if not summary_dir.exists():
        print(f"  Case summary dir not found: {summary_dir}")
        return 0

    new_files = [f for f in sorted(summary_dir.glob("*.json"))
                 if f"summaries/{f.name}" not in existing]

    if dry_run:
        print(f"  {len(new_files)} new case summaries out of {len(list(summary_dir.glob('*.json')))} total")
        return len(new_files)

    if new_files:
        print(f"  {len(new_files)} new case summaries to embed")
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "openai_embed.py"), "--type", "cases"],
            cwd=str(PROJECT_ROOT), timeout=600,
        )
    return len(new_files)


def check_section_updates(dry_run: bool) -> int:
    """Re-scan legislation and commentary files for changes."""
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "openai_embed.py")],
        cwd=str(PROJECT_ROOT), timeout=600,
    )
    return 0


def rebuild_index(dry_run: bool) -> int:
    """Rebuild the similarity index."""
    if dry_run:
        print("  Would rebuild similarity index")
        return 0
    print("  Rebuilding similarity index...")
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_similarity_index.py")],
        cwd=str(PROJECT_ROOT), timeout=600,
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description="Monthly content ingestion")
    parser.add_argument("--type", choices=["sections", "commentary", "rulings", "cases", "all"])
    parser.add_argument("--rebuild-index", action="store_true", help="Rebuild similarity index only")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    if args.rebuild_index:
        rebuild_index(args.dry_run)
        return

    types = ["sections", "rulings", "cases"] if args.type == "all" or not args.type else [args.type]

    for t in types:
        print(f"\n=== Checking: {t} ===")
        t0 = time.time()
        if t == "sections" or t == "commentary":
            n = check_section_updates(args.dry_run)
        elif t == "rulings":
            n = check_ruling_updates(args.dry_run)
        elif t == "cases":
            n = check_case_updates(args.dry_run)
        else:
            n = 0
        elapsed = time.time() - t0
        print(f"  Done: {n} items processed in {elapsed:.1f}s")

    # Always rebuild index after content changes
    if not args.dry_run:
        rebuild_index(False)


if __name__ == "__main__":
    main()
