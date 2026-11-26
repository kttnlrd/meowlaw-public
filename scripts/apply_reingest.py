#!/usr/bin/env python3
"""CDN-0193 — the whole-section applier: staged READY re-ingest -> live corpus.

The counterpart to scripts/apply_itaa_table_fixes.py, which is a per-TABLE
driver: it re-extracts a table from the PDF and rebuilds the text itself, so it
cannot write a table-less section and it does not write the bytes a human
reviewed.  This one does exactly one thing: it copies a staged `output.md` over
the corpus file BYTE-FOR-BYTE.  Nothing is re-extracted, re-rendered or
reformatted here — the reviewed bytes are the bytes that land.

Refusals (a refusal is per section; the batch continues only with --keep-going):
  * not READY in the staging tree (verdict comes from table_rebuild_staging,
    never reimplemented here);
  * the staged output's sha256 no longer matches its staging record (tamper);
  * the corpus file's sha256 no longer matches the source hash recorded at
    staging time (the corpus moved underneath the candidate);
  * a review record that is not pinned to this exact output hash;
  * no corpus file recorded at staging time (a NEW section — creating files is
    a different decision from replacing reviewed ones).

Writes are atomic per file (temp file in the same directory, fsync, os.replace)
and resumable (an append-only JSONL log; a re-run skips what it already wrote
unless --force).  After a real batch, scripts/corpus_change_guard.py runs over
the changed files and a BLOCK is a hard failure — with ONE adjudicated
exception.  The guard's G-B rule ("prose collapsed while table content grew")
was written for the per-TABLE rebuild path, where the prose around a table must
survive byte-for-byte.  On a whole-section RE-INGEST, prose becoming rows is the
fix being applied.  So a BLOCK whose reasons are ALL G-B prose-collapse is
adjudicated per file: staged verdict ACCEPTED with needs_review false, every
collapsed prose token present in the section's source PDF, and every one of
those tokens now sitting in a table row or preserved region (words that exist in
the old file only inside an HTML tag — the ingester's own `<a id=...>` anchors —
are markup, not text, and are exempt from both; a word that also occurs outside a
tag is not).  All three hold ->
WARN with the evidence recorded; anything else, or any other rule in the block
-> the batch fails exactly as before.  --no-adjudicate turns this off.

Usage
  /usr/bin/python3.12 scripts/apply_reingest.py --act itaa-1997 \\
      --staging-root ../legislation-explorer-staging/table-rebuild \\
      [--only 82-150,30-15] [--limit N] [--dry-run] [--keep-going] \\
      [--force] [--log apply-log.jsonl] [--corpus-root DIR] [--no-guard]
      [--no-adjudicate] [--pdf-dir DIR]

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STAGING = _load("table_rebuild_staging")   # verdict logic lives there, not here
GUARD = _load("corpus_change_guard")       # the guard itself, for its own primitives

# The comp-266 volumes the re-ingest was certified against (same tree
# scripts/apply_itaa_table_fixes.py resolves against).
PDF_DIR = Path(os.path.join(os.environ["LEGISLATION_STAGING_DIR"], "data", "itaa-1997", "raw", "comp266"))


class ApplyError(RuntimeError):
    """This section will not be written; the reason is the message."""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str | None:
    """Hash of a file on disk, or None if it cannot be read (missing, unreadable)."""
    try:
        return _sha(p.read_bytes())
    except OSError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── the per-section decision ────────────────────────────────────────────────
def plan_section(act: str, section: str, status: dict, root: Path,
                 corpus_root: Path | None = None) -> dict:
    """What would be written for one section, or ApplyError with the reason.

    `status` is table_rebuild_staging's own verdict for this section.
    """
    key = f"{act}/{section}"
    if status is None:
        raise ApplyError(f"{key}: not staged under {root}")
    if status["status"] != "READY":
        why = "; ".join(status.get("reasons") or []) or ",".join(status.get("risks") or [])
        raise ApplyError(f"{key}: {status['status']} — {why}")

    d = root / act / section
    rep = json.loads((d / "report.json").read_text())

    out_path = d / "output.md"
    new_bytes = out_path.read_bytes()
    new_sha = _sha(new_bytes)
    want = (rep.get("artifacts") or {}).get("output.md", {}).get("sha256")
    if want != new_sha:
        raise ApplyError(f"{key}: staged output.md sha256 {new_sha[:12]} != staged record "
                         f"{str(want)[:12]} — tampered after staging")

    src = rep.get("source_path")
    target = _resolve_target(act, section, src, corpus_root)
    if not target.is_file():
        raise ApplyError(f"{key}: corpus file {target} is gone since staging")
    old_bytes = target.read_bytes()
    old_sha = _sha(old_bytes)
    if old_sha != rep.get("source_sha256"):
        raise ApplyError(f"{key}: corpus file changed since staging "
                         f"(now {old_sha[:12]}, staged against {str(rep.get('source_sha256'))[:12]})")

    rev_path = d / "review.json"
    if rev_path.is_file():
        rev = json.loads(rev_path.read_text())
        if rev.get("output_sha256") != new_sha:
            raise ApplyError(f"{key}: review.json is pinned to output "
                             f"{str(rev.get('output_sha256'))[:12]}, not {new_sha[:12]} "
                             f"— stale approval")

    return {"act": act, "section": section, "target": target, "bytes": new_bytes,
            "old_bytes": old_bytes, "old_sha256": old_sha, "new_sha256": new_sha, "size": len(new_bytes),
            "verdict": status["status"], "changed": old_sha != new_sha,
            "reviewed_by": status.get("reviewed_by")}


def _resolve_target(act: str, section: str, recorded: str | None, corpus_root: Path | None) -> Path:
    """The corpus file this section must be written to, found by IDENTITY, not by trust.

    plan_section used to take the recorded `source_path` at face value. For 187 of the
    hand-reviewed sections that record is a /tmp copy, so the applier would have
    overwritten the temp file, reported success, and left the corpus untouched — a silent
    no-op dressed as an apply. A section id occurs exactly once in the corpus tree, so
    resolve it there and refuse anything that does not line up.
    """
    if not recorded:
        # The record says this section had no corpus file when it was staged. Even if a
        # file matching the id exists now, that contradiction means the candidate was not
        # built against it — and creating sections is a different decision from replacing
        # reviewed ones. Refuse.
        raise ApplyError(f"{act}/{section}: no corpus file recorded at staging time (new section) — "
                         f"this applier replaces reviewed files, it does not create them")
    base = (Path(corpus_root) if corpus_root else REPO / "data") / act / "sections"
    if not base.is_dir():
        raise ApplyError(f"{act}/{section}: corpus sections dir {base} does not exist")
    hits = sorted(base.rglob(f"{section}.md"))
    if len(hits) > 1:
        raise ApplyError(f"{act}/{section}: {len(hits)} corpus files match {section}.md "
                         f"({', '.join(str(h) for h in hits[:3])}…) — ambiguous, refusing")
    if not hits:
        raise ApplyError(f"{act}/{section}: no corpus file for {section}.md under {base} — "
                         f"this applier replaces reviewed files, it does not create them")
    target = hits[0]
    if corpus_root is None and recorded:
        rec = Path(recorded)
        try:
            inside = rec.resolve().is_relative_to((REPO / "data").resolve())
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise ApplyError(f"{act}/{section}: recorded source_path {rec} is not inside the "
                             f"corpus (data/) — it is a staging copy, not the corpus file. "
                             f"Refusing to write {target.name} to the wrong place.")
        if rec.resolve() != target.resolve():
            raise ApplyError(f"{act}/{section}: recorded source_path {rec} disagrees with the "
                             f"corpus file {target} — refusing")
    return target


def _corpus_base(src: str, act: str) -> Path:
    """The `data/<act>` prefix of a recorded corpus path, for re-rooting."""
    p = Path(src)
    for parent in p.parents:
        if parent.name == act:
            return parent
    raise ApplyError(f"cannot locate '{act}' in recorded corpus path {src}")


# ── the write ───────────────────────────────────────────────────────────────
def atomic_write(target: Path, data: bytes) -> None:
    """Temp file in the same directory, fsync, os.replace: no partial file."""
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_log(path: Path) -> dict[str, dict]:
    """Sections the log claims were written, keyed by 'act/section'.

    A log entry is a claim about the past, not evidence about the present: the corpus may
    have been rolled back or reverted since (git checkout after a batch, a restore from
    archive). Callers must confirm the corpus still holds the recorded new bytes before
    trusting a skip — otherwise a stale log makes an applier report 'already applied' for
    files that are back at their old content.
    """
    done: dict[str, dict] = {}
    if not path.is_file():
        return done
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("applied"):
            done[f"{rec.get('act')}/{rec.get('section')}"] = rec
    return done


def append_log(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def guard_paths(paths: list[Path]) -> list[str]:
    """The path strings handed to the guard: repo-relative where possible."""
    rel = []
    for p in paths:
        try:
            rel.append(str(p.resolve().relative_to(REPO)))
        except ValueError:
            rel.append(str(p))
    return rel


def run_guard(paths: list[Path]) -> tuple[int, str, list[dict]]:
    """corpus_change_guard over the files we just changed. rc 1 == BLOCK.

    Also returns the guard's own per-file JSON report, so the adjudication
    below reads the guard's verdicts rather than scraping its printout.
    """
    rel = guard_paths(paths)
    with tempfile.TemporaryDirectory() as td:
        jpath = Path(td) / "guard.json"
        proc = subprocess.run([sys.executable, str(SCRIPTS / "corpus_change_guard.py"),
                               "--paths", *rel, "--quiet-ok", "--json", str(jpath)],
                              capture_output=True, text=True, cwd=str(REPO))
        results = json.loads(jpath.read_text()) if jpath.is_file() else []
    text = (proc.stdout + proc.stderr).strip()
    # The guard only looks at data/<act>/sections/*.md paths inside the repo.
    # A run that inspected 0 files is not a pass — say so rather than show a
    # green line over files nobody checked.
    if " 0 changed section file(s)" in text:
        return 1, text + ("\n*** guard inspected 0 files — the applied paths are "
                          "outside the repo corpus, so nothing was checked ***"), results
    return proc.returncode, text, results


# ── adjudication of a G-B prose-collapse BLOCK ──────────────────────────────
# G-B was written for the per-TABLE rebuild path, where the prose around a table
# must survive byte-for-byte, so "non-pipe content collapsed while pipe content
# grew" is loss there.  On a whole-section RE-INGEST that shape is the FIX: the
# corpus stored a real table as mangled prose and the re-ingest rebuilt it from
# the PDF.  The verdict is right for an incremental diff and wrong as a pass/fail
# here — so it is adjudicated per file against the source PDF, never relaxed.
GB_COLLAPSE_RE = re.compile(r"^\[BLOCK\] G-B prose collapsed ")
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")


def lost_prose_tokens(old: str, new: str) -> dict[str, int]:
    """The tokens G-B counted as gone from prose — with the guard's own split,
    word regex and frontmatter blanking, so this is the guard's arithmetic."""
    _, old_prose = GUARD._split(old)
    _, new_prose = GUARD._split(new)
    lost = Counter(GUARD.WORD_RE.findall("\n".join(old_prose)))
    lost.subtract(Counter(GUARD.WORD_RE.findall("\n".join(new_prose))))
    return {w: c for w, c in sorted(lost.items()) if c > 0}


