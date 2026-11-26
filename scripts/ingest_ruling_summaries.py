#!/usr/bin/env python3
"""Ingest AI ruling summaries into existing DB documents via batch docker exec.

Reads summary JSON files from data/rulings/summaries/ and updates the
matching ruling document's metadata with AI-generated fields.

Batches UPDATEs into single SQL scripts for performance.

Usage:
  python3 scripts/ingest_ruling_summaries.py                    # all summaries
  python3 scripts/ingest_ruling_summaries.py --dry-run           # count only
  python3 scripts/ingest_ruling_summaries.py --type aid          # ATO IDs only
  python3 scripts/ingest_ruling_summaries.py --type full         # Full rulings only
  python3 scripts/ingest_ruling_summaries.py --batch-size 500    # tune batch size
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Unbuffered output for real-time cron monitoring
os.environ.setdefault("PYTHONUNBUFFERED", "1")

SUMMARIES_DIR = Path(__file__).resolve().parent.parent / "data" / "rulings" / "summaries"


def sql_script(script: str, db: str = "cadena_knowledge", timeout: int = 60) -> bool:
    """Run a SQL script via docker exec with temp file. Returns True on success."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
        f.write(script)
        tmp_path = f.name
    try:
        r = subprocess.run(
            ["docker", "cp", tmp_path, "cadena-postgres:/tmp/ingest_batch.sql"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            print(f"  docker cp error: {r.stderr[:200]}", file=sys.stderr)
            return False
        r = subprocess.run(
            ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres",
             "-d", db, "-q", "-v", "ON_ERROR_STOP=1", "-f", "/tmp/ingest_batch.sql"],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            print(f"  SQL batch error: {r.stderr[:300]}", file=sys.stderr)
            return False
        return True
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        subprocess.run(
            ["docker", "exec", "cadena-postgres", "rm", "-f", "/tmp/ingest_batch.sql"],
            capture_output=True, timeout=5,
        )


def sql_query(query: str, db: str = "cadena_knowledge", timeout: int = 30) -> list[list[str]]:
    """Run a SQL query via docker exec psql. Returns rows."""
    SEP = "¶"
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", db,
         "-t", "-F", SEP, "-A", "-c", query],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.split("\n"):
        line = line.strip()
        if line:
            rows.append(line.split(SEP))
    return rows


# ── Filename → DB reference ──────────────────────────────────────────────────

def filename_to_ref(stem: str) -> str | None:
    """Convert summary filename stem to DB reference."""
    if stem.startswith("PS_LA_"):
        parts = stem.split("_", 3)
        if len(parts) >= 4:
            return f"PS LA {parts[2]}/{parts[3]}"
    elif stem.startswith("AID_"):
        parts = stem.split("_", 2)
        if len(parts) >= 3:
            rest = parts[1] + "_" + parts[2]
            m = re.match(r"(\d{4})_(\d+)", rest)
            if m:
                return f"AID_{m.group(1)}_{m.group(2)}"
    else:
        first = stem.split("_", 1)
        prefix = first[0]
        rest = stem[len(prefix)+1:]
        if prefix == "AID":
            return stem
        m = re.match(r"(\d{4})_(\d+)$", rest)
        if m:
            return f"{prefix} {m.group(1)}/{m.group(2)}"
        if rest.isdigit():
            return f"{prefix} {rest}"
    return None


# ── Field lists ──────────────────────────────────────────────────────────────

FULL_FIELDS = ["subject", "question", "background", "ruling", "notice",
               "date_of_effect", "citation"]
AID_FIELDS = ["subject", "question", "notice"]
LIST_FIELDS = ["cases_referenced", "legislation_referenced", "related_rulings"]


def build_metadata_update(data: dict, is_aid: bool) -> dict:
    meta = {"ai_summary": True, "summary_source": "deepseek_v4"}
    fields = AID_FIELDS if is_aid else FULL_FIELDS
    for f in fields:
        val = data.get(f)
        if val is not None and val != "":
            meta[f] = val
    for f in LIST_FIELDS:
        val = data.get(f)
        if val is not None and len(val) > 0:
            meta[f] = val
    return meta


def esc(s: str) -> str:
    """Escape for SQL single-quoted string. Only ' needs escaping for psql -f input."""
    return s.replace("'", "''")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ingest AI ruling summaries")
    parser.add_argument("--type", choices=["aid", "full", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    # Collect files
    if args.type in ("aid", "all"):
        aid_files = sorted(SUMMARIES_DIR.glob("AID_*.json"))
    else:
        aid_files = []
    if args.type in ("full", "all"):
        full_files = sorted(
            f for f in SUMMARIES_DIR.glob("*.json")
            if not f.name.startswith("AID_")
        )
    else:
        full_files = []
    all_files = aid_files + full_files
    total = len(all_files)

    print(f"Total summary files: {total} "
          f"({len(aid_files)} AID, {len(full_files)} full)")
    if args.dry_run:
        print("DRY RUN — no DB changes")

    # Load existing references
    print("Loading existing DB references...")
    existing_rows = sql_query(
        "SELECT reference FROM documents WHERE doc_type='ruling'", timeout=60
    )
    existing_refs = set()
    for row in existing_rows:
        if row:
            existing_refs.add(row[0].strip())
    print(f"Existing ruling documents in DB: {len(existing_refs)}")

    # Preprocess all files: build update batches
    updates = []  # list of (ref, meta_json)
    not_found = []
    skipped = 0

    for f in all_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue

        ref = filename_to_ref(f.stem)
        if not ref:
            skipped += 1
            continue

        if ref not in existing_refs:
            not_found.append(ref)
            continue

        meta = build_metadata_update(data, f.name.startswith("AID_"))
        if len(meta) > 1:  # more than just ai_summary
            updates.append((ref, json.dumps(meta)))

    total_matched = len(updates)
    total_not_found = len(not_found)
    total_skipped = skipped

    print(f"\nMatched: {total_matched}  Not found: {total_not_found}  Skipped: {total_skipped}")

    if total_not_found > 0:
        nf_by_type = {}
        for r in not_found:
            prefix = r.split(" ")[0].split("_")[0]
            nf_by_type[prefix] = nf_by_type.get(prefix, 0) + 1
        print(f"  Not found breakdown: {nf_by_type}")
        print(f"  Sample: {not_found[:5]}")

    if args.dry_run or not updates:
        return

    # Batch updates
    n_batches = (len(updates) + args.batch_size - 1) // args.batch_size
    t0 = time.time()
    total_updated = 0

    for batch_idx in range(n_batches):
        start = batch_idx * args.batch_size
        end = min(start + args.batch_size, len(updates))
        batch = updates[start:end]

        # Build a single SQL script with all UPDATEs
        sql_parts = []
        for ref, meta_json in batch:
            sql_parts.append(
                f"UPDATE documents SET metadata = metadata || '{esc(meta_json)}'::jsonb "
                f"WHERE reference = '{esc(ref)}';"
            )

        script = "BEGIN;\n" + "\n".join(sql_parts) + "\nCOMMIT;"
        ok = sql_script(script, timeout=60)
        if ok:
            total_updated += len(batch)
        else:
            print(f"  Batch {batch_idx + 1}/{n_batches} FAILED — retrying one-by-one")
            for ref, meta_json in batch:
                single = (
                    f"UPDATE documents SET metadata = metadata || '{esc(meta_json)}'::jsonb "
                    f"WHERE reference = '{esc(ref)}';"
                )
                if sql_script(single, timeout=10):
                    total_updated += 1

        elapsed = time.time() - t0
        rate = total_updated / elapsed if elapsed > 0 else 0
        pct = (batch_idx + 1) / n_batches * 100
        print(f"  Batch {batch_idx + 1}/{n_batches} ({pct:.0f}%) — "
              f"{total_updated}/{total_matched} updated @ {rate:.0f}/s")

    elapsed = time.time() - t0
    print(f"\n=== Results ({elapsed:.0f}s) ===")
    print(f"Total matched: {total_matched}")
    print(f"Total updated: {total_updated}")
    print(f"Not found:     {total_not_found}")
    print(f"Skipped:       {total_skipped}")

    # Verify
    verified = sql_query(
        "SELECT COUNT(*) FROM documents WHERE doc_type='ruling' AND metadata ? 'ai_summary'"
    )
    count = int(verified[0][0]) if verified else 0
    print(f"\nVerified: {count} documents now have ai_summary metadata")


if __name__ == "__main__":
    main()