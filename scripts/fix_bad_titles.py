#!/usr/bin/env python3
"""Fix PR/CR/TR ruling summary files that have title only in the inner 'raw' field.

Summary files with "error": "JSON parse failed" store the actual title
inside a truncated escaped JSON string in the "raw" field.  This script
extracts the title from raw and patches the outer JSON so the search
index builder can find it.

Usage: python3 fix_bad_titles.py
"""
import json
import re
from pathlib import Path

SUMMARIES_DIR = Path(__file__).resolve().parent.parent / "data" / "rulings" / "summaries"

TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')

def extract_title_from_raw(raw: str) -> str | None:
    m = TITLE_RE.search(raw)
    return m.group(1) if m else None

def main():
    fixed = 0
    skipped = 0

    for f in sorted(SUMMARIES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Can't even parse the outer JSON - skip
            skipped += 1
            continue

        # Already has a non-trivial title
        outer_title = data.get("title", "")
        if outer_title and outer_title != f.stem:
            continue

        # No raw field to extract from
        raw = data.get("raw", "")
        if not raw:
            continue

        title = extract_title_from_raw(raw)
        if not title:
            continue

        data["title"] = title
        # Clean up subject too if missing
        if "subject" not in data:
            subj_re = re.compile(r'"subject"\s*:\s*"([^"]+)')
            subj_m = subj_re.search(raw)
            if subj_m:
                data["subject"] = subj_m.group(1)

        # Write back with same formatting (2-space indent)
        f.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  ✅ {f.stem:25s} → {title[:60]}")
        fixed += 1

    print(f"\nFixed: {fixed}, Skipped (unparseable): {skipped}")

if __name__ == "__main__":
    main()