def classify_lines(text: str) -> list[tuple[str, int, str]]:
    """(kind, 1-based line no, line) with kind in row/region/prose.

    ponytail: a fence scan and a leading '|', not the gate's parser — this only
    has to say WHERE a token now sits, never whether the region is valid.
    """
    out, in_region = [], False
    for i, ln in enumerate(GUARD.body_lines(text), 1):
        if FENCE_RE.match(ln):
            in_region = not in_region
            out.append(("region", i, ln))
        elif in_region:
            out.append(("region", i, ln))
        elif ln.lstrip().startswith("|"):
            out.append(("row", i, ln))
        else:
            out.append(("prose", i, ln))
    return out


def section_pdf_text(out_text: str, gate_path: Path, pdf_dir: Path = PDF_DIR) -> tuple[str, str]:
    """(text, provenance) for the section's PDF pages.

    The volume is the `source_pdf` the re-ingest wrote into the staged output's
    frontmatter; the page span is the `pdf_pages` the re-ingest gate recorded
    when it certified that output.  Both come from the staging record, not from
    a fresh guess about where the section lives.
    """
    m = re.search(r'^source_pdf:\s*"?([^"\n]+?)"?\s*$', out_text, re.M)
    if not m:
        raise ApplyError("staged output has no source_pdf in its frontmatter")
    if not gate_path.is_file():
        raise ApplyError(f"gate report {gate_path} is gone — no recorded page span")
    span = json.loads(gate_path.read_text()).get("pdf_pages")
    if not span:
        raise ApplyError("gate report records no pdf_pages span")
    pdf = pdf_dir / m.group(1)
    if not pdf.is_file():
        raise ApplyError(f"source volume {pdf} not found")
    lo, _, hi = str(span).partition("-")
    import fitz                                   # only needed to adjudicate
    doc = fitz.open(pdf)
    try:
        pages = range(int(lo), int(hi or lo) + 1)
        text = "\n".join(doc[n - 1].get_text() for n in pages)
    finally:
        doc.close()
    return text, f"{pdf.name} pp.{span}"


