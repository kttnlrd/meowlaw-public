#!/usr/bin/env python3
"""Insert the 22 newly-fetched ruling .txt files as documents rows in cadena_knowledge.

Idempotent: skips references already present. Uses the same docker-exec psql
pattern as ingest_ruling_summaries.py.
"""
import glob
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.data_loader import validate_ruling_meta  # noqa: E402

DB = "cadena_knowledge"
RULINGS_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data/rulings")

NEW_FILES = [
    Path(p)
    for p in (
        sorted(glob.glob(str(RULINGS_DIR / "CR_2026_4[6-9].txt")))
        + sorted(glob.glob(str(RULINGS_DIR / "CR_2026_5*.txt")))
        + sorted(glob.glob(str(RULINGS_DIR / "CR_2026_60.txt")))
        + sorted(glob.glob(str(RULINGS_DIR / "LCG_2026_*.txt")))
        + sorted(glob.glob(str(RULINGS_DIR / "PR_2026_1[34].txt")))
        + sorted(glob.glob(str(RULINGS_DIR / "TD_2026_D?.txt")))
    )
]


def filename_to_ref(stem: str) -> str:
    parts = stem.split("_")
    rtype = parts[0]
    if rtype == "PS_LA":
        return f"PS LA {parts[2]}/{parts[3]}"
    if rtype == "TD" and len(parts) == 4 and parts[2] == "D":
        # TD_2026_D1 -> TD 2026/D1
        return f"TD {parts[1]}/{parts[3]}"
    year, num = parts[1], parts[2]
    return f"{rtype} {year}/{num}"


def title_from_file(path: Path, ref: str) -> str:
    t = path.read_text(encoding="utf-8", errors="replace")
    first = t.split("\n")[0].strip()
    if first and len(first) > 3 and "Legal database" not in first and "Download" not in first:
        return first[:200]
    # fall back to the second non-noise line
    for line in t.split("\n")[2:6]:
        s = line.strip()
        if len(s) > 10 and "Legal database" not in s and "Download" not in s:
            return s[:200]
    return ref


def esc(s: str) -> str:
    return s.replace("'", "''")


def sql_script(script: str, timeout: int = 120) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
        f.write(script)
        tmp = f.name
    try:
        r = subprocess.run(["docker", "cp", tmp, "cadena-postgres:/tmp/ingest_docs.sql"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            print(f"  docker cp error: {r.stderr[:200]}", file=sys.stderr)
            return False
        r = subprocess.run(
            ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", DB,
             "-q", "-v", "ON_ERROR_STOP=1", "-f", "/tmp/ingest_docs.sql"],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            print(f"  SQL error: {r.stderr[:400]}", file=sys.stderr)
            return False
        return True
    finally:
        Path(tmp).unlink(missing_ok=True)
        subprocess.run(["docker", "exec", "cadena-postgres", "rm", "-f", "/tmp/ingest_docs.sql"],
                       capture_output=True, timeout=5)


def main():
    # Existing refs
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", DB,
         "-t", "-A", "-c", "SELECT reference FROM documents WHERE doc_type='ruling'"],
        capture_output=True, text=True, timeout=60,
    )
    existing = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    print(f"Existing ruling docs: {len(existing)}")

    inserts = []
    for path in NEW_FILES:
        stem = path.stem  # e.g. CR_2026_46
        # Completeness gate: sidecar meta (citation/status/type/year) must agree
        # with the filename — fail the ingest rather than insert a bad draft.
        sidecar = None
        for cand in (Path(str(path) + ".meta.json"), Path(str(path) + ".txt.meta.json")):
            if cand.exists():
                try:
                    sidecar = json.loads(cand.read_text(encoding="utf-8"))
                except Exception:
                    pass
                break
        if sidecar:
            try:
                validate_ruling_meta(stem, sidecar)
            except ValueError as e:
                print(f"  GATE FAIL {stem}: {e}")
                sys.exit(1)
        ref = filename_to_ref(stem)
        if ref in existing:
            print(f"  skip (exists): {ref}")
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) < 100:
            print(f"  SKIP too short: {ref} ({len(content)}b)")
            continue
        title = title_from_file(path, ref)
        meta = {
            "source": "ato",
            "type": stem.split("_")[0],
            "draft": "D" in ref.split("/")[-1].upper() or "/D" in ref.upper(),
            "ingested_at": datetime.now().isoformat(),
        }
        inserts.append((ref, title, content, json.dumps(meta)))

    print(f"To insert: {len(inserts)}")
    if not inserts:
        return

    sql_parts = []
    for ref, title, content, meta in inserts:
        cap = content[:100000]
        sql_parts.append(
            f"INSERT INTO documents (doc_type, reference, title, content, metadata) "
            f"SELECT 'ruling', '{esc(ref)}', '{esc(title)}', '{esc(cap)}', '{esc(meta)}'::jsonb "
            f"WHERE NOT EXISTS (SELECT 1 FROM documents WHERE reference = '{esc(ref)}');"
        )
    script = "BEGIN;\n" + "\n".join(sql_parts) + "\nCOMMIT;"
    if sql_script(script):
        print(f"INSERTED {len(inserts)} documents")
    else:
        print("FAILED — no rows inserted")

    # Verify
    refs = ", ".join(f"'{esc(r[0])}'" for r in inserts)
    r2 = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", DB,
         "-t", "-A", "-c", f"SELECT count(*) FROM documents WHERE reference IN ({refs})"],
        capture_output=True, text=True, timeout=30,
    )
    print(f"Verified rows now present: {r2.stdout.strip()}")


if __name__ == "__main__":
    main()
