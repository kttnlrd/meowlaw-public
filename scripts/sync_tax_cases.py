#!/usr/bin/env python3
"""
Monthly cron: check for new tax cases in the cadena_knowledge SQL database
and update the tax case JSON files used by the legislation-explorer.

Runs via: cronjob (monthly)
"""
import json
import re
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data")

# Tax keywords for filtering
TAX_KEYWORDS = [
    "commissioner of taxation", "deputy commissioner of taxation",
    "taxation", "income tax", "gst", "fringe benefit",
    "tax practitioners board", "ato",
]

SEP = "¶"

def sql(query: str, timeout: int = 30) -> list[list[str]]:
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", "cadena_knowledge",
         "-t", "-F", SEP, "-A", "-c", query],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        print(f"SQL error: {r.stderr[:200]}")
        return []
    rows = []
    for line in r.stdout.split("\n"):
        line = line.strip()
        if line:
            rows.append(line.split(SEP))
    return rows


def sql_content(citation: str) -> str | None:
    safe = citation.replace("'", "''")
    r = subprocess.run(
        ["docker", "exec", "cadena-postgres", "psql", "-U", "postgres", "-d", "cadena_knowledge",
         "-t", "-F", SEP, "-A", "-c",
         f"SELECT LEFT(content, 3000) FROM documents WHERE reference = '{safe}' LIMIT 1"],
        capture_output=True, text=True, timeout=10,
    )
    output = r.stdout.strip()
    if not output:
        return None
    lines = output.split("\n")
    return "\n".join(l.strip() for l in lines if l.strip())


def extract_catchwords(content: str) -> str | None:
    if not content or len(content) < 100:
        return None
    pattern = r'(?:CATCHWORDS|Catchwords)\s*\n(.+?)(?:\n\n[A-Z\s\(\)]{3,80}\n|\n\nLEGISLATION|\n\nNoteup|\nLast Updated:|\Z)'
    m = re.search(pattern, content, re.DOTALL)
    if m:
        cw = m.group(1).strip()
        cw = re.sub(r'\s+', ' ', cw)
        return cw[:3000] if len(cw) > 30 else None
    return None


def is_tax_case(title: str, citation: str) -> bool:
    text = f"{title.lower()} {citation.lower()}"
    return any(kw in text for kw in TAX_KEYWORDS)


def load_json(filename: str) -> list[dict]:
    path = DATA_DIR / filename
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return []


def save_json(filename: str, data: list[dict]):
    with open(DATA_DIR / filename, "w") as f:
        json.dump(data, f, indent=2)


def build_austlii_url(citation: str) -> str:
    m = re.match(r"\[(\d{4})\]\s+(\S+)\s+(\d+)", citation)
    if m:
        return f"https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/{m.group(2)}/{m.group(1)}/{m.group(3)}.html"
    return ""


# =========================================================================
# Main
# =========================================================================

print("=== Tax Case Sync ===\n")

# Load existing cases and catchwords
existing_catchwords = {}
cw_path = DATA_DIR / "case_catchwords.json"
if cw_path.exists():
    with open(cw_path) as f:
        existing_catchwords = json.load(f)

# Build set of existing citations
court_files = {
    "hca": ("hca_tax_cases.json", "HCA"),
    "fca": ("fca_tax_cases.json", "FCA"),
    "fcafc": ("fcafc_tax_cases.json", "FCAFC"),
    "aata": ("aata_tax_cases.json", "AATA"),
}

existing_citations = set()
for court, (filename, _) in court_files.items():
    cases = load_json(filename)
    for c in cases:
        existing_citations.add(c.get("citation", ""))

print(f"Existing citations: {len(existing_citations)}")

# Find the latest year across all files
max_year = 0
for court, (filename, db_court) in court_files.items():
    cases = load_json(filename)
    for c in cases:
        y = c.get("year", 0) or 0
        if y > max_year:
            max_year = y

print(f"Latest year in files: {max_year}")

# Query SQL for cases newer than max_year
print(f"\nQuerying SQL for cases >= {max_year}...")
new_total = 0
new_catchwords = {}

for court_key, (filename, db_court) in court_files.items():
    cases = load_json(filename)
    rows = sql(
        f"SELECT d.reference, d.title, d.metadata, LEFT(d.content, 500) "
        f"FROM documents d JOIN cases c ON c.document_id = d.id "
        f"WHERE d.doc_type='case' AND c.court = '{db_court}' "
        f"AND EXTRACT(YEAR FROM c.decision_date) >= {max_year} "
        f"ORDER BY d.reference",
        timeout=60
    )
    print(f"  {db_court}: {len(rows)} rows from SQL")

    added = 0
    for row in rows:
        if len(row) < 2:
            continue
        citation = row[0].strip()
        title = row[1].strip()

        if citation in existing_citations:
            continue

        if not is_tax_case(title, citation):
            continue

        # Get year from citation
        m = re.match(r"\[(\d{4})\]", citation)
        year = int(m.group(1)) if m else 0

        case = {
            "title": title,
            "citation": citation,
            "year": year,
            "austlii_url": build_austlii_url(citation),
        }

        # Get catchwords
        if citation not in existing_catchwords:
            content = sql_content(citation)
            cw = extract_catchwords(content) if content else None
            if cw:
                case["catchwords"] = cw
                new_catchwords[citation] = cw

        cases.append(case)
        existing_citations.add(citation)
        added += 1

    save_json(filename, cases)
    print(f"  Added {added} new cases to {filename}")
    new_total += added

# Save new catchwords
if new_catchwords:
    existing_catchwords.update(new_catchwords)
    with open(cw_path, "w") as f:
        json.dump(existing_catchwords, f, indent=2)
    print(f"\nAdded {len(new_catchwords)} new catchwords")

print(f"\n=== Done: {new_total} new cases added ===")