_GATE = None
TAG_RE = re.compile(r"<[^>]*>")


def norm_words(text: str) -> list[str]:
    """Lowercased words after the FIDELITY GATE's own normalisation (soft hyphens,
    line-end hyphenation, smart quotes, markdown emphasis), cut with the GUARD's own
    word regex.  Every side of every comparison below goes through this, so a
    straight apostrophe in the corpus and a typographic one in the re-ingested
    output are the same word — which they are.
    """
    global _GATE
    if _GATE is None:
        _GATE = _load("table_rebuild_gate")       # imports fitz; adjudication only
    return [w.lower() for w in GUARD.WORD_RE.findall(" ".join(_GATE.norm_tokens(text)))]


def markup_only_tokens(old: str) -> set[str]:
    """Words that exist in the old file ONLY inside an HTML tag.

    The damaged corpus carries generated anchors — `<a id="s112-77-a"></a>` — and
    G-B counts `id` and `s112-77-a` as prose tokens.  They were never in the PDF
    because they were never law text; they are the ingester's own markup.  A word
    that ALSO occurs outside a tag is not covered here and still has to be proven
    present in the PDF, so this cannot hide the loss of any real text.
    """
    _, prose = GUARD._split(old)
    text = "\n".join(prose)
    inside = set(norm_words(" ".join(TAG_RE.findall(text))))
    outside = set(norm_words(TAG_RE.sub(" ", text)))
    return inside - outside


