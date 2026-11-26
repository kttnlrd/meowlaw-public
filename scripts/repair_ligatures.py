#!/usr/bin/env python3.12
"""E-c: typographic ligatures (U+FB00-U+FB06) never NFKC-normalised.

Root cause (docs/bug-plan-2026-09-25.md, E-c):
  Nothing in the pipeline normalised with NFKC, so the PDFs' ligatures stayed in the served
  text.  The backend hides it for the UI and `sections_fts` behind a fold table
  (`backend/services/data_loader.py:31-43`), but every RAW path reads the file: MCP
  `get_section` (`fastmcp_server.py:1030`), the embeddings (`scripts/embed_legislation.py:270`)
  and `insolvency_fts` (`backend/services/search_service.py:243`).  In Keays, "ﬁnancial" is a
  separate token, so searching "financial" missed 10 of the 21 chapters.
  Commit 1f8c551ac NFKC-renamed the MTG section FILES and tree ids but not
  `section_index.json`, leaving 193 stale ligature ids (the CDN-0206 fallback gap).

Rule applied: the same targeted mapping the producers use (`pipeline/text_normalize.py`,
`nfkc_ligatures`) - `unicodedata.normalize('NFKC', c)` for U+FB00-U+FB06 and NOTHING else, so
superscripts and fractions in the formulas are untouched.  Applying full NFKC to the corpus
would rewrite them, which is why the restriction is in one shared function.

Measured before this repair (2026-09-26): 18,688 ligatures in 1,315 MTG section files,
404 in `section_index.json` (193 ids, 202 characters each in ids and titles), 3,107 in 21
Keays chapters, 13 in 5 `data/maps/*.json` commentary slugs = 22,212 (the plan's figure).
The 13 map slugs are included here on purpose: each is a commentary reference to an MTG
section whose file was already NFKC-renamed, so they are dangling links that the same
mapping repairs (`cgt-cost-base-modiﬁcations-for-leases` ->
`...modifications-for-leases`, a file that exists on disk).

Dry run by default.  --apply writes in blocks and records every changed file and JSON value;
--verify RE-DERIVES each file (re-applies the mapping to the HEAD bytes) and asserts the
link-integrity the repair is for: MTG section_index ids == tree.json ids == the file names
on disk.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# The producer's own mapping, imported and never forked, so a repair cannot diverge from
# the pipeline rule (`pipeline/text_normalize.py`).
sys.path.insert(0, str(ROOT / "pipeline"))
from text_normalize import nfkc_ligatures  # noqa: E402

TICKET = "e-c-ligatures"
PROVENANCE_DIR = ROOT / "docs" / "provenance"

# (label, path, glob or None).  A None glob means "the file itself".
TARGETS: list[tuple[str, Path, str | None]] = [
    ("MTG section markdown", DATA / "master-tax-guide" / "sections", "**/*.md"),
    ("Keays chapter markdown", DATA / "insolvency-keays" / "chapters", "*.md"),
    ("MTG section_index (ids and titles)", DATA / "master-tax-guide" / "section_index.json", None),
    ("map node ids / commentary slugs", DATA / "maps", "*.json"),
]
# Deliberately NOT touched: data/keays-raw.txt (raw source, not served) and
# data/master-tax-guide/tree.json (measured 2026-09-26: already NFKC-normalised, 0 ligatures).

LIGATURE = re.compile(r"[\uFB00-\uFB06]")


def files() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for label, path, glob in TARGETS:
        if glob is None:
            if path.exists():
                out.append((label, path))
            continue
        out += [(label, p) for p in sorted(path.rglob(glob)) if p.is_file()]
    return out


def expansion(text: str) -> int:
    """How many bytes the mapping changes: a ligature is 3 bytes of UTF-8 and expands to
    2 or 3 ASCII bytes, so the delta is -1 or 0 per ligature."""
    return sum(len(unicodedata.normalize("NFKC", c).encode()) - len(c.encode())
               for c in LIGATURE.findall(text))


def head_text(p: Path) -> str:
    r = subprocess.run(["git", "show", f"HEAD:{p.relative_to(ROOT)}"], cwd=ROOT,
                       capture_output=True, text=True)
    return r.stdout


def json_values(text: str) -> dict[str, str]:
    """Every string leaf of a JSON document, keyed by a stable JSON path."""
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            yield path, node
    return dict(walk(json.loads(text)))


def plan() -> list[dict]:
    """-> [{file, label, head, new, ligatures, changed_values}] for files that change."""
    out = []
    for label, p in files():
        head = head_text(p)
        cur = p.read_text(encoding="utf-8")
        if cur != head:
            raise SystemExit(f"{p.relative_to(ROOT)}: on disk differs from HEAD - refusing "
                             f"(commit or revert first)")
        n = len(LIGATURE.findall(head))
        if not n:
            continue
        new = nfkc_ligatures(head)
        changed_values = []
        if p.suffix == ".json":
            hv, nv = json_values(head), json_values(new)
            changed_values = [{"path": k, "old": hv[k], "new": nv[k]}
                              for k in hv if hv[k] != nv.get(k)]
            if json.loads(new) is None:
                raise SystemExit(f"{p.relative_to(ROOT)}: the mapped text is not valid JSON")
        out.append({"file": str(p.relative_to(ROOT)), "label": label, "head": head, "new": new,
                    "ligatures": n, "changed_values": changed_values})
    return out


def scan() -> None:
    items = plan()
    by_label: dict[str, list[int]] = {}
    for it in items:
        b = by_label.setdefault(it["label"], [0, 0])
        b[0] += 1
        b[1] += it["ligatures"]
    for label, (n, c) in sorted(by_label.items()):
        print(f"  {label}: {n} file(s), {c} ligature(s)")
    print(f"  total: {len(items)} file(s), {sum(i['ligatures'] for i in items)} ligature(s), "
          f"{sum(expansion(i['head']) for i in items)} byte(s) delta")
    json_changed = sum(len(i["changed_values"]) for i in items)
    print(f"  JSON values that change: {json_changed}")
    print("  (dry run - nothing written; --apply to write)")


def rollback_tag() -> tuple[str, str]:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    tag = f"{TICKET}-apply-rollback-{head}" if head else f"{TICKET}-apply-rollback-nogit"
    if head:
        subprocess.run(["git", "tag", "-f", tag], cwd=ROOT, check=True,
                       capture_output=True, text=True)
    return head, tag


def apply(block_size: int) -> int:
    items = plan()
    if not items:
        print("  nothing to do - no tag, no provenance file")
        return 0
    head, tag = rollback_tag()
    print(f"  rollback point: tag {tag} (HEAD {head or 'unknown'}); "
          f"git checkout -- <path> also reverts these tracked files")
    records = []
    for n, it in enumerate(items, 1):
        p = ROOT / it["file"]
        p.write_text(it["new"], encoding="utf-8")
        records.append({"file": it["file"], "label": it["label"],
                        "ligatures_removed": it["ligatures"],
                        "bytes_before": len(it["head"].encode()),
                        "bytes_after": len(it["new"].encode()),
                        "changed_values": it["changed_values"]})
        if n % block_size == 0:
            print(f"    block: {n}/{len(items)} files written")
    PROVENANCE_DIR.mkdir(parents=True, exist_ok=True)
    log = PROVENANCE_DIR / f"{TICKET}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    log.write_text(json.dumps({
        "ticket": TICKET, "script": str(Path(__file__).name),
        "rollback_tag": tag, "head": head,
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mapping": "unicodedata.normalize('NFKC', c) for U+FB00-U+FB06 only",
        "untouched_by_design": ["data/keays-raw.txt (raw source, not served)",
                                "data/master-tax-guide/tree.json (already NFKC, 0 ligatures)"],
        "files": records}, indent=1, ensure_ascii=False))
    print(f"  wrote {len(records)} file(s); provenance at {log.relative_to(ROOT)}")
    return 0


def latest_log() -> Path | None:
    if not PROVENANCE_DIR.is_dir():
        return None
    runs = sorted(PROVENANCE_DIR.glob(f"{TICKET}-*.json"))
    return runs[-1] if runs else None


def verify(log: Path | None = None) -> int:
    """Re-derive every written file from its HEAD bytes, then check link integrity."""
    log = log or latest_log()
    if log is None or not log.exists():
        print(f"  no provenance file under {PROVENANCE_DIR} - nothing to verify")
        return 1
    print(f"  verifying against {log.name}")
    data = json.loads(log.read_text())
    bad = 0
    for rec in data["files"]:
        p = ROOT / rec["file"]
        head = head_text(p)
        cur = p.read_text(encoding="utf-8")
        redone = nfkc_ligatures(head)
        if redone != cur:
            print(f"  {rec['file']}: current bytes are not the mapping of HEAD"); bad += 1
        if LIGATURE.search(cur):
            print(f"  {rec['file']}: ligature(s) still present"); bad += 1
        want = len(head.encode()) + expansion(head)
        if len(cur.encode()) != want:
            print(f"  {rec['file']}: {len(cur.encode())} bytes, expected {want}"); bad += 1
        if rec["ligatures_removed"] != len(LIGATURE.findall(head)):
            print(f"  {rec['file']}: logged {rec['ligatures_removed']} ligature(s), "
                  f"HEAD has {len(LIGATURE.findall(head))}"); bad += 1
    # link integrity - this repair's point: the ids must now MATCH the files and the tree
    idx = json.loads((DATA / "master-tax-guide" / "section_index.json").read_text())
    tree = json.loads((DATA / "master-tax-guide" / "tree.json").read_text())
    ids = {e["id"] for e in idx}
    tids = {s["id"] for part in tree["parts"] for s in part["sections"]}
    stems = {p.stem for p in (DATA / "master-tax-guide" / "sections").rglob("*.md")}
    for name, diff in (("ids not in tree.json", ids - tids),
                       ("ids with no file on disk", ids - stems),
                       ("tree ids not in section_index", tids - ids)):
        print(f"  {name}: {len(diff)}" + (f" e.g. {sorted(diff)[:3]}" if diff else ""))
        bad += len(diff)
    left = sum(len(LIGATURE.findall((DATA / "master-tax-guide" / "sections" / f"{i}.md")
                                    .read_text(errors="replace")))
               for i in ids if (DATA / "master-tax-guide" / "sections" / f"{i}.md").exists())
    print(f"  ligatures left in the sections the index points at: {left}")
    bad += left
    print("  VERIFIED: every file re-derives, no ligature left, ids match tree and disk"
          if not bad else f"  {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--log", help="provenance file to verify against (default: the newest)")
    ap.add_argument("--block-size", type=int, default=200)
    a = ap.parse_args()
    if a.verify:
        sys.exit(verify(Path(a.log) if a.log else None))
    if a.apply:
        sys.exit(apply(a.block_size))
    scan()
