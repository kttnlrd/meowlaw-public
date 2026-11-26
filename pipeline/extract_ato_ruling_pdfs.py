#!/usr/bin/env python3
"""Extract text + metadata from ATO ruling PDFs into RULING dir naming convention."""
import os
import json
import re
from pathlib import Path

import fitz  # pymupdf

SOURCE_DIR = Path(os.path.join(os.environ["KNOWLEDGE_MCP_DIR"], "data", "ato_rulings"))
OUT_DIR = Path(f"{Path(__file__).resolve().parent.parent}/data/rulings")

FILENAME_RE = re.compile(r"^([a-z]+)(\d{4})-(\d+)$", re.I)
STATUS_RE = re.compile(r"Status:\s*(.+)")


def extract_title(cover_page_text: str, ruling_type: str, ruling_number: str) -> str:
    # cover sheet: "TD 2024/1 - Title text..." wrapped over several lines,
    # ending before "This cover sheet is provided for information only."
    lines = [l.strip() for l in cover_page_text.splitlines() if l.strip()]
    prefix = f"{ruling_type} {ruling_number} - "
    title_lines = []
    for l in lines:
        if l.startswith("This cover sheet"):
            break
        if l.startswith(prefix):
            l = l[len(prefix):]
        title_lines.append(l)
    title = " ".join(title_lines).strip()
    return title or f"{ruling_type} {ruling_number}"


def extract_status(full_text: str) -> str:
    m = STATUS_RE.search(full_text)
    return m.group(1).strip() if m else ""


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(SOURCE_DIR.glob("*/**/*.pdf"))
    print(f"Found {len(pdfs)} PDFs")

    for pdf_path in pdfs:
        m = FILENAME_RE.match(pdf_path.stem)
        if not m:
            print(f"  Skip (name mismatch): {pdf_path}")
            continue
        ruling_type = m.group(1).upper()
        year = m.group(2)
        number = str(int(m.group(3)))
        stem = f"{ruling_type}_{year}_{number}"

        doc = fitz.open(pdf_path)
        pages_text = [p.get_text() for p in doc]
        full_text = "".join(pages_text)
        doc.close()

        title = extract_title(pages_text[0], ruling_type, f"{year}/{number}")
        status = extract_status(full_text)

        out_txt = OUT_DIR / f"{stem}.txt"
        out_txt.write_text(full_text, encoding="utf-8")

        meta = {
            "ruling_type": ruling_type,
            "ruling_number": f"{ruling_type} {year}/{number}",
            "title": title,
            "status": status,
            "issue_date": None,
        }
        (OUT_DIR / f"{stem}.txt.meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        print(f"  {pdf_path.name} -> {out_txt.name}")


if __name__ == "__main__":
    main()