def adjudicate_file(res: dict, plan: dict, status: dict, root: Path,
                    old_text: str | None = None, pdf_text: str | None = None,
                    pdf_dir: Path = PDF_DIR) -> dict:
    """Is this BLOCK a proven prose->table reclassification? -> evidence dict.

    verdict ADJUDICATED only when all three hold:
      (a) the staged verdict is ACCEPTED with needs_review false;
      (b) every token G-B counted as lost is in the section's source PDF;
      (c) every one of those tokens now sits in a table row or preserved region.
    Anything else — including a BLOCK carrying any other rule — is FATAL.
    """
    ev = {"path": res["path"], "act": plan["act"], "section": plan["section"],
          "reasons": res["reasons"], "verdict": "FATAL", "why": "", "tokens": [],
          "in_pdf": 0, "reclassified": 0, "markup_only": 0, "pdf": None}

    blocks = [r for r in res["reasons"] if r.startswith("[BLOCK] ")]
    if not blocks or not all(GB_COLLAPSE_RE.match(r) for r in blocks):
        ev["why"] = "block carries a rule other than G-B prose-collapse"
        return ev

    # (a) the staged verdict — table_rebuild_staging's, never recomputed here
    d = root / plan["act"] / plan["section"]
    rep = json.loads((d / "report.json").read_text())
    gate = rep.get("gate") or {}
    if status.get("status") != "READY" or gate.get("verdict") != "ACCEPTED" \
            or gate.get("needs_review"):
        ev["why"] = (f"staged verdict is {status.get('status')}/{gate.get('verdict')} "
                     f"needs_review={gate.get('needs_review')} — not ACCEPTED-clean")
        return ev

    # The "before" side: the bytes this applier actually replaced, hash-pinned to
    # the staging record.  Adjudicating a different diff from the one the guard
    # judged would prove nothing, so re-run the guard's own check_file over this
    # pair and refuse unless it reproduces the very reasons under adjudication.
    new_text = plan["bytes"].decode("utf-8", "replace")
    if old_text is None:
        old_text = plan["old_bytes"].decode("utf-8", "replace")
    if not old_text.strip():
        ev["why"] = "no 'before' text to compare against — nothing can be proven"
        return ev
    mine = GUARD.check_file(old_text, new_text, res["path"])
    if mine["reasons"] != res["reasons"]:
        ev["why"] = ("the guard judged a different diff from the bytes this applier "
                     f"replaced (guard: {res['reasons']}; replaced: {mine['reasons']})")
        return ev
    tokens = lost_prose_tokens(old_text, new_text)

    try:
        if pdf_text is None:
            pdf_text, ev["pdf"] = section_pdf_text(new_text, Path(gate.get("path") or ""), pdf_dir)
        words = set(norm_words(pdf_text))
    except ApplyError as exc:
        ev["why"] = f"cannot read the source PDF for this section: {exc}"
        return ev
    except Exception as exc:                       # a broken PDF proves nothing
        ev["why"] = f"source PDF unreadable: {exc.__class__.__name__}: {exc}"
        return ev

    lines = [(kind, i, set(norm_words(ln))) for kind, i, ln in classify_lines(new_text)]
    markup = markup_only_tokens(old_text)
    for tok, n in tokens.items():
        norm = norm_words(tok)
        key = norm[0] if norm else tok.lower()
        hits = [f"{kind}:{i}" for kind, i, words_on_line in lines
                if kind != "prose" and key in words_on_line]
        ev["tokens"].append({"token": tok, "lost": n, "in_pdf": key in words,
                             "markup": key in markup,
                             "now_in": sorted({h.split(":")[0] for h in hits}),
                             "at": hits[:5]})
        ev["in_pdf"] += bool(key in words)
        ev["reclassified"] += bool(hits)
    ev["markup_only"] = sum(t["markup"] for t in ev["tokens"])

    # Markup-only tokens are exempt from BOTH tests: they are not text the PDF
    # ever held, and there is nothing for the output to reclassify them into.
    real = [t for t in ev["tokens"] if not t["markup"]]
    missing = [t["token"] for t in real if not t["in_pdf"]]
    gone = [t["token"] for t in real if t["in_pdf"] and not t["now_in"]]
    if missing:
        ev["why"] = (f"{len(missing)} token(s) not in the source PDF: "
                     f"{', '.join(missing[:8])} — this is a real loss, not a reclassification")
        return ev
    if gone:
        ev["why"] = (f"{len(gone)} token(s) are in the PDF but appear nowhere in a table row "
                     f"or preserved region of the output: {', '.join(gone[:8])}")
        return ev
    ev["verdict"] = "ADJUDICATED"
    ev["why"] = (f"all {len(real)} collapsed prose token(s) are in the source PDF and now "
                 f"sit in a table row or preserved region "
                 f"({ev['markup_only']} further token(s) were HTML-markup-only) — "
                 f"prose->table reclassification, not loss")
    return ev



