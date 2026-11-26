#!/usr/bin/env python3
"""Add any section missing from the lexical index, per act, so every section on disk is findable.

Rebuilds nothing and drops nothing: it enumerates the sections in each act's tree.json, compares
them with the rows already in sections_fts/sections_meta, and inserts only what is absent. Content
comes from the same helper the running service uses, and is normalised the same way, so an inserted
row is indistinguishable from one the indexer wrote.

Sections are addressed by their tree.json id, so this also repairs acts that were indexed before
later sections existed.

Usage:
    python3 scripts/rebuild_fts_coverage.py --dry-run
    python3 scripts/rebuild_fts_coverage.py
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
SEARCH_DB = ROOT / "search_index.db"


def tree_sections(act: str) -> list[tuple[str, str, str, str]]:
    """(id, title, part, division) for every section in an act's tree, at any depth."""
    tree = json.loads((DATA / act / "tree.json").read_text(encoding="utf-8"))
    out: list[tuple[str, str, str, str]] = []

    def walk(node: dict, part: str = "", division: str = "") -> None:
        part = node.get("part", part) if isinstance(node, dict) else part
        division = node.get("division", division) if isinstance(node, dict) else division
        for node_key in ("parts", "divisions", "subdivisions"):
            for child in node.get(node_key, []) or []:
                label = str(child.get("title") or child.get("id") or "").strip()
                if node_key == "parts":
                    walk(child, part=label, division="")
                else:
                    walk(child, part=part, division=label)
        for sec in node.get("sections", []) or []:
            out.append((str(sec.get("id")), str(sec.get("title") or ""), part, division))

    walk(tree)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", default="",
                    help="comma-separated acts to rewrite wholesale: needed after a re-ingest, "
                         "because inserting missing rows leaves the previous compilation's text "
                         "indexed under ids that still exist")
    args = ap.parse_args()

    from backend.services.data_loader import get_act_section_content  # noqa: E402

    con = sqlite3.connect(SEARCH_DB)
    existing: dict[str, set[str]] = {}
    for act, section in con.execute("select act, section from sections_fts"):
        existing.setdefault(act, set()).add(str(section))

    refresh = {a.strip() for a in args.refresh.split(",") if a.strip()}
    if refresh and not args.dry_run:
        for act in sorted(refresh):
            n = con.execute("delete from sections_fts where act = ?", (act,)).rowcount
            con.execute("delete from sections_meta where act = ?", (act,))
            existing[act] = set()
            print(f"    {act}: cleared {n} stale rows for refresh")

    total_added, total_missing_content, per_act = 0, 0, []
    for tree in sorted(DATA.glob("*/tree.json")):
        act = tree.parent.name
        have = existing.get(act, set())
        missing = [s for s in tree_sections(act) if s[0] not in have]
        added = 0
        for sec_id, title, part, division in missing:
            try:
                _fm, body = get_act_section_content(act, sec_id)
            except Exception as exc:
                print(f"    {act}/{sec_id}: no content ({type(exc).__name__})")
                body = ""
            content = re.sub(r"[#*`_\[\]\(\)]", " ", body or "")
            content = re.sub(r"\s+", " ", content).strip()[:50000]
            if not content:
                total_missing_content += 1
            if not args.dry_run:
                con.execute("INSERT INTO sections_fts (act, section, title, content) VALUES (?,?,?,?)",
                            (act, sec_id, title, content))
                con.execute("INSERT OR REPLACE INTO sections_meta "
                            "(act, section, title, part, division) VALUES (?,?,?,?,?)",
                            (act, sec_id, title, part, division))
            added += 1
        if added:
            per_act.append(f"    {act:24s} +{added:5d} (had {len(have):5d})")
        total_added += added

    if not args.dry_run:
        con.commit()
    print("\n".join(per_act) or "    nothing to add")
    print(f"\n  sections added: {total_added:,}"
          + (f" | {total_missing_content} with empty content" if total_missing_content else ""))
    if not args.dry_run:
        print(f"  sections_fts rows now: "
              f"{con.execute('select count(*) from sections_fts').fetchone()[0]:,}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
