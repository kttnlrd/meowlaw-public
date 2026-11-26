#!/usr/bin/env python3.12
"""The embeddings producer's stale-row sweep may only delete rows it produced.

openai_embed.py walks data/<act>/sections for its own act list and then treats every row in
`embeddings` whose file is not in that walk as gone. That reading is what deleted 260,297 ruling
and case rows in an earlier incident, and it is what would delete another act's rows if the act
list and the walk ever disagreed. The sweep added for CDN-0173 must therefore be scoped twice:
to rows whose source_type this producer writes, and to paths under the acts it actually walked.

The check points the producer at a scratch DATA_DIR (so nothing real is touched) and asserts:
  * a now-missing section file under a walked act IS deleted;
  * a ruling/case row, and a section row for an act that was not walked, are NOT;
  * commit=False deletes nothing.

Run: /usr/bin/python3.12 scripts/test_embed_prune_scope.py      (exit 1 on any failure)
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def load_module():
    spec = importlib.util.spec_from_file_location("openai_embed_under_test",
                                                  ROOT / "scripts" / "openai_embed.py")
    if spec is None or spec.loader is None:
        raise SystemExit("cannot import scripts/openai_embed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = load_module()

    with tempfile.TemporaryDirectory() as td:
        data = Path(td)
        # a walked act with one file present and one gone
        (data / "walked" / "sections" / "keep").mkdir(parents=True)
        (data / "walked" / "sections" / "keep" / "1.md").write_text("# keep\n", encoding="utf-8")
        # an act that exists on disk but is NOT in this producer's act list
        (data / "unwalked" / "sections").mkdir(parents=True)
        (data / "unwalked" / "sections" / "keep.md").write_text("# keep\n", encoding="utf-8")

        mod.DATA_DIR = data
        mod.LEGISLATION_ACTS = ["walked"]
        mod.COMMENTARY_ACTS = []

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY, source_type TEXT,"
                     " act TEXT, file_path TEXT)")
        rows = [
            # (source_type, act, file_path, must_be_deleted)
            ("section", "walked", "walked/sections/keep/1.md", False),        # on disk
            ("section", "walked", "walked/sections/keep/gone.md", True),      # file removed
            ("section", "unwalked", "unwalked/sections/keep.md", False),      # act not walked
            ("section", "unwalked", "unwalked/sections/vanished.md", False),  # act not walked
            ("ruling", "walked", "rulings/TR2020-1.json", False),             # another producer
            ("case", "walked", "summaries/case-1.json", False),               # another producer
            ("commentary", "walked", "walked/sections/keep/2.md", True),      # commentary too
        ]
        conn.executemany("INSERT INTO embeddings (source_type, act, file_path) VALUES (?,?,?)",
                         [(r[0], r[1], r[2]) for r in rows])

        print("== dry run deletes nothing ==")
        stale = mod.prune_stale_rows(conn, commit=False)
        check("dry run reports the stale file", stale == ["walked/sections/keep/2.md",
                                                          "walked/sections/keep/gone.md"],
              str(stale))
        check("dry run left every row in place",
              conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == len(rows))

        print("== the sweep deletes only this producer's now-missing input ==")
        stale = mod.prune_stale_rows(conn, commit=True)
        left = {r[0] for r in conn.execute("SELECT file_path FROM embeddings")}
        for st, act, path, deleted in rows:
            check(f"{'deleted' if deleted else 'survives'}: {path}",
                  (path not in left) if deleted else (path in left),
                  f"stale={stale}")

        print("== idempotent ==")
        check("a second run finds nothing", mod.prune_stale_rows(conn, commit=True) == [])

    print("PASS" if not failures else f"{len(failures)} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