# ── driver ──────────────────────────────────────────────────────────────────
def apply_run(act: str, root: Path, only: list[str] | None = None, limit: int | None = None,
              dry_run: bool = False, keep_going: bool = False, force: bool = False,
              log: Path | None = None, corpus_root: Path | None = None,
              guard: bool = True, adjudicate: bool = True, pdf_dir: Path = PDF_DIR,
              out=sys.stdout) -> dict:
    root = STAGING.stage_root(root)
    statuses = STAGING.verify(root)          # one pass; verdicts are theirs
    done = load_log(log) if (log and not force) else {}

    if only is not None:
        # An EXPLICIT filter, even an empty one, is never 'everything'.  Treating an
        # empty --only as 'no filter' is how a 25-section batch once wrote the entire
        # corpus: the caller's section list came back empty and the applier applied all
        # 4,140 READY sections.
        sections = [s for s in only if s]
    else:
        sections = [k.split("/", 1)[1] for k in sorted(statuses)
                    if k.split("/", 1)[0] == act and statuses[k]["status"] == "READY"]
    if limit is not None:
        sections = sections[:limit]

    applied, skipped, refused, unchanged, stale = [], [], [], 0, []
    for section in sections:
        key = f"{act}/{section}"
        if key in done:
            claimed = done[key]
            target = Path(claimed.get("path") or "")
            current = _sha256_file(target) if target else None
            if current and current == claimed.get("new_sha256"):
                skipped.append(key)
                continue
            # The corpus no longer holds what the log says we wrote — rolled back,
            # restored from archive, or edited by hand. The log is stale, so re-apply
            # rather than reporting a file as done when it is back at its old bytes.
            stale.append(key)
        try:
            plan = plan_section(act, section, statuses.get(key), root, corpus_root)
        except ApplyError as exc:
            refused.append(str(exc))
            print(f"REFUSE {exc}", file=out)
            if not keep_going:
                break
            continue

        if not plan["changed"]:
            unchanged += 1
        if dry_run:
            print(f"WOULD  {key}  {plan['old_sha256'][:12]} -> {plan['new_sha256'][:12]}  "
                  f"{plan['size']}B{'' if plan['changed'] else '  (no byte change)'}  {plan['target']}",
                  file=out)
            applied.append(plan)
            continue

        atomic_write(plan["target"], plan["bytes"])
        rec = {"ts": _now(), "act": act, "section": section, "path": str(plan["target"]),
               "old_sha256": plan["old_sha256"], "new_sha256": plan["new_sha256"],
               "bytes": plan["size"], "verdict": plan["verdict"],
               "changed": plan["changed"], "applied": True}
        if log:
            append_log(log, rec)
        print(f"APPLY  {key}  {plan['old_sha256'][:12]} -> {plan['new_sha256'][:12]}  "
              f"{plan['size']}B", file=out)
        applied.append(plan)

    summary = {"act": act, "root": str(root), "dry_run": dry_run,
               "considered": len(sections), "applied": len(applied),
               "unchanged_bytes": unchanged, "skipped_already_applied": len(skipped),
               "stale_log_reapplied": len(stale), "stale_log_sections": stale,
               "refused": len(refused), "refusals": refused}

    if not dry_run and applied and guard:
        rc, text, results = run_guard([p["target"] for p in applied])
        summary["guard"] = {"returncode": rc, "verdict": "BLOCK" if rc else "OK",
                            "output": text}
        print(text, file=out)
        if rc and adjudicate:
            by_path = dict(zip(guard_paths([p["target"] for p in applied]), applied))
            ev = [adjudicate_file(r, by_path[r["path"]], statuses[f"{act}/{by_path[r['path']]['section']}"],
                                  root, pdf_dir=pdf_dir)
                  for r in results if r["status"] == "BLOCK" and r["path"] in by_path]
            unknown = [r["path"] for r in results
                       if r["status"] == "BLOCK" and r["path"] not in by_path]
            summary["adjudication"] = {"files": ev, "unattributed": unknown,
                                       "adjudicated": sum(e["verdict"] == "ADJUDICATED" for e in ev),
                                       "fatal": sum(e["verdict"] == "FATAL" for e in ev) + len(unknown)}
            for e in ev:
                head = "WARN" if e["verdict"] == "ADJUDICATED" else "BLOCK"
                print(f"{head} {e['verdict']} {e['path']}"
                      + (f"  [{e['pdf']}]" if e["pdf"] else ""), file=out)
                print(f"        {e['why']}", file=out)
                for t in e["tokens"]:
                    print(f"        token {t['token']!r} x{t['lost']}  in_pdf={t['in_pdf']}"
                          f"{'  markup-only' if t['markup'] else ''}  "
                          f"now_in={','.join(t['now_in']) or 'NOWHERE'}"
                          f"{'  @ ' + ' '.join(t['at']) if t['at'] else ''}", file=out)
            if summary["adjudication"]["fatal"] == 0:
                rc = 0
                summary["guard"]["verdict"] = "ADJUDICATED"
                print(f"*** every BLOCK adjudicated: {len(ev)} file(s) are proven prose->table "
                      f"reclassifications against the source PDF. Batch stands. ***", file=out)
        summary["guard"]["returncode"] = rc
        if rc:
            print("*** corpus_change_guard BLOCKED the applied files — "
                  "this batch is NOT safe to keep. ***", file=out)
    return summary


