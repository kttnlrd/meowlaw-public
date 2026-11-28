#!/usr/bin/env python3
"""CDN-0193 Phase 0 — the offline-staging convention (codex R4).

A rebuilt table is a **candidate** until a human says otherwise.  This script
is the only sanctioned route from candidate to live corpus, and it enforces
what codex R4 (and the plan's Phase-0 disposition table) asked for:

  * candidates live OUTSIDE the repo worktree (default
    ../legislation-explorer-staging/table-rebuild/), so a bad candidate cannot
    be served by the API, the site build, or a corpus scan;
  * every candidate carries four artifacts — the original section, the
    proposed replacement, a unified diff, and a hash-pinned report — plus a
    fidelity gate's verdict, either an in-place TABLE rebuild
    (scripts/table_rebuild_gate.py --json, gates G0-G6) or a whole-section
    RE-INGEST (scripts/ingest_reingest_gate.py --json, gates R1-R6).  The kind
    is auto-detected from the report's gate names; --gate-kind asserts it;
  * a candidate carrying anything a machine cannot prove safe (row joins,
    formula glyphs, changed row count, merged cells, ambiguous page mapping)
    is BLOCKED until an explicit review record is written against that exact
    output hash — re-staging invalidates the approval;
  * a WHOLE-SECTION re-ingest is certified against the SOURCE PDF, not against
    the corpus file it replaces, so for a reingest report the corpus-diff
    heuristics below (row counts, blockquote joins, stray glyphs) are NOT
    risk signals — a replaced body is the point of a re-ingest, and R1/R2/R3
    already prove nothing was lost, nothing invented, and no glyph sits in a
    pipe.  The one thing the gate cannot prove is what a preserved region
    MEANT, so needs_review -> the 'preserved_regions' risk -> BLOCKED;
  * the apply driver refuses to write a corpus file unless the section is
    READY here (scripts/apply_itaa_table_fixes.py).

Statuses
  READY    gate ACCEPTED, artifacts intact, risky files reviewed
  BLOCKED  needs an explicit review (or the review no longer matches)
  FAIL     artifact missing/tampered, gate REJECTED, or no gate verdict

Usage
  python3.12 scripts/table_rebuild_staging.py stage --act itaa-1997 \\
      --section 115-30 --source <corpus .md> --output <candidate .md> \\
      [--gate-report gate.json] [--gate-kind table|reingest] \\
      [--flag ambiguous_page_mapping]
  python3.12 scripts/table_rebuild_staging.py stage-run --act itaa-1997 \\
      --gate-json BATCH.json|DIR [--corpus-root data/itaa-1997] \\
      [--summary summary.json]        # whole-run, one candidate per section
  python3.12 scripts/table_rebuild_staging.py verify [--root DIR]
  python3.12 scripts/table_rebuild_staging.py list   [--root DIR]
  python3.12 scripts/table_rebuild_staging.py review --act A --section S --by WHO [--note TEXT]
  python3.12 scripts/table_rebuild_staging.py --selfcheck

Stdlib only.  Import as a library: stage_root(), stage(), verify(),
apply_list(), assert_ready().
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "data"
ENV_ROOT = "TABLE_REBUILD_STAGE"
CONVENTION = "offline-staging v1 (codex R4, CDN-0193 Phase 0)"
ARTIFACTS = ("source.md", "output.md", "diff.txt")

RISK_FLAGS = {
    "row_joins",              # mangled fragments folded into a row
    "formula_glyphs",         # formula-font junk in the output
    "changed_row_count",      # unique data rows added/lost vs the original
    "merged_cells",           # multi-line cell rejoin
    "ambiguous_page_mapping", # section pages not unambiguously located
    "multi_page",             # table spans a page break
    "preserved_regions",      # re-ingest kept a region verbatim (needs_review)
}

GATE_KINDS = {"G": "table", "R": "reingest"}   # by the report's gate-name prefix

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class StagingError(RuntimeError):
    """The convention was violated; nothing was written."""


# ── root / names ────────────────────────────────────────────────────────────
def default_root() -> Path:
    return Path(os.environ.get(ENV_ROOT)
                or REPO.parent / "legislation-explorer-staging" / "table-rebuild")


def stage_root(root: str | Path | None = None) -> Path:
    """Resolve the staging root, refusing anything inside the repo worktree."""
    r = Path(root).expanduser() if root else default_root()
    r = r.resolve()
    if r == REPO or REPO in r.parents:
        raise StagingError(
            f"staging root {r} is inside the repo worktree ({REPO}) — rebuild "
            f"outputs must be staged outside the live corpus (codex R4)")
    return r


def _safe(name: str, what: str) -> str:
    if not name or not _NAME_RE.match(name) or "/" in name or ".." in name:
        raise StagingError(f"unsafe {what} name: {name!r}")
    return name


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── risk heuristics (indicative, not a proof of safety) ─────────────────────
def _rows(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("|") and s.count("|") >= 2 and "---" not in s:
            out.append(s)
    return out


def _unique_rows(text: str) -> list[str]:
    """Table rows with verbatim repeats collapsed.

    Page-split corruption repeats header rows and rows verbatim, so collapsing
    those is provably lossless and must not be what raises a review demand.
    """
    seen: set[str] = set()
    out = []
    for row in _rows(text):
        if row not in seen:
            seen.add(row)
            out.append(row)
    return out


def _is_formula_glyph(c: str) -> bool:
    # ASCII punctuation is not junk ('|' is category Sm); junk is non-ASCII.
    return ord(c) > 127 and (unicodedata.category(c) in {"Sm", "So", "Sk"} or c in "´`ˆ˜¨")


def _check_flags(extra) -> list[str]:
    for f in extra:
        if f not in RISK_FLAGS:
            raise StagingError(f"unknown --flag {f!r}; known: {', '.join(sorted(RISK_FLAGS))}")
    return sorted(set(extra))


def _risks(src_text: str, out_text: str, extra=()) -> tuple[list[str], dict]:
    risks: set[str] = set()
    detail: dict = {}
    src_rows, out_rows = _unique_rows(src_text), _unique_rows(out_text)
    if len(src_rows) != len(out_rows):
        risks.add("changed_row_count")
        detail["changed_row_count"] = {"source_rows": len(src_rows), "output_rows": len(out_rows)}
    fragments = [ln for ln in src_text.splitlines() if ln.lstrip().startswith(">")]
    dropped = Counter(src_rows) - Counter(out_rows)
    if fragments and dropped:
        risks.add("row_joins")
        detail["row_joins"] = {"blockquote_fragments": len(fragments),
                               "rows_not_carried": sum(dropped.values())}
    glyphs = sorted({c for c in out_text if _is_formula_glyph(c)})
    if glyphs:
        risks.add("formula_glyphs")
        detail["formula_glyphs"] = glyphs
    risks.update(_check_flags(extra))
    return sorted(risks), detail


def read_gate(rep: dict, where: str) -> str:
    """The gate family that wrote this report, from its gate names."""
    names = list((rep.get("gates") or {}).keys())
    kinds = {GATE_KINDS.get(n[:1]) for n in names} - {None}
    if len(kinds) != 1:
        raise StagingError(
            f"{where}: cannot tell which gate wrote this report from its gate "
            f"names {names} — expected G* (table rebuild) or R* (re-ingest)")
    return kinds.pop()


# ── stage ───────────────────────────────────────────────────────────────────
def stage(act: str, section: str, source: str | Path, output: str | Path,
          gate_report: str | Path | None = None, flags=(), root: str | Path | None = None,
          force: bool = False, gate_kind: str | None = None) -> dict:
    """Stage a candidate: write source/output/diff/report outside the worktree."""
    act, section = _safe(act, "act"), _safe(section, "section")
    root = stage_root(root)
    out = Path(output).expanduser().resolve()
    if out == REPO or REPO in out.parents:
        raise StagingError(f"candidate {out} is inside the worktree — stage it outside")
    src = Path(source).expanduser() if source is not None else None
    if src is not None:
        if not src.is_file():
            raise StagingError(f"source not found: {src}")
        src = src.resolve()
    if not out.is_file():
        raise StagingError(f"candidate not found: {out}")

    d = root / act / section
    if d.exists() and any(d.iterdir()) and not force:
        raise StagingError(f"{act}/{section} already staged at {d} — pass --force to restage "
                           f"(re-staging discards any review)")

    src_text = src.read_text(errors="replace") if src is not None else ""
    out_text = out.read_text(errors="replace")
    diff = "".join(difflib.unified_diff(
        src_text.splitlines(keepends=True), out_text.splitlines(keepends=True),
        fromfile=f"corpus/{act}/{section}", tofile=f"staged/{act}/{section}"))

    gate = {"verdict": None, "kind": None, "path": None, "gates_failed": []}
    flags = list(flags)
    if gate_report:
        gp = Path(gate_report).expanduser()
        if not gp.is_file():
            raise StagingError(f"gate report not found: {gp}")
        rep = json.loads(gp.read_text())
        kind = read_gate(rep, str(gp))
        if gate_kind and gate_kind != kind:
            raise StagingError(f"{gp} is a {kind} gate report, but --gate-kind "
                               f"{gate_kind} was asserted")
        # A re-ingest that is lossless and invention-free but kept a preserved
        # region is ACCEPTED-with-needs_review: the text is proven safe, its
        # STRUCTURE is not.  That is exactly this convention's definition of a
        # risky file, so it becomes a risk flag and BLOCKs pending review.
        if kind == "reingest" and rep.get("needs_review") and \
                "preserved_regions" not in flags:
            flags.append("preserved_regions")
        gate = {"verdict": rep.get("verdict"), "kind": kind,
                "path": str(gp.resolve()), "sha256": _sha(gp),
                "needs_review": rep.get("needs_review"),
                "gates_failed": [k for k, v in (rep.get("gates") or {}).items()
                                 if not v.get("pass")]}

    if gate["kind"] == "reingest":
        # The certificate compared the candidate against the SOURCE PDF, so the
        # corpus file is not the reference and differing from it is the point.
        # Only what the gate itself could not prove (a preserved region) and
        # what the operator explicitly asserted are risks here.
        risks, detail = _check_flags(flags), {}
    else:
        risks, detail = _risks(src_text, out_text, extra=flags)

    d.mkdir(parents=True, exist_ok=True)
    (d / "source.md").write_text(src_text)
    (d / "output.md").write_text(out_text)
    (d / "diff.txt").write_text(diff)
    report = {
        "convention": CONVENTION,
        "act": act,
        "section": section,
        "staged_at": _now(),
        "source_path": str(src) if src else None,
        "source_sha256": _sha(src) if src else None,
        "artifacts": {name: {"sha256": _sha(d / name), "bytes": (d / name).stat().st_size}
                      for name in ARTIFACTS},
        "gate": gate,
        "risks": risks,
        "risk_detail": detail,
    }
    (d / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    review = d / "review.json"
    if review.exists():
        review.unlink()  # a re-stage invalidates any prior approval
    return report


def review(act: str, section: str, by: str, note: str = "",
           root: str | Path | None = None) -> dict:
    """Write the explicit review record, pinned to the current output hash."""
    act, section = _safe(act, "act"), _safe(section, "section")
    d = stage_root(root) / act / section
    if not (d / "report.json").is_file():
        raise StagingError(f"{act}/{section} is not staged at {d}")
    rec = {"approved_by": by, "approved_at": _now(), "note": note,
           "output_sha256": _sha(d / "output.md")}
    (d / "review.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return rec


# ── whole-run staging (one command for a whole compilation) ─────────────────
def load_gate_reports(path: str | Path) -> list[tuple[dict, Path]]:
    """Every gate report under a file or directory, as (report, file) pairs.

    ingest_reingest_gate writes one dict for a single section and a list for a
    batch; both forms, and a directory of either, are accepted.
    """
    p = Path(path).expanduser()
    files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
    if not files:
        raise StagingError(f"no gate reports under {p}")
    out: list[tuple[dict, Path]] = []
    for f in files:
        data = json.loads(f.read_text())
        for rep in (data if isinstance(data, list) else [data]):
            out.append((rep, f))
    return out


def corpus_index(corpus_root: Path) -> dict[str, Path]:
    """section id -> live corpus file, so a run need not be told 4,649 paths."""
    idx: dict[str, Path] = {}
    for md in corpus_root.rglob("*.md"):
        idx.setdefault(md.stem, md)
    return idx


def stage_run(act: str, gate_json: str | Path, corpus_root: str | Path | None = None,
              root: str | Path | None = None, summary: str | Path | None = None) -> dict:
    """Stage a whole re-ingest run: one candidate per gated section.

    Each section's certificate is its own ingest_reingest_gate report, copied
    into the candidate directory (so the candidate is self-contained) and
    hash-pinned by the staging report exactly as the per-table path does.
    """
    act = _safe(act, "act")
    root = stage_root(root)
    croot = Path(corpus_root).expanduser() if corpus_root else CORPUS / act
    index = corpus_index(croot) if croot.is_dir() else {}
    rows: list[dict] = []
    for rep, f in load_gate_reports(gate_json):
        section = rep.get("section")
        if not section:
            raise StagingError(f"{f}: gate report has no 'section'")
        kind = read_gate(rep, f"{f} [{section}]")
        if kind != "reingest":
            raise StagingError(
                f"{f} [{section}] is a {kind} gate report — stage-run stages "
                f"whole-section re-ingests; use 'stage' for a table rebuild")
        out = Path(rep.get("ingested") or "")
        if not out.is_file():
            raise StagingError(f"{f} [{section}]: gated file {out} is gone — "
                               f"the certificate no longer describes anything")
        d = root / act / _safe(section, "section")
        d.mkdir(parents=True, exist_ok=True)
        gp = d / "gate.json"
        gp.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
        src = index.get(section)
        staged = stage(act, section, src, out, gate_report=gp, root=root, force=True)
        res = _inspect(d)
        rows.append({"section": section, "status": res["status"],
                     "gate_verdict": rep.get("verdict"),
                     "needs_review": rep.get("needs_review"),
                     "risks": staged["risks"],
                     "new_section": src is None,
                     "reasons": res.get("reasons", []),
                     "dir": str(d)})

    hist = Counter(r["status"] for r in rows)
    reasons: Counter = Counter()
    for r in rows:
        if r["status"] == "BLOCKED":
            reasons[",".join(r["risks"]) or "unknown"] += 1
        elif r["status"] == "FAIL":
            reasons[r["reasons"][0] if r["reasons"] else "unknown"] += 1
    out = {"convention": CONVENTION, "act": act, "staged_at": _now(),
           "root": str(root), "corpus_root": str(croot),
           "gate_json": str(Path(gate_json).expanduser()),
           "sections": len(rows), "histogram": dict(hist),
           "reasons": dict(reasons),
           "new_sections": sum(r["new_section"] for r in rows),
           "results": sorted(rows, key=lambda r: r["section"])}
    if summary:
        Path(summary).expanduser().write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return out


# ── verify ──────────────────────────────────────────────────────────────────
def _inspect(d: Path) -> dict:
    reasons: list[str] = []
    rep_path = d / "report.json"
    if not rep_path.is_file():
        return {"status": "FAIL", "reasons": ["report.json missing"], "risks": []}
    try:
        rep = json.loads(rep_path.read_text())
    except Exception as exc:
        return {"status": "FAIL", "reasons": [f"report.json unparseable: {exc}"], "risks": []}
    for name in ARTIFACTS:
        if not (d / name).is_file():
            reasons.append(f"{name} missing")
    if reasons:
        return {"status": "FAIL", "reasons": reasons, "risks": rep.get("risks", [])}
    for name in ARTIFACTS:
        want = (rep.get("artifacts") or {}).get(name, {}).get("sha256")
        if want and _sha(d / name) != want:
            reasons.append(f"{name} sha256 mismatch (tampered after staging)")
    verdict = (rep.get("gate") or {}).get("verdict")
    if verdict != "ACCEPTED":
        reasons.append(f"gate verdict {verdict!r} — not ACCEPTED")
    if reasons:
        return {"status": "FAIL", "reasons": reasons, "risks": rep.get("risks", []),
                "gate": verdict}

    risks = rep.get("risks") or []
    rev_path = d / "review.json"
    if risks:
        if not rev_path.is_file():
            return {"status": "BLOCKED", "risks": risks, "gate": verdict,
                    "reasons": [f"risky ({', '.join(risks)}) and no review.json"]}
        try:
            rev = json.loads(rev_path.read_text())
        except Exception as exc:
            return {"status": "BLOCKED", "risks": risks, "gate": verdict,
                    "reasons": [f"review.json unparseable: {exc}"]}
        if rev.get("output_sha256") != _sha(d / "output.md"):
            return {"status": "BLOCKED", "risks": risks, "gate": verdict,
                    "reasons": ["review.json does not match the current output (stale approval)"]}
        return {"status": "READY", "risks": risks, "gate": verdict,
                "reviewed_by": rev.get("approved_by")}
    return {"status": "READY", "risks": [], "gate": verdict}


def verify(root: str | Path | None = None) -> dict:
    root = stage_root(root)
    results: dict[str, dict] = {}
    if not root.exists():
        return results
    for d in sorted(p for p in root.glob("*/*") if p.is_dir()):
        key = f"{d.parent.name}/{d.name}"
        results[key] = _inspect(d)
        results[key]["dir"] = str(d)
    return results


def apply_list(root: str | Path | None = None) -> list[str]:
    """Sections the apply driver may touch: READY only."""
    return sorted(k for k, v in verify(root).items() if v["status"] == "READY")


def assert_ready(act: str, section: str, root: str | Path | None = None) -> dict:
    """Raise unless this section is staged READY. Used by the apply driver."""
    key = f"{_safe(act, 'act')}/{_safe(section, 'section')}"
    res = verify(root).get(key)
    if res is None:
        raise StagingError(
            f"{key} is not staged under {stage_root(root)} — stage it with "
            f"table_rebuild_staging.py before writing the corpus (codex R4)")
    if res["status"] != "READY":
        raise StagingError(f"{key} is {res['status']}: {'; '.join(res.get('reasons', []))}")
    return res


# ── CLI ─────────────────────────────────────────────────────────────────────
def _print_verify(results: dict) -> int:
    if not results:
        print("stage: (empty — nothing staged)")
        return 0
    code = 0
    for key, res in results.items():
        if res["status"] == "FAIL":
            code = 1
        elif res["status"] == "BLOCKED" and code == 0:
            code = 2
        print(f"  {res['status']:7s} {key:44s} gate={res.get('gate') or 'absent'}"
              + (f" risks={','.join(res['risks'])}" if res.get("risks") else "")
              + (f" reviewed_by={res['reviewed_by']}" if res.get("reviewed_by") else ""))
        for r in res.get("reasons", []):
            print(f"          - {r}")
    return code


def selfcheck() -> int:
    """Prove the convention fires: refusals, tamper detection, review pinning."""
    bad = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal bad
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:26s} {extra}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = tmp / "stage"
        src = tmp / "orig.md"
        good_src = ("---\nsection: \"6\"\ncompilation_no: \"126\"\n---\n\n# 6  T\n\nprose line\n\n"
                    "| Item | Provision | Regulator |\n| --- | --- | --- |\n"
                    "| 1 | Part 2A ast | erisked APRA |\n")
        good_out = good_src.replace("| 1 | Part 2A ast | erisked APRA |", "| 1 | Part 2A | APRA |")
        src.write_text(good_src)
        out = tmp / "cand.md"
        out.write_text(good_out)
        gate_ok = tmp / "gate-ok.json"
        gate_ok.write_text(json.dumps({"verdict": "ACCEPTED", "gates": {"G1": {"pass": True}}}))
        gate_no = tmp / "gate-no.json"
        gate_no.write_text(json.dumps({"verdict": "REJECTED", "gates": {"G1": {"pass": False}}}))

        # 1. stage root inside the worktree is refused
        try:
            stage("itaa-1997", "6", src, out, root=REPO / "data" / "scratch")
            check("refuse in-repo root", False)
        except StagingError as exc:
            check("refuse in-repo root", "worktree" in str(exc))

        # 2. candidate inside the worktree is refused (checked before existence)
        try:
            stage("itaa-1997", "6", src, REPO / "data" / "nope.md", root=root)
            check("refuse in-repo output", False)
        except StagingError as exc:
            check("refuse in-repo output", "worktree" in str(exc))

        # 3. clean stage -> READY
        rep = stage("itaa-1997", "6", src, out, gate_report=gate_ok, root=root)
        d = root / "itaa-1997" / "6"
        check("stage writes 4 artifacts",
              all((d / n).is_file() for n in (*ARTIFACTS, "report.json")),
              f"risks={rep['risks']}")
        check("clean stage READY", _inspect(d)["status"] == "READY", str(_inspect(d)))
        check("apply_list has it", apply_list(root) == ["itaa-1997/6"])
        check("assert_ready passes", assert_ready("itaa-1997", "6", root)["status"] == "READY")

        # 4. tampered output is FAIL
        (d / "output.md").write_text(good_out + "x")
        check("tamper -> FAIL", _inspect(d)["status"] == "FAIL", str(_inspect(d)["reasons"]))
        (d / "output.md").write_text(good_out)

        # 5. missing artifact is FAIL
        (d / "diff.txt").unlink()
        check("missing diff -> FAIL", _inspect(d)["status"] == "FAIL")
        (d / "diff.txt").write_text("restored")

        # 6. rejected / absent gate is FAIL
        stage("itaa-1997", "6", src, out, gate_report=gate_no, root=root, force=True)
        check("gate REJECTED -> FAIL", _inspect(d)["status"] == "FAIL")
        stage("itaa-1997", "6", src, out, root=root, force=True)
        check("no gate report -> FAIL", _inspect(d)["status"] == "FAIL")

        # 7. risk flag -> BLOCKED, review -> READY, re-stage -> BLOCKED again
        stage("itaa-1997", "6", src, out, gate_report=gate_ok, root=root,
              force=True, flags=["ambiguous_page_mapping"])
        check("risky -> BLOCKED", _inspect(d)["status"] == "BLOCKED", str(_inspect(d)["reasons"]))
        check("blocked not in apply_list", "itaa-1997/6" not in apply_list(root))
        try:
            assert_ready("itaa-1997", "6", root)
            check("assert_ready blocks", False)
        except StagingError as exc:
            check("assert_ready blocks", "BLOCKED" in str(exc))
        review("itaa-1997", "6", by="selfcheck", note="synthetic", root=root)
        check("review -> READY", _inspect(d)["status"] == "READY")
        review_path = d / "review.json"
        stage("itaa-1997", "6", src, out, gate_report=gate_ok, root=root,
              force=True, flags=["ambiguous_page_mapping"])
        check("re-stage drops review", not review_path.exists() and
              _inspect(d)["status"] == "BLOCKED")

        # 7b. a RE-INGEST gate report (R* gates) is accepted, auto-detected,
        #     and its needs_review becomes a risk flag that BLOCKs.
        gate_ri = tmp / "gate-reingest.json"
        gate_ri.write_text(json.dumps({"verdict": "ACCEPTED", "needs_review": True,
                                       "gates": {"R1_pdf_token_recall": {"pass": True}}}))
        rep = stage("itaa-1997", "6", src, out, gate_report=gate_ri, root=root, force=True)
        check("reingest kind detected", rep["gate"]["kind"] == "reingest", str(rep["gate"]))
        check("needs_review -> risk", "preserved_regions" in rep["risks"], str(rep["risks"]))
        check("needs_review -> BLOCKED", _inspect(d)["status"] == "BLOCKED")
        review("itaa-1997", "6", by="selfcheck", note="read the region", root=root)
        check("reviewed reingest READY", _inspect(d)["status"] == "READY")
        (d / "output.md").write_text(good_out + " tampered")
        check("reingest tamper -> FAIL", _inspect(d)["status"] == "FAIL")
        try:
            stage("itaa-1997", "6", src, out, gate_report=gate_ri, root=root,
                  force=True, gate_kind="table")
            check("wrong --gate-kind refused", False)
        except StagingError as exc:
            check("wrong --gate-kind refused", "--gate-kind" in str(exc))
        gate_ri2 = tmp / "gate-reingest-clean.json"
        gate_ri2.write_text(json.dumps({"verdict": "ACCEPTED", "needs_review": False,
                                        "gates": {"R1_pdf_token_recall": {"pass": True}}}))
        stage("itaa-1997", "6", src, out, gate_report=gate_ri2, root=root,
              force=True, gate_kind="reingest")
        check("clean reingest READY", _inspect(d)["status"] == "READY", str(_inspect(d)))

        # 8. unstaged section is refused
        try:
            assert_ready("itaa-1997", "999-99", root)
            check("unstaged refused", False)
        except StagingError as exc:
            check("unstaged refused", "not staged" in str(exc))

        # 9. default root lives outside the worktree
        check("default root outside repo", REPO not in default_root().resolve().parents
              and default_root().resolve() != REPO, str(default_root()))

    print("selfcheck:", "PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selfcheck" in argv:
        return selfcheck()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help=f"staging root (default {default_root()}, env {ENV_ROOT})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("stage")
    st.add_argument("--act", required=True)
    st.add_argument("--section", required=True)
    st.add_argument("--source", required=True)
    st.add_argument("--output", required=True)
    st.add_argument("--gate-report")
    st.add_argument("--gate-kind", choices=sorted(set(GATE_KINDS.values())),
                    help="assert which gate wrote --gate-report (auto-detected "
                         "from its G*/R* gate names when omitted)")
    st.add_argument("--flag", action="append", default=[])
    st.add_argument("--force", action="store_true")

    sr = sub.add_parser("stage-run", help="stage a whole re-ingest run from its gate reports")
    sr.add_argument("--act", required=True)
    sr.add_argument("--gate-json", required=True,
                    help="an ingest_reingest_gate --json report, or a directory of them")
    sr.add_argument("--corpus-root", help="live corpus for the diff (default data/<act>)")
    sr.add_argument("--summary", help="write the machine-readable run summary here")

    sub.add_parser("verify")
    sub.add_parser("list")

    rv = sub.add_parser("review")
    rv.add_argument("--act", required=True)
    rv.add_argument("--section", required=True)
    rv.add_argument("--by", required=True)
    rv.add_argument("--note", default="")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "stage":
            rep = stage(a.act, a.section, a.source, a.output, gate_report=a.gate_report,
                        flags=a.flag, root=a.root, force=a.force, gate_kind=a.gate_kind)
            print(f"staged {a.act}/{a.section}  risks={rep['risks'] or 'none'}  "
                  f"gate={rep['gate']['verdict']} ({rep['gate']['kind'] or 'absent'})")
            return 0
        if a.cmd == "stage-run":
            res = stage_run(a.act, a.gate_json, corpus_root=a.corpus_root,
                            root=a.root, summary=a.summary)
            print(f"staged {res['sections']} section(s) into {res['root']}"
                  f"  ({res['new_sections']} with no live corpus counterpart)")
            for st in ("READY", "BLOCKED", "FAIL"):
                print(f"  {st:7s} {res['histogram'].get(st, 0)}")
            for why, n in Counter(res["reasons"]).most_common():
                print(f"     {n:6d}  {why}")
            if a.summary:
                print(f"summary: {a.summary}")
            return 1 if res["histogram"].get("FAIL") else (2 if res["histogram"].get("BLOCKED") else 0)
        if a.cmd == "review":
            rec = review(a.act, a.section, by=a.by, note=a.note, root=a.root)
            print(f"reviewed {a.act}/{a.section} by {rec['approved_by']} @ {rec['output_sha256'][:12]}")
            return 0
        results = verify(a.root)
        if a.cmd == "list":
            for key in apply_list(a.root):
                print(key)
            return 0
        return _print_verify(results)
    except StagingError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
