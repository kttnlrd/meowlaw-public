#!/usr/bin/env python3
"""Rebuild truncated section markdown files from the FTS5 search index.

The FTS5 search index was built from the original Federal Register XML source,
so it has the COMPLETE section text. This script reads the complete text from
the search index and rewrites the markdown files with the full content.

Usage:
    python3 scripts/rebuild_sections.py                             # fix all truncated sections
    python3 scripts/rebuild_sections.py --act itaa-1997 --section 6-1  # fix a specific section
    python3 scripts/rebuild_sections.py --dry-run                    # show what would be fixed
"""

import argparse
import logging
import os
import re
import sqlite3
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Config
BASE = Path(os.environ.get("BASE", f"{Path(__file__).resolve().parent.parent}"))
DATA_DIR = BASE / "data"
SEARCH_DB = BASE / "search_index.db"


def get_truncated_sections() -> list[dict]:
    """Find all sections whose markdown is truncated (missing sentence-ending punctuation)."""
    truncated = []
    for act_dir in sorted(DATA_DIR.iterdir()):
        if not act_dir.is_dir() or not (act_dir / "tree.json").exists():
            continue
        act = act_dir.name
        sections_dir = act_dir / "sections"
        if not sections_dir.exists():
            continue
        for md_file in sections_dir.rglob("*.md"):
            content = md_file.read_text(encoding="utf-8")
            # Strip frontmatter
            body = content
            if content.startswith("---"):
                fm_end = re.search(r"\n---\s*\n", content)
                if fm_end:
                    body = content[fm_end.end():]
            # Strip compilation footer
            body_clean = re.sub(r"\n---\s*\*Last updated:.*?\*", "", body, flags=re.DOTALL)
            body_clean = re.sub(r"\n---\s*$", "", body_clean).strip()
            # Check if ends with sentence-ending punctuation
            if body_clean and not re.search(r'[.\)"\'!?]\s*$', body_clean):
                rel_path = md_file.relative_to(DATA_DIR)
                truncated.append({
                    "act": act,
                    "section": md_file.stem,
                    "path": str(rel_path),
                    "body_length": len(body_clean),
                })
    return truncated


def get_full_text_from_fts5(act: str, section: str) -> str | None:
    """Get the full section text from the FTS5 search index."""
    if not SEARCH_DB.exists():
        log.error(f"Search index not found: {SEARCH_DB}")
        return None
    try:
        conn = sqlite3.connect(str(SEARCH_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT content FROM sections_fts WHERE act = ? AND section = ?",
            (act, section),
        ).fetchone()
        conn.close()
        if row:
            return row["content"]
    except Exception as e:
        log.error(f"FTS5 query failed: {e}")
    return None


def rebuild_section(act: str, section: str) -> bool:
    """Rebuild a section's markdown file with the full text from FTS5."""
    full_text = get_full_text_from_fts5(act, section)
    if not full_text:
        log.warning(f"No FTS5 data for {act}/{section}")
        return False

    # Find the markdown file
    md_path = None
    for f in (DATA_DIR / act / "sections").rglob(f"{section}.md"):
        md_path = f
        break

    if not md_path:
        log.warning(f"Markdown file not found for {act}/{section}")
        return False

    # Read the existing file to preserve frontmatter
    content = md_path.read_text(encoding="utf-8")
    frontmatter = ""
    if content.startswith("---"):
        fm_end = re.search(r"\n---\s*\n", content)
        if fm_end:
            frontmatter = content[:fm_end.end()]

    # Get the compilation footer from the existing file
    footer_match = re.search(r"(\n---\s*\*Last updated:.*?\*)", content, flags=re.DOTALL)
    footer = footer_match.group(1) if footer_match else ""

    # Write the new file: frontmatter + full text + footer
    new_content = frontmatter + full_text.strip() + footer
    md_path.write_text(new_content, encoding="utf-8")
    log.info(f"Rebuilt {act}/{section} ({len(full_text):,} chars)")
    return True


def main():
    parser = argparse.ArgumentParser(description="Rebuild truncated section markdown files")
    parser.add_argument("--act", type=str, help="Act ID (e.g. itaa-1997)")
    parser.add_argument("--section", type=str, help="Section number (e.g. 6-1)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fixed")
    args = parser.parse_args()

    if args.act and args.section:
        sections = [{"act": args.act, "section": args.section}]
    else:
        sections = get_truncated_sections()
        log.info(f"Found {len(sections)} truncated sections")

    if args.dry_run:
        log.info("Would rebuild:")
        for s in sections[:20]:
            log.info(f"  {s['act']}/{s['section']} ({s.get('body_length', '?')} chars)")
        if len(sections) > 20:
            log.info(f"  ... and {len(sections) - 20} more")
        return

    rebuilt = 0
    failed = 0
    for s in sections:
        if rebuild_section(s["act"], s["section"]):
            rebuilt += 1
        else:
            failed += 1

    log.info(f"Rebuilt {rebuilt} sections, {failed} failed")


if __name__ == "__main__":
    main()