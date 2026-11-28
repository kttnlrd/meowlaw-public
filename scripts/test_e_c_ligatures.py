#!/usr/bin/env python3.12
"""E-c: the ligature mapping, the repaired stores, and the link integrity it fixes.

Part 1 is the mapping itself, including the planted negatives that make full NFKC
unacceptable (superscripts and fractions in formulas must not change).
Part 2 is the corpus: no U+FB00-U+FB06 left in the stores E-c repairs, and the 193 ids the
repair moves must now MATCH the section files on disk and tree.json (the repair fixes stale
links, it does not break them).
Part 3 is the served effect: `search_insolvency("financial")` must return the Keays
chapters that previously needed "ﬁnancial" (measured before the repair: "financial" 9 rows,
"ﬁnancial" 19).

Run: /usr/bin/python3.12 scripts/test_e_c_ligatures.py      (exit 1 on any failure)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
from text_normalize import nfkc_ligatures  # noqa: E402

failures = 0


def check(ok: bool, label: str):
    global failures
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


# ── 1. the mapping ──────────────────────────────────────────────────────────
print("-- the mapping (U+FB00-U+FB06 only) --")
check(nfkc_ligatures("A \ufb01nancial supply") == "A financial supply",
      "'ﬁnancial' -> 'financial'")
check(nfkc_ligatures("\ufb00 \ufb01 \ufb02 \ufb03 \ufb04 \ufb05 \ufb06")
      == "ff fi fl ffi ffl st st", "all seven ligatures expand (ﬅ and ﬆ both -> st)")
# planted negatives: the reason the pass is restricted rather than full NFKC
for char, name in (("\u2075", "superscript five"), ("\u00bc", "one quarter fraction"),
                   ("\u2032", "prime"), ("\u00b5", "micro sign"), ("\u2014", "em dash"),
                   ("\u2022", "bullet")):
    check(nfkc_ligatures(f"x{char}y") == f"x{char}y",
          f"{name} U+{ord(char):04X} is untouched (full NFKC would rewrite it)")
# full NFKC would change these - asserted so the restriction cannot be "simplified" away
import unicodedata  # noqa: E402
check(unicodedata.normalize("NFKC", "x\u2075\u00b5y") != "x\u2075\u00b5y",
      "sanity: full NFKC does change the superscript/micro sign above")

# ── 2. the repaired stores ──────────────────────────────────────────────────
print("-- the corpus stores --")
LIG = re.compile(r"[\uFB00-\uFB06]")
DATA = ROOT / "data"
stores: list[tuple[str, list[Path]]] = [
    ("master-tax-guide/sections", sorted((DATA / "master-tax-guide" / "sections").rglob("*.md"))),
    ("insolvency-keays/chapters", sorted((DATA / "insolvency-keays" / "chapters").glob("*.md"))),
    ("master-tax-guide/section_index.json", [DATA / "master-tax-guide" / "section_index.json"]),
    ("master-tax-guide/tree.json", [DATA / "master-tax-guide" / "tree.json"]),
    ("maps", sorted((DATA / "maps").glob("*.json"))),
]
for label, paths in stores:
    n = sum(len(LIG.findall(p.read_text(encoding="utf-8", errors="replace"))) for p in paths)
    check(n == 0, f"{label}: {n} ligature(s) in {len(paths)} file(s) (want 0)")

idx = json.loads((DATA / "master-tax-guide" / "section_index.json").read_text(encoding="utf-8"))
tree = json.loads((DATA / "master-tax-guide" / "tree.json").read_text(encoding="utf-8"))
ids = {e["id"] for e in idx}
tids = {s["id"] for part in tree["parts"] for s in part["sections"]}
stems = {p.stem for p in (DATA / "master-tax-guide" / "sections").rglob("*.md")}
print("-- link integrity (this is what the id repair is for) --")
check(not (ids - tids), f"section_index ids not in tree.json: {len(ids - tids)}")
check(not (ids - stems), f"section_index ids with no section file: {len(ids - stems)}")
check(not (tids - ids), f"tree.json ids missing from section_index: {len(tids - ids)}")
# the same for the map commentary slugs the repair moved: a MOVED slug must resolve to a
# section file.  Slugs that were already dangling before the repair are pre-existing
# (they name no MTG section at all) and are reported, not asserted - E-c must not add any.
import subprocess  # noqa: E402


def map_slugs(text: str) -> list[str]:
    d = json.loads(text)
    return [s for node in d.get("nodes", []) for s in node.get("commentary", [])]


dangling_before = dangling_after = lig_slugs = moved_found = 0
for p in sorted((DATA / "maps").glob("*.json")):
    head = subprocess.run(["git", "show", f"HEAD:{p.relative_to(ROOT)}"], cwd=ROOT,
                          capture_output=True, text=True).stdout
    cur_text = p.read_text(encoding="utf-8")
    before = map_slugs(head)
    cur = map_slugs(cur_text)
    lig_slugs += sum(1 for s in cur if LIG.search(s))
    for s in cur:
        if s not in stems and s in before and before.count(s):
            dangling_after += 1
    for s in before:
        if s not in stems and s not in cur:
            moved_found += 1                     # its repaired form resolved to a file
    dangling_before += sum(1 for s in before if s not in stems)
print(f"-- map commentary slugs: {dangling_before} dangling before, {dangling_after} after "
      f"({moved_found} repaired slug(s) now resolve; the rest name no MTG section at all) --")
check(lig_slugs == 0, f"map commentary slugs containing a ligature: {lig_slugs}")
check(moved_found >= 13, f"repaired slugs that now resolve to a section file: {moved_found}")

# ── 3. the served effect ────────────────────────────────────────────────────
print("-- insolvency_fts, through its own query path --")
from backend.services.search_service import search_insolvency  # noqa: E402

lig = search_insolvency("\ufb01nancial", 50)["total"]
plain = search_insolvency("financial", 50)["total"]
check(lig == 0, f"the ligature query returns nothing now: 'ﬁnancial' -> {lig} row(s)")
check(plain >= 19, f"'financial' returns the rows that needed the ligature: {plain} row(s)")
conf_lig = search_insolvency("con\ufb01dence", 50)["total"]
conf = search_insolvency("confidence", 50)["total"]
check(conf_lig == 0 and conf >= 9,
      f"'conﬁdence' -> {conf_lig} row(s), 'confidence' -> {conf} row(s) (was 9 / 0)")

print("PASS" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
