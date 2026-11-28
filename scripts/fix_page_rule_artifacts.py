#!/usr/bin/env python3.12
"""CDN-0203: a page rule left in the text, splitting a sentence and swallowing a Note.

The source PDFs draw a horizontal rule at a page break.  The extractor kept it as a run of
underscores in the middle of a paragraph, which:
  * leaves 6-80 underscores where the rule was, and
  * splits the sentence across it (the text resumes on the next line), and
  * swallows a following Note, which then reads as part of the subsection instead of its own
    block (the corpus convention elsewhere is a blockquote "> **Note:** ...").

Measured before this fixer: 522 underscore runs in PROSE across 320 files (itaa-1997 491,
gst-1999 16, taa-1953 14, master-tax-guide 1).  A further 62 runs sit INSIDE formula fences
(52 files) where they are legitimate fraction bars and must not be touched - that is why this
walks fences rather than grepping.

Dry run by default.  --apply writes in blocks and records every change so the write is
reversible; --verify re-checks the applied bytes against the recorded provenance.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
# ── provenance (X2, 2026-09-26) ─────────────────────────────────────────────
# This was /tmp/cdn203-apply.json: a write outside the repo, lost on reboot, invisible to
# git, and the template every Part B repair script would have copied. A repair's record of
# what it changed belongs with the repo, next to the rollback point it names.
#   every write  -> docs/provenance/<ticket>-<UTC stamp>.json   (one file per apply run)
#   --verify     -> reads the newest such file, or --log to name one
TICKET = "cdn203-page-rule-artifacts"
PROVENANCE_DIR = ROOT / "docs" / "provenance"

RULE = re.compile(r"[ \t]*_{6,}[ \t]*")
# "…see Division 27. Note If you receive…" - a Note that should be its own block.
INLINE_NOTE = re.compile(r"(?<=[.;:])\s+Note\s+(?=[A-Z])")
# A dot-leadered index row ("capital gains .......... 102-5") is a structured line, not a
# sentence: strip its rule, never merge it with the next row.
INDEX_ROW = re.compile(r"\.{5,}")


def _strip_rules(line: str) -> tuple[str, int]:
    """Remove page-rule runs from one line.  Returns (line, rules_removed)."""
    n = len(RULE.findall(line))
    return (RULE.sub(" ", line).rstrip(), n) if n else (line, 0)


def rewrite(text: str) -> tuple[str, list[dict]]:
    """Return (new text, changes).  Only prose is touched; fences are copied verbatim.

    Every consumed line's own rule is stripped as it is consumed - the first version stripped
    only the line it started on, so a rule sitting at the end of the CONTINUATION line rode into
    the merged sentence.  That left 21 runs on index rows, which is how this was caught.
    """
    out: list[str] = []
    changes: list[dict] = []
    lines = text.split("\n")
    infence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            infence = not infence
            out.append(line)
            i += 1
            continue
        if infence:
            out.append(line)
            i += 1
            continue

        # A page rule rendered INSIDE a table is a row of underscores.  Blanking its cells leaves
        # an empty "| | | |" row behind (which the corpus guard then flags as glue); the row is the
        # rule, so the whole row goes.
        if line.lstrip().startswith("|") and not re.sub(r"[|_\s]", "", line):
            changes.append({"kind": "page_rule_row_dropped", "line": i + 1, "before": line[:120]})
            i += 1
            continue

        new, n_rules = _strip_rules(line)
        if n_rules:
            changes.append({"kind": "page_rule_removed", "line": i + 1, "rules": n_rules,
                            "before": line[:120]})
            # join forward only when a sentence was actually split - and never for an index row
            if not INDEX_ROW.search(line) and not re.search(r"[.;:)]\s*$", new):
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and re.match(r"^[a-z,;)]", lines[j].strip()):
                    nxt, n_nxt = _strip_rules(lines[j])
                    if n_nxt:
                        changes.append({"kind": "page_rule_removed", "line": j + 1, "rules": n_nxt,
                                        "before": lines[j][:120]})
                    new = new.rstrip() + " " + nxt.lstrip()
                    changes.append({"kind": "sentence_rejoined", "line": i + 1, "with_line": j + 1,
                                    "joined": nxt.lstrip()[:80]})
                    i = j
        # a Note consumed by the paragraph becomes its own block, matching the corpus convention.
        # The guard skips real block/list/table marks but MUST allow bold, because a subsection
        # begins "**(3)** ..." and the first draft of this guard silently skipped every one of
        # them (caught by the dry run, not by the corpus).
        if INLINE_NOTE.search(new) and not re.match(r"^(>|[-+]\s|\d+\.\s|\|)", new.lstrip()):
            new = INLINE_NOTE.sub("\n\n> **Note:** ", new, count=1)
            changes.append({"kind": "note_split", "line": i + 1})
        out.append(new)
        i += 1
    return "\n".join(out), changes


def scan() -> None:
    files = 0
    kinds: dict[str, int] = {}
    sample: list[str] = []
    for p in sorted(DATA.rglob("sections/**/*.md")):
        text = p.read_text(errors="replace")
        _, ch = rewrite(text)
        if not ch:
            continue
        files += 1
        for c in ch:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        if len(sample) < 6:
            sample.append(f"  {p.relative_to(DATA)}: " + "; ".join(c["kind"] for c in ch[:3]))
    print(f"  files affected: {files}")
    print(f"  changes by kind: {kinds}")
    print("  sample:")
    for s in sample:
        print(s)


def latest_log() -> Path | None:
    """Newest docs/provenance/<ticket>-*.json written by --apply (None if there is none)."""
    if not PROVENANCE_DIR.is_dir():
        return None
    runs = sorted(PROVENANCE_DIR.glob(f"{TICKET}-*.json"))
    return runs[-1] if runs else None


def rollback_tag() -> tuple[str, str]:
    """Tag HEAD before the first write, the way apply_operator_proposal.py:175-177 does.

    A tag is the rollback point a reviewed bulk edit is allowed to have; recording the sha in
    the provenance file means the log names the state the write can be undone to.
    """
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    tag = f"page-rule-apply-rollback-{head}" if head else "page-rule-apply-rollback-nogit"
    if head:
        subprocess.run(["git", "tag", "-f", tag], cwd=ROOT, check=True,
                       capture_output=True, text=True)
    return head, tag


def apply(block_size: int) -> int:
    records = []
    targets = []
    for p in sorted(DATA.rglob("sections/**/*.md")):
        text = p.read_text(errors="replace")
        new, ch = rewrite(text)
        if ch:
            targets.append((p, text, new, ch))
    print(f"  {len(targets)} file(s) to change")
    if not targets:
        print("  nothing to do - no tag, no provenance file")
        return 0
    head, tag = rollback_tag()
    print(f"  rollback point: tag {tag} (HEAD {head or 'unknown'})")
    written = 0
    for n, (p, before, after, ch) in enumerate(targets, 1):
        p.write_text(after, encoding="utf-8")
        records.append({"file": str(p.relative_to(ROOT)), "changes": ch,
                        "lines_before": len(before.split("\n")), "lines_after": len(after.split("\n"))})
        written += 1
        if n % block_size == 0:
            print(f"    block: {n}/{len(targets)} files written")
    PROVENANCE_DIR.mkdir(parents=True, exist_ok=True)
    log = PROVENANCE_DIR / f"{TICKET}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    log.write_text(json.dumps({"ticket": TICKET, "script": str(Path(__file__).name),
                               "rollback_tag": tag, "head": head,
                               "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "files": records}, indent=1))
    print(f"  wrote {written} file(s); provenance at {log.relative_to(ROOT)}")
    return 0


def verify(log: Path | None = None) -> int:
    """Every recorded change must be re-derivable from the current bytes.

    Re-derives rather than re-checking the recorded line numbers: editing shifts them, so the
    first version of this reported phantom problems against the wrong lines.
    """
    log = log or latest_log()
    if log is None or not log.exists():
        print(f"  no provenance file under {PROVENANCE_DIR} - nothing to verify")
        return 1
    print(f"  verifying against {log}")
    data = json.loads(log.read_text())
    bad = 0
    for rec in data["files"]:
        p = ROOT / rec["file"]
        if not p.exists():
            print(f"  MISSING {rec['file']}")
            bad += 1
            continue
        text = p.read_text(errors="replace")
        _, again = rewrite(text)
        rules_left = [c for c in again if c["kind"] == "page_rule_removed"]
        if rules_left:
            print(f"  {rec['file']}: {len(rules_left)} page rule(s) still present after the fix")
            bad += 1
    print("  VERIFIED: no page rule left in prose" if not bad else f"  {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--log", help="provenance file to verify against (default: the newest)")
    ap.add_argument("--block-size", type=int, default=50)
    a = ap.parse_args()
    if a.verify:
        sys.exit(verify(Path(a.log) if a.log else None))
    if a.apply:
        sys.exit(apply(a.block_size))
    scan()
