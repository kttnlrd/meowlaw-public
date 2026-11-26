#!/usr/bin/env python3
"""Insert the 22 new rulings into the rulings table (linked to documents)."""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DB = "cadena_knowledge"

# ref -> (ruling_type, ruling_number)
NEW = {
    "CR 2026/46": ("CR", "2026/46"), "CR 2026/47": ("CR", "2026/47"),
    "CR 2026/48": ("CR", "2026/48"), "CR 2026/49": ("CR", "2026/49"),
    "CR 2026/50": ("CR", "2026/50"), "CR 2026/51": ("CR", "2026/51"),
    "CR 2026/52": ("CR", "2026/52"), "CR 2026/53": ("CR", "2026/53"),
    "CR 2026/54": ("CR", "2026/54"), "CR 2026/55": ("CR", "2026/55"),
    "CR 2026/56": ("CR", "2026/56"), "CR 2026/57": ("CR", "2026/57"),
    "CR 2026/58": ("CR", "2026/58"), "CR 2026/59": ("CR", "2026/59"),
    "CR 2026/60": ("CR", "2026/60"),
    "LCG 2026/1": ("LCG", "2026/1"), "LCG 2026/2": ("LCG", "2026/2"),
    "LCG 2026/3": ("LCG", "2026/3"),
    "PR 2026/13": ("PR", "2026/13"), "PR 2026/14": ("PR", "2026/14"),
    "TD 2026/D1": ("TD", "2026/D1"), "TD 2026/D2": ("TD", "2026/D2"),
}


def esc(s: str) -> str:
    return s.replace("'", "''")


def sql_script(script: str, timeout: int = 60) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
        f.write(script)
        tmp = f.name
    try:
        r = subprocess.run(["docker", "cp", tmp, "cadena-postgres:/tmp/ingest_rulings.sql"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            print(f"  docker cp error: {r.stderr[:200]}", file=sys.stderr)
            return False
        r = subprocess.run(
            ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", DB,
             "-q", "-v", "ON_ERROR_STOP=1", "-f", "/tmp/ingest_rulings.sql"],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            print(f"  SQL error: {r.stderr[:400]}", file=sys.stderr)
            return False
        return True
    finally:
        Path(tmp).unlink(missing_ok=True)
        subprocess.run(["docker", "exec", "cadena-postgres", "rm", "-f", "/tmp/ingest_rulings.sql"],
                       capture_output=True, timeout=5)


def main():
    # existing
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", DB,
         "-t", "-A", "-c",
         "SELECT ruling_type || ' ' || ruling_number FROM rulings"],
        capture_output=True, text=True, timeout=60,
    )
    existing = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    print(f"Existing rulings rows: {len(existing)}")

    parts = []
    for ref, (rtype, num) in NEW.items():
        if ref in existing:
            print(f"  skip (exists): {ref}")
            continue
        parts.append(
            f"INSERT INTO rulings (document_id, ruling_type, ruling_number, status, related_provisions) "
            f"SELECT d.id, '{rtype}', '{esc(num)}', 'current', '{{}}' "
            f"FROM documents d WHERE d.reference = '{esc(ref)}' "
            f"AND NOT EXISTS (SELECT 1 FROM rulings WHERE ruling_type='{rtype}' AND ruling_number='{esc(num)}');"
        )
    print(f"To insert: {len(parts)}")
    if not parts:
        return
    script = "BEGIN;\n" + "\n".join(parts) + "\nCOMMIT;"
    if sql_script(script):
        print("INSERTED")
    else:
        print("FAILED")
    # verify
    refs = ", ".join(f"'{esc(r)}'" for r in NEW)
    r2 = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", DB,
         "-t", "-A", "-c",
         f"SELECT count(*) FROM rulings WHERE ruling_type || ' ' || ruling_number IN ({refs})"],
        capture_output=True, text=True, timeout=30,
    )
    print(f"Verified rows present: {r2.stdout.strip()}")


if __name__ == "__main__":
    main()
