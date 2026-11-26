#!/usr/bin/env python3
"""CDN-0184/CDN-0185: re-extract legislation_refs for affected cases.

Affected = cases having (a) the same section under >1 act (act-multiplication
bug) or (b) a truncated s.N row whose context contains N-NNN (hyphen
truncation). For each, re-run the scoped extractor over the document content,
DELETE old rows, INSERT new rows — per case inside a transaction.

Usage:
    python3 scripts/re_extract_leg_refs.py --dry-run   # report only
    python3 scripts/re_extract_leg_refs.py --limit 50  # first N cases
    python3 scripts/re_extract_leg_refs.py             # all affected
"""
from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from backfill_legislation_refs import ACTS, extract_refs, is_chrome

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("re_extract_leg_refs")

SEP = "¶"


def sql(query: str, timeout: int = 60) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", "cadena_knowledge",
         "-t", "-F", SEP, "-A", "-c", query],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        log.error("SQL error: %s", r.stderr[:300])
        return []
    rows = []
    for line in r.stdout.split("\n"):
        line = line.strip()
        if line:
            parts = line.split(SEP)
            if parts and parts[0].strip():
                rows.append(parts)
    return rows


def quote(s: str) -> str:
    return s.replace("'", "''")


def affected_cases() -> list[tuple[str, str]]:
    """(case_id, citation) for cases with dup-across-act or truncation rows."""
    q = """
    WITH dup_cases AS (
      SELECT DISTINCT case_id FROM (
        SELECT case_id, paragraph_number, section_reference
        FROM case_legislation_refs
        GROUP BY case_id, paragraph_number, section_reference
        HAVING COUNT(DISTINCT act_title) > 1
      ) d
    ),
    trunc_cases AS (
      SELECT DISTINCT case_id FROM case_legislation_refs
      WHERE section_reference ~ '^s\\.\\d+$' AND context ~ '\\d+-\\d+'
    )
    SELECT c.id, c.citation
    FROM cases c
    JOIN (SELECT case_id FROM dup_cases UNION SELECT case_id FROM trunc_cases) u
      ON u.case_id = c.id
    WHERE EXISTS (SELECT 1 FROM documents d WHERE d.id = c.document_id
                  AND LENGTH(COALESCE(d.content, '')) > 100)
    ORDER BY c.citation;
    """
    return [(r[0], r[1]) for r in sql(q)]


def get_content(case_id: str) -> str:
    """Fetch full document content raw — must NOT go through sql() which
    splits stdout on newlines (content is multi-line HTML)."""
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", "cadena_knowledge",
         "-t", "-A", "-c",
         f"SELECT content::text FROM documents WHERE id = "
         f"(SELECT document_id FROM cases WHERE id = '{case_id}') LIMIT 1;"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    return r.stdout.strip()


def count_rows(case_id: str) -> int:
    rows = sql(f"SELECT COUNT(*) FROM case_legislation_refs WHERE case_id = '{case_id}';")
    return int(rows[0][0]) if rows else 0


def replace_case_refs(case_id: str, citation: str) -> tuple[int, int]:
    """Delete + reinsert refs for one case inside a transaction. (old, new)."""
    content = get_content(case_id)
    old_n = count_rows(case_id)
    refs = extract_refs(content, citation)
    if not refs and old_n == 0:
        return (0, 0)
    # Build insert VALUES
    vals = []
    for ref in refs:
        act = quote(ref["act_title"])
        sec = quote(ref["section_reference"])
        ctx = quote(ref["context"] or "")
        para = ref.get("paragraph_number") or 0
        vals.append(f"('{case_id}', '{act}', '{sec}', '{ctx}', {para})")
    ins_sql = "INSERT INTO case_legislation_refs (case_id, act_title, section_reference, context, paragraph_number) VALUES "
    tx = [
        "BEGIN;",
        f"DELETE FROM case_legislation_refs WHERE case_id = '{case_id}';",
    ]
    if vals:
        # insert in chunks of 200
        for i in range(0, len(vals), 200):
            tx.append(ins_sql + ",".join(vals[i:i + 200]) + ";")
    tx.append("COMMIT;")
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", "cadena_knowledge",
         "-v", "ON_ERROR_STOP=1", "-c", " ".join(tx)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        log.error("  %s: TX FAILED: %s", citation, r.stderr[:200])
        sql("ROLLBACK;")
        return (-1, -1)
    new_n = count_rows(case_id)
    return (old_n, new_n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cases = affected_cases()
    if args.limit:
        cases = cases[:args.limit]
    log.info("Affected cases: %d%s", len(cases), f" (limiting to {args.limit})" if args.limit else "")

    if args.dry_run:
        # quick per-case peek: current dup/trunc row count
        for cid, cit in cases[:20]:
            dup = sql(f"SELECT COUNT(*) FROM (SELECT case_id, paragraph_number, section_reference "
                      f"FROM case_legislation_refs WHERE case_id='{cid}' "
                      f"GROUP BY case_id, paragraph_number, section_reference "
                      f"HAVING COUNT(DISTINCT act_title)>1) x;")
            print(f"  {cit}: dup_rows={dup[0][0] if dup else 0}")
        return 0

    changed = 0
    failed = 0
    total_old = total_new = 0
    for i, (cid, cit) in enumerate(cases, 1):
        old_n, new_n = replace_case_refs(cid, cit)
        if old_n == -1:
            failed += 1
            continue
        if old_n != new_n:
            changed += 1
            log.info("  %s: %d -> %d rows", cit, old_n, new_n)
        total_old += old_n
        total_new += new_n
        if i % 100 == 0:
            log.info("... %d/%d cases processed", i, len(cases))
    log.info("Done: %d/%d cases changed, %d failed. rows %d -> %d",
             changed, len(cases), failed, total_old, total_new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
