#!/usr/bin/env python3
"""Export GSTR, SGR, MT, TA rulings from PostgreSQL to flat text files.

Usage: python3 export_missing_rulings.py [--dry-run]

Connects via `docker exec cadena-postgres psql -U postgres -d cadena_knowledge`.
Writes .txt and .txt.meta.json files to RULING_DIR.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

RULING_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "rulings"))

# Type mapping for filename generation
# Each has: type_in_db, filename_prefix, docid_code, issues around year derivation
TYPES_TO_EXPORT = {
    "GSTR": {"prefix": "GSTR"},
    "SGR": {"prefix": "SGR"},
    "MT": {"prefix": "MT"},
    "TA": {"prefix": "TA"},
}

# These types already exist in RULING_DIR - don't overwrite
# But we should skip if file already exists


def run_psql(query: str) -> str:
    """Run a query via docker exec psql and return stdout."""
    cmd = [
        "docker", "exec", "-i", "cadena-postgres",
        "psql", "-U", "postgres", "-d", "cadena_knowledge",
        "-t", "--no-align",
        "-c", query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"PSQL error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def parse_ruling_number(ruling_number: str):
    """Parse '2000/1' -> (year, num)."""
    m = re.match(r'(\d{4})/(\d+)', str(ruling_number))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def get_citation_display(rtype: str, year: int, num: int) -> str:
    """Get the standard citation display like 'GSTR 2000/1'."""
    return f"{rtype} {year}/{num}"


def guess_title_from_content(content: str) -> str:
    """Try to extract a descriptive title from the content.

    Strategy:
    1. Look for the 'Title:' line in the HEAD NOTE section
    2. Fall back to looking for the descriptive line after the citation
    """
    lines = content.splitlines()

    # Strategy 1: Find "Title:" in the HEAD NOTE section
    for ln in lines:
        m = re.match(r'^Title:\s*(.+)', ln.strip())
        if m:
            title_text = m.group(1).strip()
            # Filter out generic titles
            generic = [
                "Goods and Services Tax Ruling",
                "Taxpayer Alerts",
                "Miscellaneous Taxation Ruling",
                "Superannuation Guarantee Ruling",
            ]
            if title_text not in generic:
                return title_text

    # Strategy 2: Find the citation line and take the next meaningful line
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if re.match(r'^[A-Z]+\s+\d{4}/\d+[Ww]?\s*\|', ln) or re.match(r'^[A-Z]+\s+\d{4}/\d+[Ww]?$', ln):
            for j in range(i + 1, min(i + 8, len(lines))):
                next_ln = lines[j].strip()
                if next_ln and not next_ln.startswith("Please") and not next_ln.startswith("PDF") and not next_ln.startswith("Legal database") and not next_ln.startswith("Contents") and not next_ln.startswith("Download") and not next_ln.startswith("Email") and not next_ln.startswith("Print") and not next_ln.startswith("Back to browse"):
                    return next_ln
            break
    return ""


def export_type(rtype: str, dry_run: bool = False) -> int:
    """Export all rulings of a given type. Returns count exported."""
    query = f"""
        SELECT d.content, d.title, r.ruling_number, r.status, r.issue_date, r.related_provisions
        FROM documents d
        JOIN rulings r ON r.document_id = d.id
        WHERE r.ruling_type = '{rtype}'
        ORDER BY r.ruling_number;
    """
    output = run_psql(query)
    if not output:
        print(f"  No results for type {rtype}")
        return 0

    # psql --no-align outputs pipe-separated with one row per line
    # But content can have newlines, so this won't work easily.
    # Let's use a different approach - psql with JSON output
    return export_type_json(rtype, dry_run)


def export_type_json(rtype: str, dry_run: bool = False) -> int:
    """Export using JSON output from psql."""
    query = f"""
        SELECT json_agg(json_build_object(
            'content', d.content,
            'title', d.title,
            'ruling_number', r.ruling_number,
            'status', r.status,
            'issue_date', r.issue_date,
            'related_provisions', r.related_provisions
        ) ORDER BY r.ruling_number)
        FROM documents d
        JOIN rulings r ON r.document_id = d.id
        WHERE r.ruling_type = '{rtype}';
    """
    output = run_psql(query)
    if not output or output == "[]" or output == "":
        print(f"  No results for type {rtype}")
        return 0

    try:
        rows = json.loads(output)
    except json.JSONDecodeError:
        print(f"  JSON decode error for type {rtype}: {output[:200]}")
        return 0

    count = 0
    for row in rows:
        content = row.get("content", "")
        title = row.get("title", "")
        ruling_number = str(row.get("ruling_number", ""))
        status = row.get("status", "")
        issue_date = row.get("issue_date")
        related_provisions = row.get("related_provisions") or []

        year, num = parse_ruling_number(ruling_number)
        if year is None or num is None:
            print(f"  WARNING: Could not parse ruling_number '{ruling_number}' for {rtype}")
            continue

        prefix = TYPES_TO_EXPORT[rtype]["prefix"]
        filename = f"{prefix}_{year}_{num}.txt"
        filepath = RULING_DIR / filename

        if filepath.exists():
            print(f"  SKIP (exists): {filename}")
            count += 1
            continue

        # Also check if there's a W (withdrawn) variant
        if status and status.lower() == "withdrawn":
            alt_filename = f"{prefix}_{year}_{num}W.txt"
            alt_filepath = RULING_DIR / alt_filename
            if alt_filepath.exists():
                print(f"  SKIP (W variant exists): {alt_filename}")
                count += 1
                continue

        if not content:
            print(f"  WARNING: Empty content for {prefix}_{year}_{num}")
            continue

        citation_display = get_citation_display(rtype, year, num)

        # Fix: if title doesn't contain the ruling type, use a better title
        # The title from DB is often just "Goods and Services Tax Ruling" which is generic
        extracted_title = guess_title_from_content(content)
        if extracted_title:
            display_title = extracted_title
        else:
            display_title = title or citation_display

        meta = {
            "doc_type": "ruling",
            "ruling_type": rtype,
            "ruling_number": citation_display,
            "title": display_title,
            "status": status or None,
            "issue_date": issue_date,
            "related_provisions": related_provisions,
            "related_rulings": [],
        }

        if dry_run:
            print(f"  WOULD CREATE: {filename} ({citation_display})")
        else:
            filepath.write_text(content, encoding="utf-8")
            print(f"  CREATED: {filename}")

            meta_path = filepath.with_suffix(filepath.suffix + ".meta.json")
            meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
            print(f"  CREATED: {meta_path.name}")

        count += 1

    return count


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=== DRY RUN MODE ===\n")

    print("Exporting missing rulings from PostgreSQL...\n")

    total = 0
    for rtype in TYPES_TO_EXPORT:
        print(f"\n--- {rtype} ---")
        count = export_type_json(rtype, dry_run)
        print(f"  Total {rtype}: {count}")
        total += count

    print(f"\n=== Total: {total} rulings {'(would be)' if dry_run else ''} exported ===")


if __name__ == "__main__":
    main()