def _parse_only(value: str | None) -> list[str] | None:
    """None means 'no filter'. A PROVIDED filter that yields no ids is an error.

    An empty `--only` used to fall through to 'no filter', i.e. apply everything: a
    caller whose section list came back empty silently turned a 25-section batch into a
    full-corpus write. Refuse it instead.
    """
    if value is None:
        return None
    ids = [s.strip() for s in value.split(",") if s.strip()]
    if not ids:
        raise SystemExit(
            "--only was given but contains no section ids. Refusing to interpret that as "
            "'apply everything'. Pass real ids, or omit --only to apply every READY section."
        )
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--act", required=True)
    ap.add_argument("--staging-root")
    ap.add_argument("--corpus-root", help="re-root recorded corpus paths (throwaway copies/tests)")
    ap.add_argument("--only", help="comma-separated section ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-going", action="store_true", help="continue past a refusal")
    ap.add_argument("--force", action="store_true", help="ignore the log and re-apply")
    ap.add_argument("--log", help="append-only JSONL apply log")
    ap.add_argument("--no-guard", action="store_true", help="skip corpus_change_guard")
    ap.add_argument("--no-adjudicate", action="store_true",
                    help="raw guard behaviour: a BLOCK is always fatal, never adjudicated")
    ap.add_argument("--pdf-dir", help=f"source volumes for adjudication (default {PDF_DIR})")
    ap.add_argument("--summary", help="write the run summary JSON here")
    args = ap.parse_args()

    try:
        summary = apply_run(
            args.act, args.staging_root,
            only=_parse_only(args.only),
            limit=args.limit, dry_run=args.dry_run, keep_going=args.keep_going,
            force=args.force, log=Path(args.log) if args.log else None,
            corpus_root=Path(args.corpus_root) if args.corpus_root else None,
            guard=not args.no_guard, adjudicate=not args.no_adjudicate,
            pdf_dir=Path(args.pdf_dir) if args.pdf_dir else PDF_DIR)
    except STAGING.StagingError as exc:
        print(f"apply_reingest: {exc}", file=sys.stderr)
        return 2

    print(f"apply_reingest: {'would apply' if args.dry_run else 'applied'} {summary['applied']}"
          f"/{summary['considered']} — {summary['skipped_already_applied']} already applied, "
          f"{summary['refused']} refused, {summary['unchanged_bytes']} byte-identical")
    if summary.get("stale_log_reapplied"):
        print(f"apply_reingest: {summary['stale_log_reapplied']} section(s) were logged as "
              f"applied but the corpus no longer holds those bytes (rolled back, restored or "
              f"edited since) — re-applied, not skipped: "
              f"{', '.join(summary['stale_log_sections'][:10])}"
              f"{' …' if len(summary['stale_log_sections']) > 10 else ''}")
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    if summary.get("guard", {}).get("returncode"):
        return 1
    return 1 if summary["refused"] and not args.keep_going else 0


if __name__ == "__main__":
    sys.exit(main())
