#!/usr/bin/env python3
"""CDN-0193 Phase 0: build a PROPOSED rebuilt section file and gate it.

Never writes to data/.  Candidates land in
$TABLE_REBUILD_OUT_DIR (default ~/table-rebuild-out)/<act>/<section>.md with a sibling
<section>.gate.json.

What it does for one (act, section):
  1. pick the source PDF from the VERIFIED map below and prove its compilation
     number matches the corpus file's frontmatter before reading a word of it,
  2. extract the section's table in-process with the fixed extractor (R10),
  3. collapse the section's multi-copy table region into ONE table, leaving
     frontmatter and every non-table line byte-identical,
  4. run all the gates in table_rebuild_gate and write the report.

Run under /usr/bin/python3.12 (python3.11 has no fitz).
"""
from __future__ import annotations
import os

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import extract_itaa_tables_pdf as EX  # noqa: E402
import table_rebuild_gate as GATE  # noqa: E402

REPO = SCRIPTS.parent
DATA = REPO / "data"
OUT = Path(os.path.expanduser(os.environ.get("TABLE_REBUILD_OUT_DIR", "~/table-rebuild-out")))
STAGING = Path(os.environ["LEGISLATION_STAGING_DIR"])

# VERIFIED source map.  The plan's table is wrong for itaa-1997 and itaa-1936; these paths
# were checked against the PDFs' own footers.
#
# S1 (batch 5, 2026-09-26): the repo now vendors the comp-266 volumes itself as
# source/itaa-1997/C2026C00324VOL*.pdf, so it no longer has to read them from staging.
# S2 (same batch): gst-1999 moved from source/gst-1999-vol{1,2}.pdf into source/gst-1999/.
#
# TRAP (unchanged, now guarded by register id instead of a directory name):
# staging/source/itaa-1997/C2026C00122VOL*.pdf are compilation 263 while the corpus is 266.
# Rebuilding from 263 would silently revert law text, so any candidate volume whose own
# authorised-version register is on the `forbid` list is refused before it is read.
REPO_SOURCE = REPO / "source"
SOURCES: dict[str, dict] = {
    "itaa-1997": {
        "comp": 266,
        "pdfs": sorted((REPO_SOURCE / "itaa-1997").glob("*.pdf")),
        "forbid": ("C2026C00122",),
    },
    "sis-1993": {"comp": 126, "pdfs": [STAGING / "source/sis-1993/part1.pdf",
                                       STAGING / "source/sis-1993/part2.pdf"]},
    "fbt-1986": {"comp": 96, "pdfs": [STAGING / "source/fbt-1986/part1.pdf",
                                      STAGING / "source/fbt-1986/part2.pdf"]},
    "taa-1953": {"comp": 222, "pdfs": sorted((STAGING / "source/taa-1953").glob("vol*.pdf"))},
    "itaa-1936": {"comp": 191, "pdfs": sorted((STAGING / "source/itaa-1936").glob("C2026C00165VOL*.pdf"))},
    # gst-1999: the plan (and codex/opus) said "no PDFs" — WRONG.  Two exact-compilation
    # volumes exist (verified footer: "Compilation No. 96", pages 430 + 306).  Since S2 they
    # live in the repo at source/gst-1999/, which is also what rebuild.sh's 1b guard reads.
    "gst-1999": {"comp": 96, "pdfs": [REPO_SOURCE / "gst-1999" / "gst-1999-vol1.pdf",
                                      REPO_SOURCE / "gst-1999" / "gst-1999-vol2.pdf"]},
}

COMP_RE = re.compile(r"Compilation No\.\s*(\d+)")


def find_section_file(act: str, section: str) -> Path:
    hits = sorted((DATA / act / "sections").rglob(f"{section}.md"))
    if not hits:
        raise SystemExit(f"no corpus file for {act} {section}")
    return hits[0]


def frontmatter_comp(path: Path) -> int | None:
    m = re.search(r"^compilation_no:\s*(\d+)", path.read_text(encoding="utf-8"), re.M)
    return int(m.group(1)) if m else None


def pdf_compilation(pdf: Path) -> int | None:
    import fitz
    doc = fitz.open(pdf)
    for pno in range(min(12, doc.page_count)):
        m = COMP_RE.search(doc[pno].get_text("text"))
        if m:
            return int(m.group(1))
    return None


