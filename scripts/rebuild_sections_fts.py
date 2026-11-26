#!/usr/bin/env python3.12
"""Targeted rebuild of sections_fts + sections_meta only.

Reflects changes to the section-content indexing regex (e.g. preserving
subsection `(1)` brackets in snippets) without rebuilding rulings/cases/
private-ruling FTS tables, which are far more expensive to regenerate.

Safe to run on a live index: drops and recreates only the two section tables
inside a single transaction (rolls back cleanly on failure).
"""
import sys
from backend.services.search_service import (
    search_conn, load_tree, _index_section, DATA_DIR,
)


def rebuild() -> None:
    with search_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        try:
            conn.execute("DROP TABLE IF EXISTS sections_fts")
            conn.execute("""
                CREATE VIRTUAL TABLE sections_fts USING fts5(
                    act, section, title, content,
                    tokenize='porter'
                )
            """)
            conn.execute("DROP TABLE IF EXISTS sections_meta")
            conn.execute("""
                CREATE TABLE sections_meta (
                    act TEXT, section TEXT, title TEXT, part TEXT, division TEXT,
                    UNIQUE (act, section)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_act_section ON sections_meta(act, section)")

            seen: set[tuple[str, str]] = set()
            count = 0
            for act_dir in DATA_DIR.iterdir():
                if not act_dir.is_dir() or not (act_dir / "tree.json").exists():
                    continue
                act = act_dir.name
                tree = load_tree(act)
                for part in tree.get("parts", []):
                    part_id = part.get("id", "")
                    for sec in part.get("sections", []):
                        _index_section(conn, act, sec, part_id, "", seen)
                        count += 1
                    for div in part.get("divisions", []):
                        div_id = div.get("id", "")
                        for sec in div.get("sections", []):
                            _index_section(conn, act, sec, part_id, div_id, seen)
                            count += 1
                        for sub in div.get("subdivisions", []):
                            for sec in sub.get("sections", []):
                                _index_section(conn, act, sec, part_id, div_id, seen)
                                count += 1

            conn.execute("COMMIT")
            print(f"Rebuilt sections_fts: {count} section rows traversed, "
                  f"{len(seen)} unique inserted.")
        except Exception:
            conn.execute("ROLLBACK")
            raise


if __name__ == "__main__":
    rebuild()
