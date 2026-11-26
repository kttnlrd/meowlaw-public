#!/usr/bin/env python3
"""CDN-0193 Phase 1 — act-generic writer for gate-ACCEPTED staged candidates.

`apply_itaa_table_fixes.py` is the itaa-1997 driver and re-extracts from the PDF
at write time. Every other act has no PDF-driven driver, and doesn't need one:
the reviewed bytes ARE the payload. This script copies the staged `output.md`
over the corpus file, so **what was gated is exactly what lands** — there is no
second extraction that could drift between gate and corpus.

It refuses to write unless (codex R4):

  * the section is staged and `READY` (gate ACCEPTED, artifacts intact, any
    risk flag explicitly reviewed against the current output hash);
  * the live corpus file still hashes to the staged `report.json`
    `source_sha256` — i.e. nobody edited that section since it was staged;
  * exactly one corpus file matches `<act>/sections/**/<section>.md`;
  * the resolved path is under `data/<act>/sections/`.

Every write appends a JSON line to `<stage root>/apply-log.jsonl` (act, section,
corpus path, pre/post sha256, staging dir, timestamp).

Usage
  python3 scripts/apply_staged_tables.py                 # dry run: what would land
  python3 scripts/apply_staged_tables.py --apply         # write
  python3 scripts/apply_staged_tables.py --act sis-1993 --apply
  python3 scripts/apply_staged_tables.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import table_rebuild_staging as STAGING  # noqa: E402

REPO = SCRIPTS.parent
DATA = REPO / "data"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def corpus_file(act: str, section: str) -> Path:
    """The single corpus file for `section`, refusing ambiguity."""
    root = DATA / act / "sections"
    if not root.is_dir():
        raise STAGING.StagingError(f"no sections tree for act {act!r} ({root})")
    hits = sorted(root.rglob(f"{section}.md"))
    hits = [h for h in hits if h.stem == section]
    if not hits:
        raise STAGING.StagingError(f"{act}: no corpus file for section {section}")
    if len(hits) > 1:
        raise STAGING.StagingError(
            f"{act}/{section}: {len(hits)} corpus files match — refusing "
            f"({', '.join(str(h.relative_to(REPO)) for h in hits)})")
    return hits[0]


def plan(root: str | Path | None = None, act: str | None = None) -> list[dict]:
    """One entry per staged candidate: READY or not, drifted or not."""
    root = STAGING.stage_root(root)
    out: list[dict] = []
    for key, res in STAGING.verify(root).items():
        a, sec = key.split("/", 1)
        if act and a != act:
            continue
        entry = {"key": key, "act": a, "section": sec, "status": res["status"],
                 "dir": res["dir"], "risks": res.get("risks", []),
                 "gate": res.get("gate"), "applyable": False, "reason": ""}
        if res["status"] != "READY":
            entry["reason"] = "; ".join(res.get("reasons", [])) or res["status"]
            out.append(entry)
            continue
        d = Path(res["dir"])
        rep = json.loads((d / "report.json").read_text())
        try:
            live = corpus_file(a, sec)
        except STAGING.StagingError as exc:
            entry["reason"] = str(exc)
            out.append(entry)
            continue
        entry["corpus"] = str(live.relative_to(REPO))
        live_sha = sha(live)
        entry["live_sha256"] = live_sha
        if live_sha != rep.get("source_sha256"):
            entry["reason"] = ("corpus file changed since staging "
                               f"(staged {str(rep.get('source_sha256'))[:12]}, live "
                               f"{live_sha[:12]}) — restage before applying")
            out.append(entry)
            continue
        entry["applyable"] = True
        entry["output_sha256"] = sha(d / "output.md")
        out.append(entry)
    return out


def apply_one(entry: dict, root: str | Path | None = None) -> dict:
    """Copy the reviewed staged output over the corpus file. Returns the log record."""
    if not entry.get("applyable"):
        raise STAGING.StagingError(f"{entry['key']}: not applyable ({entry['reason']})")
    d = Path(entry["dir"])
    stage_root = STAGING.stage_root(root)
    # Re-assert against the live tree immediately before the write: `plan` may
    # be seconds old, but READY/hash are the whole safety argument.
    STAGING.assert_ready(entry["act"], entry["section"], root)
    live = corpus_file(entry["act"], entry["section"])
    rep = json.loads((d / "report.json").read_text())
    pre = sha(live)
    if pre != rep.get("source_sha256"):
        raise STAGING.StagingError(f"{entry['key']}: live file changed since staging")
    payload = (d / "output.md").read_bytes()
    live.write_bytes(payload)
    post = sha(live)
    rec = {"applied_at": STAGING._now(), "key": entry["key"], "act": entry["act"],
           "section": entry["section"], "corpus": str(live), "pre_sha256": pre,
           "post_sha256": post, "staging_dir": str(d),
           "gate": entry.get("gate"), "risks": entry.get("risks", [])}
    with (stage_root / "apply-log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def selftest() -> int:
    """Prove the refusal paths on a throwaway tree, then a real write."""
    bad = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal bad
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:34s} {extra}")

    global DATA, REPO
    saved = (DATA, REPO)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        REPO = tmp / "repo"
        DATA = REPO / "data"
        sec_dir = DATA / "sis-1993" / "sections" / "part-1" / "division-1"
        sec_dir.mkdir(parents=True)
        live = sec_dir / "6.md"
        orig = ("---\nact: \"sis-1993\"\nsection: \"6\"\n---\n\n# 6  T\n\nprose\n\n"
                "| Item | A | B |\n| --- | --- | --- |\n| 1 | Part 2A ast | erisked APRA |\n")
        live.write_text(orig)
        cand = tmp / "cand.md"
        cand.write_text(orig.replace("| 1 | Part 2A ast | erisked APRA |", "| 1 | Part 2A | APRA |"))
        gate_ok = tmp / "g.json"
        gate_ok.write_text('{"verdict": "ACCEPTED", "gates": {"G1": {"pass": true}}}')
        stage_root = tmp / "stage"

        # 1. nothing staged -> refuse
        try:
            corpus_file("sis-1993", "6")
            check("corpus_file resolves", True)
        except STAGING.StagingError as exc:
            check("corpus_file resolves", False, str(exc))
        check("empty stage -> nothing applyable", plan(stage_root) == [])

        # 2. staged clean -> applyable, apply lands the reviewed bytes
        STAGING.stage("sis-1993", "6", live, cand, gate_report=gate_ok, root=stage_root)
        p = plan(stage_root)
        check("staged READY applyable", len(p) == 1 and p[0]["applyable"], json.dumps(p)[:160])
        rec = apply_one(p[0], root=stage_root)
        check("apply wrote reviewed bytes", live.read_text() == cand.read_text())
        check("log record hashes", rec["pre_sha256"] != rec["post_sha256"]
              and (stage_root / "apply-log.jsonl").is_file())

        # 3. corpus drift after staging -> refuse (note: reverting to the exact
        #    original is NOT drift — the staged source hash would still match,
        #    which is correct: applying would then be a no-op-equivalent write)
        live.write_text(orig.replace("prose\n", "prose edited after staging\n"))
        p = plan(stage_root)
        check("drift refused", len(p) == 1 and not p[0]["applyable"]
              and "changed since staging" in p[0]["reason"], p[0]["reason"])

        # 4. restage clean, then flag risk without review -> refuse; review -> applyable
        STAGING.stage("sis-1993", "6", live, cand, gate_report=gate_ok, root=stage_root, force=True)
        check("restage -> applyable again", plan(stage_root)[0]["applyable"])
        STAGING.stage("sis-1993", "6", live, cand, gate_report=gate_ok, root=stage_root,
                      force=True, flags=["row_joins"])
        check("unreviewed risk refused", not plan(stage_root)[0]["applyable"])
        STAGING.review("sis-1993", "6", by="selftest", root=stage_root)
        check("reviewed risk applyable", plan(stage_root)[0]["applyable"])

        # 5. ambiguous section -> refuse
        (sec_dir.parent / "division-2").mkdir(parents=True)
        dup = sec_dir.parent / "division-2" / "6.md"
        dup.write_text(orig)
        try:
            corpus_file("sis-1993", "6")
            check("ambiguous section refused", False)
        except STAGING.StagingError as exc:
            check("ambiguous section refused", "corpus files match" in str(exc))
        check("ambiguous not applyable", not plan(stage_root)[0]["applyable"])
        dup.unlink()
        check("sibling section ignored", corpus_file("sis-1993", "6").name == "6.md")

        # 6. a 6a.md must not satisfy a request for 6
        (sec_dir / "6a.md").write_text(orig)
        try:
            corpus_file("sis-1993", "6")
            check("6a not matched for 6", True)
        except STAGING.StagingError as exc:
            check("6a not matched for 6", False, str(exc))
    DATA, REPO = saved
    print("selftest:", "PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="staging root (default: the convention's)")
    ap.add_argument("--act", help="only this act")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.selftest:
        return selftest()

    entries = plan(a.root, a.act)
    if not entries:
        print(f"stage: nothing staged under {STAGING.stage_root(a.root)}")
        return 0
    ready = [e for e in entries if e["applyable"]]
    for e in entries:
        state = "APPLYABLE" if e["applyable"] else f"SKIP ({e['status']})"
        print(f"  {state:18s} {e['key']:28s} {e.get('corpus', '')}"
              + (f"  risks={','.join(e['risks'])}" if e["risks"] else "")
              + (f"\n      {e['reason']}" if e["reason"] else ""))
    print(f"\n{len(ready)}/{len(entries)} applyable")
    if not a.apply:
        print("dry run — nothing written (pass --apply)")
        return 0
    if not ready:
        print("nothing to apply")
        return 1
    for e in ready:
        rec = apply_one(e, a.root)
        print(f"applied {rec['key']} -> {rec['corpus']}  {rec['pre_sha256'][:12]} -> {rec['post_sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