def pick_pdf(act: str, section: str, want_comp: int, override: Path | None):
    """The PDF volume holding the section — refusing anything off-compilation.

    Returns EVERY distinct table the extractor finds, not the biggest: taking
    max() by row count is what dropped whole tables.  G1 then proves the set is
    complete against the candidate.
    """
    import fitz
    cfg = SOURCES.get(act)
    if cfg is None:
        raise SystemExit(f"{act}: no verified source map entry")
    cands = [override] if override else list(cfg["pdfs"])
    if not cands:
        raise SystemExit(f"{act}: no source PDFs (repo raw text only) — "
                         f"out of scope for the PDF rebuild path")
    # The trap guard, now keyed on the volume's own register id rather than a directory name:
    # the superseded comp-263 volumes (C2026C00122) must never be read for a comp-266 corpus.
    forbid = cfg.get("forbid") or ()
    if forbid:
        bad = [p for p in cands if any(f in Path(p).name.upper() for f in forbid)]
        if bad:
            raise SystemExit(
                "REFUSING itaa-1997 rebuild: source must be a comp-266 volume "
                f"(got {bad[0]}).  C2026C00122VOL*.pdf are compilation 263 and "
                "would silently revert the corpus from 266 to 263.")
    best = None
    for pdf in cands:
        if not Path(pdf).exists():
            continue
        got = pdf_compilation(Path(pdf))
        if got != want_comp:
            print(f"  skip {Path(pdf).name}: compilation {got} != corpus {want_comp}")
            continue
        doc = fitz.open(pdf)
        tables = GATE.dedup_tables(EX.collect_tables(doc, section, detail=True))
        if not tables:
            continue
        rows = sum(len(t["rows"]) for t in tables)
        # Most rows wins the VOLUME choice; every table in it is kept either way.
        if best is None or rows > best[2]:
            best = (Path(pdf), tables, rows)
    if best is None:
        raise SystemExit(f"{act} {section}: no table found in any compilation-"
                         f"{want_comp} volume")
    return best[0], best[1]


def render_table(t: dict) -> list[str]:
    header = [t.get("id_label") or "Item"] + list(t["header"])
    n = len(header)
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join(["---"] * n) + " |"]
    for r in t["rows"]:
        cells = [r["item"]] + [r["cols"].get(str(i), "") for i in range(1, n)]
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return out


def build_candidate(original: Path, tables: list[dict]) -> str:
    """Every distinct table where the multi-copy region was; other lines untouched.

    The corrupt files repeat the table header once per source page with the
    rows and stray blockquote shards interleaved.  The rebuilt table replaces
    the whole '|'-line span.  Non-'|' lines inside that span are kept, in
    order, immediately after the table — G2 checks that byte-for-byte, so the
    rebuild cannot quietly drop prose (including prose that is really stranded
    table text; de-duplicating that is a Phase 1 decision, not a gate-time one).
    """
    lines = original.read_text(encoding="utf-8").splitlines()
    idx = [i for i, l in enumerate(lines) if l.startswith("|")]
    if not idx:
        raise SystemExit(f"{original}: no markdown table to rebuild")
    lo, hi = idx[0], idx[-1]
    region_prose = [l for l in lines[lo:hi + 1] if not l.startswith("|")]

    notes = []
    existing = {tuple(GATE.norm_tokens(l)) for l in lines if not l.startswith("|")}
    for t in tables:
        for n in t.get("notes") or []:
            if tuple(GATE.norm_tokens(n)) not in existing:
                notes.append(n)

    body: list[str] = []
    for i, t in enumerate(tables):
        if i:
            body.append("")          # blank line between consecutive tables
        body += render_table(t)
    if notes:
        body += [""] + notes
    out = lines[:lo] + body + region_prose + lines[hi + 1:]
    return "\n".join(out) + "\n"


def calibrate(act: str, section: str, pdf_override: Path | None = None) -> dict:
    original = find_section_file(act, section)
    want = frontmatter_comp(original)
    cfg = SOURCES.get(act, {})
    if want is None:
        raise SystemExit(f"{original}: no compilation_no in frontmatter")
    if cfg.get("comp") and want != cfg["comp"]:
        raise SystemExit(f"{original}: frontmatter compilation {want} != verified "
                         f"map {cfg['comp']} — source map is stale, stop")
    print(f"== {act} {section}  ({original.relative_to(REPO)}, compilation {want})")
    pdf, tables = pick_pdf(act, section, want, pdf_override)
    print(f"   source: {pdf}  {len(tables)} distinct table(s)  "
          + "  ".join(f"[{len(t['rows'])}x{t['cols']} p{t['page']}]" for t in tables))

    dest = OUT / act / f"{section}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build_candidate(original, tables), encoding="utf-8")

    rep = GATE.gate_file(act, original, dest, tables)
    rep["section"] = section
    rep["source_pdf"] = str(pdf)
    rep["pdf_pages"] = [t["page"] for t in tables]
    (OUT / act / f"{section}.gate.json").write_text(
        json.dumps(rep, indent=2, ensure_ascii=False))

    print(f"   {rep['verdict']}")
    for name, g in rep["gates"].items():
        print(f"     {'PASS  ' if g['pass'] else 'REJECT'} {name}"
              + (f"\n            {g['reason'][:400]}" if g["reason"] else ""))
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--act", required=True)
    ap.add_argument("--section", required=True, nargs="+")
    ap.add_argument("--pdf", type=Path, help="override the source volume")
    a = ap.parse_args()
    bad = 0
    for sec in a.section:
        try:
            if calibrate(a.act, sec, a.pdf)["verdict"] != "ACCEPTED":
                bad += 1
        except SystemExit as e:
            print(f"== {a.act} {sec}\n   ABORTED: {e}")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
