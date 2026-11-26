#!/usr/bin/env python3.12
"""S4: the duplicate master-tax-examples twins - prove them duplicate, then delete safely.

23 files under `data/master-tax-examples/sections/` are the 80-81-character cut versions of
a registered section: two slug generations were written in one regeneration (19068816f,
2026-06-11) and only one was registered in tree.json / section_index.json.  The plan asks for
three things before `git rm`:

  1. each unregistered twin's normalised body must equal its registered twin's body;
  2. no reference to the unregistered ids or file names anywhere in backend/, scripts/,
     graph.db, the embeddings or the search index;
  3. the deletion goes through `scripts/corpus_change_guard.py` with its --json output as
     evidence - not a blanket bypass.

This script does 1 and 2 and writes the JSON the commit message cites; 3 is
`corpus_change_guard.py --json` over the staged deletion, run separately (see the batch log).

Dry run by default; `--json <path>` writes the full report.

    /usr/bin/python3.12 scripts/check_unregistered_twins.py [--json report.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ACT = "master-tax-examples"
sys.path.insert(0, str(ROOT / "scripts"))
# the orphan rule is the scanner's, imported and never forked (S4 / C27)
import scan_corpus_error_classes as S  # noqa: E402


def strip_frontmatter(text: str) -> tuple[str, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[1], parts[2]
    return "", text


def normalised_body(text: str) -> str:
    """Body with frontmatter dropped, bullets unified and every whitespace run collapsed."""
    _fm, body = strip_frontmatter(text)
    body = body.replace("\u2022", "-").replace("\u2013", "-").replace("\u2014", "-")
    return " ".join(body.split())


def normalised_frontmatter(text: str) -> dict[str, str]:
    """Frontmatter as a dict, with the `section:` slug dropped - the slug IS the difference."""
    fm, _body = strip_frontmatter(text)
    out = {}
    for line in fm.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip() != "section":
                out[k.strip()] = v.strip()
    return out


def twins(orphans: list[Path]) -> list[dict]:
    section_files = sorted((DATA / ACT / "sections").rglob("*.md"))
    out = []
    for p in orphans:
        cands = [q for q in section_files if q.stem.startswith(p.stem) and q != p]
        rec = {"orphan": p.name, "orphan_id": p.stem, "twin": None, "body_equal": None,
               "frontmatter_equal": None, "lengths": None}
        if len(cands) == 1:
            q = cands[0]
            a, b = normalised_body(p.read_text(errors="replace")), normalised_body(q.read_text(errors="replace"))
            rec.update({"twin": q.name, "twin_id": q.stem, "body_equal": a == b,
                        "frontmatter_equal": normalised_frontmatter(p.read_text(errors="replace"))
                        == normalised_frontmatter(q.read_text(errors="replace")),
                        "lengths": {"orphan": len(a), "twin": len(b),
                                    "orphan_name": len(p.stem), "twin_name": len(q.stem)}})
        elif cands:
            rec["twin"] = f"AMBIGUOUS: {[q.name for q in cands]}"
        out.append(rec)
    return out


REF_COLUMNS = {
    DATA / "graph.db": [("nodes", ["id", "key", "label", "content_ref", "meta"]),
                        ("graph_edges", ["source_doc", "method"])],
    DATA / "embeddings.db": [("embeddings", ["file_path", "section", "section_title"])],
    ROOT / "search_index.db": [("sections_meta", ["act", "section", "title", "part", "division"]),
                              ("insolvency_meta", ["chapter", "title", "slug"]),
                              ("rulings_meta", ["citation", "title"])],
}
# The twin's id CONTAINS the orphan's id as a prefix (the orphan name is the same slug cut at
# 80-81 characters), so a `LIKE '%<orphan>%'` scan matches the REGISTERED twin's own row and
# reports a phantom reference.  An exact match is the only thing that is a reference to the
# unregistered file; prefix matches are counted separately as context.
PATH_COLUMNS = ("file_path", "content_ref", "source_doc")


def store_refs(needles: list[str]) -> dict:
    """Exact references to an unregistered id / file name, plus prefix-match context."""
    out: dict[str, dict] = {}
    for db, tables in REF_COLUMNS.items():
        if not db.exists():
            out[str(db.name)] = {"exact": ["DB MISSING"], "prefix_only": 0}
            continue
        exact: list[str] = []
        prefix = 0
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error as e:
            out[str(db.name)] = {"exact": [f"unreadable: {e}"], "prefix_only": 0}
            continue
        for table, cols in tables:
            have = {r[1] for r in c.execute(f"PRAGMA table_info('{table}')")}
            cols = [x for x in cols if x in have]
            if not cols:
                continue
            for n in needles:
                for col in cols:
                    try:
                        if col in PATH_COLUMNS:
                            # a file reference: the orphan's own file name, not the twin's
                            hit = c.execute(f"SELECT count(*) FROM {table} WHERE {col} LIKE ?",
                                            (f"%/{n}.md",)).fetchone()[0]
                            loose = c.execute(f"SELECT count(*) FROM {table} WHERE {col} LIKE ?",
                                              (f"%{n}%",)).fetchone()[0]
                        else:
                            hit = c.execute(f"SELECT count(*) FROM {table} WHERE {col} = ?",
                                            (n,)).fetchone()[0]
                            loose = c.execute(f"SELECT count(*) FROM {table} WHERE {col} LIKE ?",
                                              (f"%{n}%",)).fetchone()[0]
                    except sqlite3.Error as e:
                        exact.append(f"{table}.{col}: error {e}")
                        continue
                    if hit:
                        exact.append(f"{table}.{col}: {hit} ExactMatch {n!r}")
                    prefix += max(0, loose - hit)
        c.close()
        out[str(db.name)] = {"exact": exact, "prefix_only": prefix}
    return out


def source_refs(needles: list[str]) -> list[str]:
    """backend/ and scripts/ source text that names one of the ids."""
    hits = []
    for d in ("backend", "scripts", "pipeline", "frontend/src", "tests"):
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in (".py", ".ts", ".tsx", ".js", ".json", ".md"):
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            for n in needles:
                if n in text:
                    hits.append(f"{p.relative_to(ROOT)}: names {n!r}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the full report here")
    a = ap.parse_args()

    act_dir = DATA / ACT
    refs = S.tree_section_refs(act_dir)
    section_files = sorted((act_dir / "sections").rglob("*.md"))
    orphans = [p for p in section_files if not (str(p.relative_to(act_dir / "sections")) in refs
                                                or p.stem in refs or p.name in refs)]
    report = {"act": ACT, "registered": len(section_files) - len(orphans),
              "files_on_disk": len(section_files), "orphans": len(orphans),
              "twins": twins(orphans)}
    needles = [p.stem for p in orphans] + [p.name for p in orphans]
    report["source_references"] = source_refs(needles)
    report["store_references"] = store_refs(needles)

    not_equal = [t for t in report["twins"] if t["body_equal"] is not True]
    print(f"  {ACT}: {len(section_files)} files on disk, {report['registered']} registered, "
          f"{len(orphans)} unregistered")
    print(f"  twins: {len(report['twins'])}; bodies equal: "
          f"{len(report['twins']) - len(not_equal)}/{len(report['twins'])}")
    for t in not_equal:
        print(f"    NOT EQUAL: {t['orphan']} -> {t['twin']} {t['lengths']}")
    fm_bad = [t for t in report["twins"] if t["frontmatter_equal"] is not True]
    print(f"  frontmatter (minus the section slug) equal: "
          f"{len(report['twins']) - len(fm_bad)}/{len(report['twins'])}")
    # a hit inside a scripts/test_*.py is a planted fixture (the C27 self-test writes its own
    # twin pair into a temp dir); anything else that names an orphan must be read before the
    # id is deleted.
    fixtures = [h for h in report["source_references"] if h.split(":")[0].startswith("scripts/test_")]
    # scripts/legacy/results/*.json are archived outputs of retired audit scripts (nothing in
    # the repo reads them); they are a historical record of the orphans, not a reference.
    legacy = [h for h in report["source_references"]
              if h.split(":")[0].startswith("scripts/legacy/")]
    other = [h for h in report["source_references"] if h not in fixtures and h not in legacy]
    report["source_references_fixtures"] = fixtures
    report["source_references_legacy"] = legacy
    report["source_references_other"] = other
    print(f"  references in source: {len(fixtures)} fixture (scripts/test_*), "
          f"{len(legacy)} legacy report, {len(other)} other")
    for h in other[:10]:
        print(f"    {h}")
    exact_total = 0
    for db, d in report["store_references"].items():
        exact_total += len(d["exact"])
        print(f"  references in {db}: {len(d['exact'])} exact, {d['prefix_only']} prefix-only "
              f"(the twin's own id contains the orphan's as a prefix)")
        for h in d["exact"][:5]:
            print(f"    {h}")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"  report: {a.json}")
    bad = len(not_equal) + len(other) + exact_total
    print("  SAFE TO DELETE: every twin's body matches, no exact reference anywhere"
          if not bad else f"  {bad} thing(s) to look at before deleting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
